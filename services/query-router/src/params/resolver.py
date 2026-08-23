"""Parameter resolution for model-level user-defined parameters.

Resolution order (highest precedence first):
  1. Persona default filter (persona ``default_filters`` keyed by ``@name``)
  2. JDBC session variable (``SET app.<param_name> = <value>``)
  3. Model default value (from ``model_parameters.default_value``)

Unresolved required parameter (no default, no session var) raises ValueError.

Multi-value wire encoding (Bug-8068). Every JDBC session variable arrives as a
single string over a ``dict[str, str]`` wire, so the string IS the multi-value
format. Two forms are accepted and resolve identically:

  * JSON array (lossless, preferred) -- ``SET app.city = '["New York, NY","Paris"]'``
  * legacy comma list -- ``SET app.region = 'EMEA,APAC'``

Only the JSON form can carry a member containing a comma; the comma form is kept
unchanged for every value that does not need it. Members must be strings or
numbers and are normalised to strings, matching the element type
``_enforce_allowed_values`` validates against.

Substitution is **AST-level placeholder binding, not text replacement**. The
SQL is run through sqlglot's dialect tokenizer, which classifies each lexeme:
a genuine ``@name`` placeholder is emitted as a ``PARAMETER`` token followed
by its name ``VAR`` token, whereas a ``@name`` lexeme that sits *inside a
string literal* (e.g. ``'a@segment.com'``) or an identifier is wholly
contained in a single ``STRING`` / identifier token and is **never** emitted
as a ``PARAMETER`` placeholder. Binding therefore touches only the real
placeholder spans the lexer recognises, and can never corrupt a ``@name``
that appears inside an author's string literal (Bug-1105 / F-029-05).

Each bound placeholder is replaced by the generator output of a sqlglot
*typed literal* node (``exp.Literal`` / ``exp.Boolean``), so a value can never
break out of its literal or alter the query structure: a string containing
quotes or semicolons is doubled and contained by sqlglot, never spliced raw
into the SQL text (F-029-01). The tokenizer approach is used rather than a
full ``parse_one`` because the documented author forms ``IN (@p)`` and
``BETWEEN @p`` (which expand a single placeholder into several literals) do
not parse as a complete statement while the placeholder is still present —
the lexer recognises the placeholder span regardless of overall parseability.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.tokens import TokenType
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import ModelParameter

# A declared parameter name must be ``@`` followed by a leading
# letter/underscore and then word characters (enforced at create time in
# model-service ``parameters.py`` via ``_PARAM_NAME_RE``). The sqlglot lexer
# only emits a ``PARAMETER`` token for an ``@`` immediately followed by such a
# name token, so binding here addresses exactly those spans and any
# out-of-shape ``@token`` is simply never bound.


class ParameterError(ValueError):
    """Raised when a parameter cannot be resolved or its value is invalid.

    A subclass of ``ValueError`` so existing callers that catch
    ``ValueError`` continue to work; the route layer maps it to a 400.
    """


def colliding_sigil_bare_names(canonical_names: list[str]) -> set[str]:
    """Bare names that appear both with and without a leading ``@``.

    Bug-9493: ``.lstrip("@")`` collapses ``@Region`` and ``Region`` into one
    catalogue identity. Callers use this set to keep session-variable keys
    unambiguous while preserving the modern ``@Name`` → ``app.name`` contract.
    """
    with_sigil: set[str] = set()
    without: set[str] = set()
    for name in canonical_names:
        bare = name.lstrip("@").lower()
        if not bare:
            continue
        if name.startswith("@"):
            with_sigil.add(bare)
        else:
            without.add(bare)
    return with_sigil & without


def parameter_session_var_key(
    canonical_name: str,
    *,
    colliding_bare: set[str] | None = None,
) -> str:
    """Exact JDBC session-variable key for a deployed parameter.

    Modern create-time names always carry ``@`` and publish ``app.<bare>``.
    When a legacy bare name collides with a sigil form of the same bare token,
    the catalogue still publishes a distinct key for the legacy row
    (``app.legacy.<bare>``) so identities remain auditable — but both colliding
    rows must be marked ``sql_usable=false`` (R2-PCR-002). Supported SQL can
    only address ``@Name`` placeholders; a usable legacy override would be a
    silent no-op.
    """
    bare = canonical_name.lstrip("@")
    if (
        colliding_bare
        and bare.lower() in colliding_bare
        and not canonical_name.startswith("@")
    ):
        return f"app.legacy.{bare}".lower()
    return f"app.{bare}".lower()


async def resolve_parameters(
    model_id: str,
    session_vars: dict[str, str],
    persona_filters: dict[str, Any],
    db: AsyncSession,
    referenced_names: set[str] | None = None,
    deployed_params: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve parameters for a model query.

    Returns a dict mapping parameter name (with @) to its resolved value,
    coerced to the declared type.

    ``referenced_names`` (Bug-6410): when provided, only parameters whose
    ``@name`` actually appears as a placeholder in the query are resolved and
    required-checked.

    ``deployed_params`` (F-029-01 / Bug-6422): when provided, these snapshot-
    backed parameter definitions supersede the live ``ModelParameter`` ORM
    query. This pins parameter defaults to the deployed model version so a
    draft default change cannot silently alter production query results before
    redeployment. Each element is a dict with at least ``name``, ``param_type``,
    ``default_value``, and ``allowed_values`` keys (matching the snapshot's
    ``model_parameters`` serialisation). Callers pass this when the model has a
    deployed version; for undeployed models, leave None to read live ORM.
    """
    if deployed_params is not None:
        # F-029-01: use the deployed snapshot's parameter definitions.
        import types as _types
        params = [
            _types.SimpleNamespace(
                name=p.get("name", ""),
                param_type=p.get("param_type", "string"),
                default_value=p.get("default_value"),
                allowed_values=p.get("allowed_values"),
            )
            for p in deployed_params
        ]
    else:
        result = await db.execute(
            select(ModelParameter).where(ModelParameter.model_id == model_id)
        )
        params = result.scalars().all()

    # Session-variable names are case-insensitive: the JDBC gateway folds
    # ``SET app.<name>`` to lower-case (Postgres GUC semantics), while model
    # parameter names may carry mixed case (``@RegionCode`` — ``_PARAM_NAME_RE``
    # in model-service allows ``[A-Za-z_]``). Matching the ``app.<bare_name>``
    # lookup with the parameter's exact case therefore never hit a folded
    # session variable, so any mixed-case parameter silently fell back to its
    # default and could never be set over JDBC (Bug-6411). Match
    # case-insensitively against a lower-cased view of the session variables.
    session_lower = {k.lower(): v for k, v in session_vars.items()}

    # Bug-7663: referenced_names from the query's placeholder spans preserve
    # the author's exact case (e.g. ``@regioncode``), while declared parameter
    # names may use different casing (e.g. ``@RegionCode``). Match
    # case-insensitively, consistent with the session-variable channel
    # (Bug-6411). Build a lower-cased lookup so both sides compare uniformly.
    referenced_lower: set[str] | None = None
    if referenced_names is not None:
        referenced_lower = {n.lower() for n in referenced_names}

    # Bug-7663: persona_filters may also carry case-variant keys from the
    # persona definition. Build a case-insensitive lookup.
    persona_lower = {k.lower(): v for k, v in persona_filters.items()}

    colliding_bare = colliding_sigil_bare_names([str(p.name or "") for p in params])

    resolved: dict[str, Any] = {}
    for p in params:
        name = p.name
        # Bug-6410: skip parameters the query never references so an unused
        # required parameter cannot fail an otherwise-valid query.
        # Bug-7663: comparison is now case-insensitive.
        if referenced_lower is not None and name.lower() not in referenced_lower:
            continue
        session_key = parameter_session_var_key(
            str(name or ""), colliding_bare=colliding_bare,
        )

        if name.lower() in persona_lower:
            value = _coerce(p.param_type, persona_lower[name.lower()], name)
        elif session_key in session_lower:
            value = _coerce(p.param_type, session_lower[session_key], name)
        elif p.default_value is not None:
            value = _coerce(p.param_type, p.default_value, name)
        else:
            raise ParameterError(
                f"Required parameter '{name}' has no default value, "
                f"session variable, or persona filter"
            )

        _enforce_allowed_values(p.param_type, value, p.allowed_values, name)
        resolved[name] = value

    return resolved


def _enforce_allowed_values(
    param_type: str, value: Any, allowed_values: Any, name: str
) -> None:
    """Reject a resolved value that is not in the modeler's declared
    ``allowed_values`` list (F-029-02).

    Governance check, fail-closed: when a parameter declares an allowed list,
    the resolved value must be a member. For ``multi_value`` every element must
    be a member. Comparison is type-normalised — each allowed entry is coerced
    through the same scalar logic as the parameter's element type so that a
    declared ``[10, 20]`` matches a coerced numeric ``10`` and a declared
    ``["EMEA"]`` matches a coerced string, never failing on a ``"10"`` vs ``10``
    spelling mismatch. An empty or absent list imposes no restriction.
    """
    if not allowed_values:
        return
    if not isinstance(allowed_values, list):
        # Malformed governance config — treat as no restriction rather than
        # locking every value out, but this shape is rejected at create time.
        return
    if param_type == "date_range":
        # A date_range resolves to a {from,to} object, not a discrete value;
        # an allowed-values whitelist has no meaning for it. Skip enforcement.
        return

    # The element type for membership: multi_value members are scalars, so
    # normalise against the implied element type rather than the list type.
    element_type = "string" if param_type == "multi_value" else param_type

    def _norm(v: Any) -> Any:
        try:
            return _coerce(element_type, v, name)
        except ParameterError:
            # An allowed entry that cannot be coerced to the element type can
            # never match a coerced value; drop it from the comparison set.
            return _SENTINEL

    allowed_set = {_norm(a) for a in allowed_values}
    allowed_set.discard(_SENTINEL)

    values = value if isinstance(value, list) else [value]
    for item in values:
        if item not in allowed_set:
            raise ParameterError(
                f"Parameter '{name}' value {item!r} is not one of the "
                f"allowed values {sorted(map(repr, allowed_values))}"
            )


_SENTINEL = object()


def _coerce(param_type: str, value: Any, name: str) -> Any:
    """Coerce a value to the declared parameter type.

    Coercion is strict and keyed off the declared ``param_type`` only — the
    runtime value is never re-inferred. This keeps the declared type the
    single source of truth at both resolve and substitute time.
    """
    if param_type == "string":
        return str(value)
    elif param_type == "number":
        try:
            if isinstance(value, str):
                return float(value) if "." in value else int(value)
            if isinstance(value, bool):
                # bool is an int subclass; reject to avoid TRUE->1 surprises.
                raise ValueError
            if isinstance(value, (int, float)):
                return value
            raise ValueError
        except (ValueError, TypeError):
            raise ParameterError(f"Parameter '{name}' expects a number, got {value!r}")
    elif param_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            # Bug-5890: this used to be `value.lower() in ("true", "1",
            # "yes")`, so ANY other string -- including a typo like "maybe"
            # or a genuinely invalid value -- silently coerced to False. A
            # mistyped persona default, session variable, or model default
            # then produced a valid-looking but wrong filtered result
            # instead of a clear error. Use a closed token set in both
            # directions and reject anything else loudly.
            normalized = value.strip().lower()
            if normalized in ("true", "1", "yes"):
                return True
            if normalized in ("false", "0", "no"):
                return False
            raise ParameterError(
                f"Parameter '{name}' expects a boolean, got {value!r} "
                "(accepted: true/false, 1/0, yes/no)"
            )
        # Bug-7439: the remaining branch used to be ``return bool(value)``, so a
        # non-string, non-boolean value took Python truthiness: ``2`` became
        # True and ``[]`` became False. A persona default or model default of
        # the wrong shape then silently enabled or inverted a row filter and the
        # user saw a plausible but wrong result set. Same closed-token stance as
        # the string branch above — reject loudly instead.
        raise ParameterError(
            f"Parameter '{name}' expects a boolean, got {type(value).__name__} "
            f"{value!r} (accepted: a real boolean, or true/false, 1/0, yes/no)"
        )
    elif param_type == "multi_value":
        if isinstance(value, list):
            # A list arrives from a persona default or a JSON body. Its elements
            # were previously passed straight through to the renderer, so a dict
            # or nested list became ``str(obj)`` inside a SQL literal — a filter
            # member that matches nothing, silently. Validate here too.
            members = list(value)
        elif isinstance(value, str):
            members = _decode_multi_value_string(value, name)
        else:
            members = [value]
        if not members:
            # Bug-7665: an empty multi_value rendered as ``IN ()`` — a syntax
            # error surfaced to the user as an unrelated database message. There
            # is no correct SQL for "filter on no members", so reject it at the
            # parameter boundary where the message can name the parameter.
            raise ParameterError(
                f"Parameter '{name}' is a multi-value parameter and needs at "
                f"least one value; got an empty list"
            )
        return [_coerce_multi_value_member(m, name) for m in members]
    elif param_type == "date_range":
        if isinstance(value, dict):
            # F-029-02: validate the {from,to} object rather than trusting any
            # two keys — a non-ISO or inverted range is a wrong filter presented
            # as a successful query.
            return _validate_date_range_obj(value, name)
        # Bug-6413: JDBC session variables (``SET app.<name> = '...'``) are
        # always strings, so a date_range set over JDBC arrives as text rather
        # than a {from,to} object and could never be supplied at all. Accept the
        # documented string forms and normalise them to the {from,to} object the
        # rest of the pipeline expects.
        if isinstance(value, str):
            parsed = _parse_date_range_string(value)
            if parsed is not None:
                # F-029-02: even a well-shaped 'a,b' string must have ISO bounds
                # ('banana,pear' -> BETWEEN 'banana' AND 'pear' is a wrong query).
                return _validate_date_range_obj(parsed, name)
        raise ParameterError(
            f"Parameter '{name}' expects a date_range as a {{'from','to'}} object, "
            f"a JSON object with 'from'/'to' keys, or two dates separated by "
            f"',' or '..' (e.g. '2024-01-01,2024-12-31'), got {value!r}"
        )
    return value


def _parse_iso_bound(value: Any, name: str, side: str) -> datetime:
    """F-029-02: parse one date_range bound as an ISO-8601 date or datetime.

    ``datetime.fromisoformat`` (Python 3.11+) accepts both ``YYYY-MM-DD`` and a
    full timestamp and enforces zero-padded ISO, which is exactly the contract we
    want. Returns a ``datetime`` so the two bounds are order-comparable.
    """
    if not isinstance(value, str):
        raise ParameterError(
            f"Parameter '{name}' date_range '{side}' must be an ISO-8601 date "
            f"string (e.g. '2024-01-01'), got {value!r}."
        )
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        raise ParameterError(
            f"Parameter '{name}' date_range '{side}' is not a valid ISO-8601 "
            f"date: {value!r}."
        )


def _validate_date_range_obj(obj: dict[str, Any], name: str) -> dict[str, Any]:
    """F-029-02: validate a ``{from,to}`` date_range object.

    Rejects a missing bound, unexpected extra keys, non-ISO bounds, and an
    inverted (from > to) range. The wire form of each bound is preserved so the
    dialect quoting in ``_render`` is unchanged.
    """
    if "from" not in obj or "to" not in obj:
        raise ParameterError(
            f"Parameter '{name}' date_range requires both 'from' and 'to' keys; "
            f"got keys {sorted(obj.keys())}."
        )
    extra = set(obj) - {"from", "to"}
    if extra:
        raise ParameterError(
            f"Parameter '{name}' date_range has unexpected key(s): "
            f"{', '.join(sorted(extra))}; only 'from' and 'to' are allowed."
        )
    lo = _parse_iso_bound(obj["from"], name, "from")
    hi = _parse_iso_bound(obj["to"], name, "to")
    if lo > hi:
        raise ParameterError(
            f"Parameter '{name}' date_range is inverted: 'from' ({obj['from']!r}) "
            f"is after 'to' ({obj['to']!r})."
        )
    return {"from": obj["from"], "to": obj["to"]}


def _decode_multi_value_string(value: str, name: str) -> list[Any]:
    """Decode a multi-value parameter supplied as a single string (Bug-8068).

    Every JDBC session variable arrives as a string (``SET app.<name> = '...'``)
    and the gateway forwards it verbatim over a ``dict[str, str]`` wire, so the
    string IS the multi-value wire format. The only encoding was "split on every
    comma", which cannot represent a member that contains a comma: a filter on
    the real member ``New York, NY`` became the two members ``New York`` and
    ``NY`` and the user silently got the wrong rows.

    Two forms are accepted, mirroring how ``date_range`` already accepts both a
    JSON object and a delimited pair:

      * JSON array (lossless, preferred) --
        ``["New York, NY","Paris"]`` -> ``["New York, NY", "Paris"]``
      * legacy comma-separated list (unchanged) -- ``EMEA,APAC``

    A string that opens with ``[`` OR closes with ``]`` is treated as an
    attempted JSON form. If it does not parse as a JSON array it is rejected
    rather than silently comma-split: either delimiter demonstrates that the
    caller attempted the structured encoding, and falling back would
    reintroduce exactly the silent wrong-rows failure this decoder exists to
    remove.
    """
    text = value.strip()
    if text.startswith("[") or text.endswith("]"):
        try:
            decoded = json.loads(text)
        except ValueError as exc:
            raise ParameterError(
                f"Parameter '{name}' looks like a JSON array but is not valid "
                f"JSON ({exc}). Use a JSON array of scalars, e.g. "
                f'\'["New York, NY","Paris"]\', or a plain comma-separated list '
                f"for values that contain no commas."
            ) from exc
        if not isinstance(decoded, list):
            raise ParameterError(
                f"Parameter '{name}' expects a JSON ARRAY of values, got "
                f"{type(decoded).__name__}"
            )
        return decoded
    # Legacy form: split on commas. Unchanged for every value that does not use
    # the JSON encoding, so existing session variables keep working.
    return [v.strip() for v in text.split(",")]


def _coerce_multi_value_member(member: Any, name: str) -> str:
    """Normalise one multi-value member to a string, rejecting non-scalars.

    ``multi_value`` members are discrete filter values whose element type is
    string — ``_enforce_allowed_values`` normalises the modeller's declared
    ``allowed_values`` against the same ``"string"`` element type, and the legacy
    comma split always produced strings. Keeping that contract means the two wire
    forms (JSON array and comma list) resolve identically.

    A boolean, null, object, or nested array is not a filter member. Passing one
    through would render ``'True'`` / ``'None'`` / ``"{'a': 1}"`` as a SQL
    literal — a member that matches nothing, with no error anywhere.
    """
    if isinstance(member, bool) or member is None or isinstance(member, (dict, list)):
        raise ParameterError(
            f"Parameter '{name}' multi-value members must be strings or numbers; "
            f"got {type(member).__name__} {member!r}"
        )
    if isinstance(member, str):
        return member
    if isinstance(member, (int, float)):
        # Rendered as a quoted literal exactly as the legacy comma split would
        # have produced for the same text, so ["10"] and [10] filter the same.
        return str(member)
    raise ParameterError(
        f"Parameter '{name}' multi-value members must be strings or numbers; "
        f"got {type(member).__name__} {member!r}"
    )


def _parse_date_range_string(value: str) -> dict[str, Any] | None:
    """Parse a date_range supplied as a session-variable string (Bug-6413).

    Two textual forms are accepted so a date_range parameter can be set over
    JDBC, where every value is a string:

      * JSON object -- ``{"from": "2024-01-01", "to": "2024-12-31"}``
      * a two-date pair separated by ``..`` or ``,`` --
        ``2024-01-01..2024-12-31`` / ``2024-01-01,2024-12-31``

    Returns a ``{"from": ..., "to": ...}`` dict, or ``None`` when the text is
    not a recognised date_range shape (the caller then raises ParameterError).
    """
    text = value.strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            return None
        if isinstance(obj, dict) and "from" in obj and "to" in obj:
            return {"from": obj["from"], "to": obj["to"]}
        return None
    # Delimited two-date form: check ``..`` before ``,`` so an ISO date that
    # contains neither is never mis-split.
    for sep in ("..", ","):
        if sep in text:
            parts = [part.strip() for part in text.split(sep)]
            if len(parts) == 2 and parts[0] and parts[1]:
                return {"from": parts[0], "to": parts[1]}
            return None
    return None


# ---------------------------------------------------------------------------
# AST-level placeholder binding (sqlglot generator-escaped typed literals)
# ---------------------------------------------------------------------------
#
# Binding renders each resolved value through a sqlglot literal node's
# generator (``.sql(dialect=...)``). Whatever the value contains, the only
# text spliced into the SQL is the generator's output for a *typed literal*:
# a string literal is single-quote-escaped and self-contained, a number is
# a bare numeric literal, a boolean is TRUE/FALSE. The raw value is never
# concatenated into the SQL text — it is always wrapped in a typed literal
# node first, so it cannot break out of its literal or alter query
# structure (F-029-01). Splicing happens only at placeholder spans the
# sqlglot lexer recognises, so a ``@name`` inside a string literal is never
# touched (Bug-1105 / F-029-05). The downstream parser then re-parses the
# fully-literal SQL and transpilation handles the final dialect form.

# ``@`` still gates the fast-path search in ``apply_parameters``: a query with
# no ``@`` anywhere references no placeholder, so there is nothing to bind.
_AT_RE = re.compile(r"@")


def _render_scalar(value: Any, dialect: str) -> str:
    """Render a single scalar value as a typed, generator-escaped literal."""
    if isinstance(value, bool):
        return exp.true().sql(dialect=dialect) if value else exp.false().sql(dialect=dialect)
    if isinstance(value, (int, float)):
        return exp.Literal.number(value).sql(dialect=dialect)
    return exp.Literal.string(str(value)).sql(dialect=dialect)


def _render(value: Any, dialect: str) -> str:
    """Render a resolved value to safe literal SQL text for its type.

    - scalar (string/number/boolean): a single typed literal.
    - multi_value (list): comma-joined typed literals, for use inside an
      author-supplied ``IN (@p)`` — no extra parentheses are emitted.
    - date_range (dict): ``<from> AND <to>``, for use inside an author-
      supplied ``BETWEEN @p``.
    """
    if isinstance(value, list):
        if not value:
            # Bug-7665: joining an empty list emits ``IN ()``. _coerce already
            # rejects an empty multi_value, so this is the second boundary — it
            # catches a list that reached the renderer without passing through
            # coercion (a caller building ``resolved`` directly).
            raise ParameterError(
                "A multi-value parameter resolved to an empty list; there is no "
                "valid SQL for a filter on no values. Supply at least one value."
            )
        return ", ".join(_render_scalar(v, dialect) for v in value)
    if isinstance(value, dict) and "from" in value and "to" in value:
        lo = exp.Literal.string(str(value["from"])).sql(dialect=dialect)
        hi = exp.Literal.string(str(value["to"])).sql(dialect=dialect)
        return f"{lo} AND {hi}"
    return _render_scalar(value, dialect)


def _placeholder_spans(sql: str, dialect: str) -> list[tuple[int, int, str]]:
    """Return the ``@name`` placeholder spans the sqlglot lexer recognises.

    Each entry is ``(start, end_exclusive, name)`` where ``name`` includes the
    leading ``@`` and ``sql[start:end_exclusive]`` is the exact ``@name`` text.
    Only genuine ``PARAMETER`` tokens (``@`` immediately followed by a ``VAR``
    name token) are returned; a ``@name`` lexeme inside a string literal or an
    identifier is captured in a single ``STRING`` / identifier token by the
    lexer and never appears here, so it is left untouched (Bug-1105).
    """
    tokens = sqlglot.Dialect.get_or_raise(dialect).tokenize(sql)
    spans: list[tuple[int, int, str]] = []
    for i, tok in enumerate(tokens):
        if tok.token_type is not TokenType.PARAMETER:
            continue
        # The ``@`` placeholder must be immediately followed by its name
        # token (a VAR) with no gap, i.e. ``@name``. ``tok.end`` is the
        # inclusive offset of the ``@`` character.
        if i + 1 >= len(tokens):
            continue
        nxt = tokens[i + 1]
        if nxt.token_type is not TokenType.VAR or nxt.start != tok.end + 1:
            continue
        spans.append((tok.start, nxt.end + 1, f"@{nxt.text}"))
    return spans


# Public alias — named_list_resolver.py and routes.py import this instead of
# the underscore-prefixed private name.
placeholder_spans = _placeholder_spans


def substitute_parameters(
    sql: str,
    resolved: dict[str, Any],
    dialect: str = "postgres",
    *,
    declared_names: set[str] | None = None,
) -> str:
    """Bind ``@param`` placeholders in ``sql`` to typed, generator-escaped
    literals and return the rewritten SQL.

    AST-level binding: the SQL is lexed with sqlglot's dialect tokenizer and
    only spans the lexer classifies as genuine ``@name`` placeholders are
    replaced. A ``@name`` inside a string literal or identifier is never a
    placeholder span, so it is left intact (Bug-1105 / F-029-05). Every bound
    value is rendered through a sqlglot literal node, so a value containing
    quotes or semicolons binds as one literal and cannot change the query's
    structure.

    Bug-7663: placeholder-to-resolved-parameter matching is now
    case-insensitive, consistent with the session-variable channel
    (Bug-6411). When ``declared_names`` is provided, any placeholder that
    does not match a declared parameter (case-insensitively) raises a clear
    ``ParameterError`` instead of silently surviving into the parser.
    """
    if not resolved:
        return sql

    spans = _placeholder_spans(sql, dialect)
    if not spans:
        return sql

    # Bug-7663: build a case-insensitive lookup from resolved parameters.
    resolved_lower = {k.lower(): v for k, v in resolved.items()}

    # Bug-7663: when declared_names is supplied, build a case-insensitive set
    # to detect unknown placeholders that match no declared parameter.
    declared_lower: set[str] | None = None
    if declared_names is not None:
        declared_lower = {n.lower() for n in declared_names}

    # Bug-7663: detect unknown placeholders — those that match no declared
    # parameter. These would silently survive into the parser and produce
    # cryptic syntax errors rather than a clear parameter error.
    if declared_lower is not None:
        unknown = [
            name for _, _, name in spans
            if name.lower() not in declared_lower
        ]
        if unknown:
            raise ParameterError(
                f"Unknown parameter placeholder(s) {sorted(set(unknown))} in query; "
                f"declared parameters are {sorted(declared_names or set())}"
            )

    # Rewrite right-to-left so earlier byte offsets stay valid as we splice.
    out = sql
    for start, end, name in sorted(spans, key=lambda s: s[0], reverse=True):
        lower_name = name.lower()
        if lower_name not in resolved_lower:
            continue
        out = out[:start] + _render(resolved_lower[lower_name], dialect) + out[end:]
    return out


async def apply_parameters(
    *,
    model_id: str,
    sql: str,
    session_vars: dict[str, str] | None,
    persona_filters: dict[str, Any] | None,
    db: AsyncSession,
    dialect: str = "postgres",
    extra_declared_names: set[str] | None = None,
    deployed_params: list[dict[str, Any]] | None = None,
) -> str:
    """Resolve and bind all model parameters into ``sql``.

    Single entry point for the query pipeline. Short-circuits when the
    model declares no parameters (the common case) so non-parameterised
    queries pay no cost. Resolution follows the documented precedence
    (persona default > JDBC session var > model default); binding is
    AST-level (lexer-recognised placeholder spans only).

    ``extra_declared_names``: names that are valid ``@``-prefixed
    placeholders consumed by another resolver (e.g. named lists). These
    names are added to the ``declared_names`` set so
    ``substitute_parameters`` does not reject them as unknown, but they
    are not resolved as parameters.

    ``deployed_params`` (F-029-01 / Bug-6422): when provided, these
    snapshot-backed parameter definitions supersede the live ORM query.
    Pins parameter defaults to the deployed version so draft edits cannot
    alter production results before redeployment.

    Returns the parameter-bound SQL. Raises ``ParameterError`` (a
    ``ValueError``) on an unresolved required parameter, a type-coercion
    failure, or an unparseable query — the route layer maps it to 400.
    """
    # Fast path: a query with no ``@`` anywhere references no placeholder,
    # so there is nothing to resolve or bind. Skip the DB probe entirely —
    # the overwhelmingly common case for non-parameterised queries pays zero
    # cost and touches the database not at all.
    if not _AT_RE.search(sql):
        return sql

    # Bug-7663 / F-029-01: load all declared parameter names. When deployed
    # params are provided (pinned to the deployed snapshot), use those instead
    # of querying the live ORM — so draft parameter additions/renames/deletions
    # do not affect production queries before redeployment.
    if deployed_params is not None:
        import types as _types
        all_params = [
            _types.SimpleNamespace(
                name=p.get("name", ""),
                param_type=p.get("param_type", "string"),
                default_value=p.get("default_value"),
                allowed_values=p.get("allowed_values"),
            )
            for p in deployed_params
        ]
    else:
        all_params_result = await db.execute(
            select(ModelParameter).where(ModelParameter.model_id == model_id)
        )
        all_params = all_params_result.scalars().all()
    if not all_params:
        return sql

    # Bug-5313: check for REAL placeholder spans before resolving.
    spans = _placeholder_spans(sql, dialect)
    if not spans:
        return sql

    # Bug-6410: enforce/resolve only the parameters this query actually
    # references (the recognised placeholder spans).
    referenced_names = {name for _, _, name in spans}
    resolved = await resolve_parameters(
        model_id=model_id,
        session_vars=session_vars or {},
        persona_filters=persona_filters or {},
        db=db,
        referenced_names=referenced_names,
        deployed_params=deployed_params,
    )
    # Bug-7663: pass all declared parameter names so substitute_parameters
    # can detect unknown placeholders and raise a clear ParameterError.
    # Named list names (extra_declared_names) are included so they are not
    # rejected as unknown — they will be consumed by the named list resolver.
    declared_names = {p.name for p in all_params}
    if extra_declared_names:
        declared_names |= extra_declared_names
    return substitute_parameters(
        sql, resolved, dialect=dialect, declared_names=declared_names,
    )
