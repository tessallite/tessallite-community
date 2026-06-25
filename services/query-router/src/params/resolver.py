"""Parameter resolution for model-level user-defined parameters.

Resolution order (highest precedence first):
  1. Persona default filter (persona ``default_filters`` keyed by ``@name``)
  2. JDBC session variable (``SET app.<param_name> = <value>``)
  3. Model default value (from ``model_parameters.default_value``)

Unresolved required parameter (no default, no session var) raises ValueError.

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

import re
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


async def resolve_parameters(
    model_id: str,
    session_vars: dict[str, str],
    persona_filters: dict[str, Any],
    db: AsyncSession,
) -> dict[str, Any]:
    """Resolve all parameters for a model query.

    Returns a dict mapping parameter name (with @) to its resolved value,
    coerced to the declared type.
    """
    result = await db.execute(
        select(ModelParameter).where(ModelParameter.model_id == model_id)
    )
    params = result.scalars().all()

    resolved: dict[str, Any] = {}
    for p in params:
        name = p.name
        bare_name = name.lstrip("@")

        if name in persona_filters:
            value = _coerce(p.param_type, persona_filters[name], name)
        elif f"app.{bare_name}" in session_vars:
            value = _coerce(p.param_type, session_vars[f"app.{bare_name}"], name)
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
            return value.lower() in ("true", "1", "yes")
        return bool(value)
    elif param_type == "multi_value":
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            return [v.strip() for v in value.split(",")]
        return [value]
    elif param_type == "date_range":
        if isinstance(value, dict) and "from" in value and "to" in value:
            return value
        raise ParameterError(
            f"Parameter '{name}' expects a date_range object "
            f"with 'from' and 'to' keys, got {value!r}"
        )
    return value


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


def substitute_parameters(sql: str, resolved: dict[str, Any], dialect: str = "postgres") -> str:
    """Bind ``@param`` placeholders in ``sql`` to typed, generator-escaped
    literals and return the rewritten SQL.

    AST-level binding: the SQL is lexed with sqlglot's dialect tokenizer and
    only spans the lexer classifies as genuine ``@name`` placeholders are
    replaced. A ``@name`` inside a string literal or identifier is never a
    placeholder span, so it is left intact (Bug-1105 / F-029-05). Every bound
    value is rendered through a sqlglot literal node, so a value containing
    quotes or semicolons binds as one literal and cannot change the query's
    structure. Placeholders with no resolved parameter are left untouched.
    """
    if not resolved:
        return sql

    spans = _placeholder_spans(sql, dialect)
    if not spans:
        return sql

    # Rewrite right-to-left so earlier byte offsets stay valid as we splice.
    out = sql
    for start, end, name in sorted(spans, key=lambda s: s[0], reverse=True):
        if name not in resolved:
            continue
        out = out[:start] + _render(resolved[name], dialect) + out[end:]
    return out


async def apply_parameters(
    *,
    model_id: str,
    sql: str,
    session_vars: dict[str, str] | None,
    persona_filters: dict[str, Any] | None,
    db: AsyncSession,
    dialect: str = "postgres",
) -> str:
    """Resolve and bind all model parameters into ``sql``.

    Single entry point for the query pipeline. Short-circuits when the
    model declares no parameters (the common case) so non-parameterised
    queries pay no cost. Resolution follows the documented precedence
    (persona default > JDBC session var > model default); binding is
    AST-level (lexer-recognised placeholder spans only).

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

    result = await db.execute(
        select(ModelParameter.id).where(ModelParameter.model_id == model_id).limit(1)
    )
    if result.first() is None:
        return sql

    # Bug-5313: check for REAL placeholder spans before resolving.  The
    # ``@`` fast-path above matched an ``@`` inside a string literal (e.g.
    # ``'user@company.com'``).  The lexer distinguishes real ``PARAMETER``
    # tokens from literal content; if there are no real spans, skip
    # resolution so a required parameter with no default does not raise a
    # spurious ParameterError.
    spans = _placeholder_spans(sql, dialect)
    if not spans:
        return sql

    resolved = await resolve_parameters(
        model_id=model_id,
        session_vars=session_vars or {},
        persona_filters=persona_filters or {},
        db=db,
    )
    return substitute_parameters(sql, resolved, dialect=dialect)
