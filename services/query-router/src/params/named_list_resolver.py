"""Named list resolution for the SQL query path.

Tessallite Named Lists are modeler-authored, governed sets of dimension member
values (string or numeric) that expand at query time into literal IN-clause
values in the SQL. They are the SQL-path counterpart to MDX named sets (which
remain XMLA-only).

Resolution runs **pre-parse, alongside model parameters** in ``routes.py``,
using the same sqlglot lexer-span recognition and typed-literal rendering
machinery as ``resolver.py``. After expansion, the parser, binder, security
layer, and matchers see ordinary ``WHERE col IN ('a', 'b')`` — indistinguishable
from hand-typed literals.

Key invariants:
  - Only ``list_type == "sql_fixed"`` lists are resolved on this path.
  - Members render through sqlglot typed literal nodes (``exp.Literal.string``
    / ``.number``), never raw text splice (F-029-01 guarantee).
  - Usage-shape whitelist: a named list placeholder is valid only in
    ``IN (@Name)`` or ``NOT IN (@Name)`` position.
  - Named lists read from the deployed snapshot (not live ORM).
  - Member count is capped at resolve time (defense in depth).
"""
from __future__ import annotations

import logging
import math
import os
import time
from typing import Any

from sqlglot import exp
from sqlglot.tokens import TokenType

from src.params.resolver import (
    ParameterError,
    placeholder_spans,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default member cap (configurable via env var). Hard ceiling is 5000 per spec.
# Bug-7938/Bug-7926: unified cap — the canonical env var is NAMED_LIST_MEMBER_CAP
# (shared with model-service settings.py). NAMED_LIST_MAX_MEMBERS is kept as a
# documented fallback alias for backward compatibility.
_DEFAULT_MAX_MEMBERS = 1000
_HARD_CEILING_MEMBERS = 5000


def _max_members() -> int:
    """Resolve the member cap from env, clamped to the hard ceiling.

    Reads NAMED_LIST_MEMBER_CAP first (canonical, shared with model-service);
    falls back to NAMED_LIST_MAX_MEMBERS (legacy alias) for backward compat.
    """
    raw = os.environ.get("NAMED_LIST_MEMBER_CAP") or os.environ.get(
        "NAMED_LIST_MAX_MEMBERS"
    )
    try:
        val = int(raw) if raw is not None else _DEFAULT_MAX_MEMBERS
    except (ValueError, TypeError):
        val = _DEFAULT_MAX_MEMBERS
    ceiling_raw = os.environ.get("NAMED_LIST_MEMBER_CAP_CEILING")
    try:
        ceiling = int(ceiling_raw) if ceiling_raw is not None else _HARD_CEILING_MEMBERS
    except (ValueError, TypeError):
        ceiling = _HARD_CEILING_MEMBERS
    return min(max(val, 1), max(ceiling, 1))


# ---------------------------------------------------------------------------
# Snapshot-backed named list cache
# ---------------------------------------------------------------------------
# Keyed by (model_id, deployed_version_id) — a deploy changes the version id,
# so the cache self-invalidates. A short TTL bounds staleness.

_CACHE_TTL_SECONDS = 300
_MAX_CACHE_ENTRIES = 256


class _ResolvedList:
    """A resolved named list ready for expansion."""

    __slots__ = ("name", "data_type", "members", "list_type", "builder_type")

    def __init__(
        self,
        name: str,
        data_type: str,
        members: list[Any],
        list_type: str,
        builder_type: str = "fixedMembers",
    ):
        self.name = name
        self.data_type = data_type
        self.members = members
        self.list_type = list_type
        self.builder_type = builder_type


# key -> (expires_at, resolved_lists_dict)
_NAMED_LIST_CACHE: dict[tuple[str, str], tuple[float, dict[str, _ResolvedList]]] = {}


def invalidate_named_list_cache(model_id: str | None = None) -> None:
    """Drop cached named lists. No arg clears everything (test hook)."""
    if model_id is None:
        _NAMED_LIST_CACHE.clear()
        return
    mid = str(model_id)
    for key in [k for k in _NAMED_LIST_CACHE if k[0] == mid]:
        _NAMED_LIST_CACHE.pop(key, None)


def _extract_lists_from_snapshot(
    snapshot: dict[str, Any],
) -> dict[str, _ResolvedList]:
    """Build a case-insensitive lookup of sql_fixed named lists from a snapshot.

    Returns ``{lowercase_@name: _ResolvedList}``.
    """
    result: dict[str, _ResolvedList] = {}
    for ns in snapshot.get("named_sets", []) or []:
        if not isinstance(ns, dict):
            continue
        name = ns.get("name")
        list_type = ns.get("list_type") or ""
        if not name:
            continue
        # Store ALL named sets (including MDX types) so we can give a
        # specific error when an MDX set is referenced in a SQL query.
        builder_def = ns.get("builder_definition") or {}
        data_type = builder_def.get("data_type", "string") if isinstance(builder_def, dict) else "string"
        members = builder_def.get("members", []) if isinstance(builder_def, dict) else []
        builder_type = builder_def.get("type", "fixedMembers") if isinstance(builder_def, dict) else "fixedMembers"
        if not isinstance(members, list):
            members = []
        key = f"@{name}".lower() if not name.startswith("@") else name.lower()
        # Bug-7927: fail closed on duplicate lowercase keys — if legacy
        # data has case variants, raise a clear error instead of silently
        # overwriting one with last-write-wins.
        if key in result:
            raise ParameterError(
                f"Deployed snapshot contains duplicate lowercase named-set key "
                f"'{key}' (names '{result[key].name}' and '{name}'). "
                f"Remove or rename the duplicate before deploying."
            )
        result[key] = _ResolvedList(
            name=name,
            data_type=data_type,
            members=members,
            list_type=list_type,
            builder_type=builder_type,
        )
    return result


async def load_named_lists(
    model_id: str,
    db: Any,
) -> dict[str, _ResolvedList]:
    """Load named lists from the deployed snapshot, with caching.

    If the model is not deployed or has no snapshot, returns an empty dict
    (no lists defined — an unresolvable ``@name`` then gets the standard
    unknown-placeholder error from ``substitute_parameters``).
    """
    from shared.db.models import Model, ModelVersion

    model = await db.get(Model, model_id)
    if model is None:
        return {}

    deployed_version_id = getattr(model, "deployed_version_id", None)
    if deployed_version_id is None:
        return {}

    cache_key = (str(model_id), str(deployed_version_id))
    now = time.monotonic()
    cached = _NAMED_LIST_CACHE.get(cache_key)
    if cached is not None and cached[0] > now:
        return cached[1]

    version = await db.get(ModelVersion, deployed_version_id)
    if version is None or not isinstance(version.snapshot_json, dict):
        return {}

    lists = _extract_lists_from_snapshot(version.snapshot_json)

    # Evict oldest if cache is full.
    if len(_NAMED_LIST_CACHE) >= _MAX_CACHE_ENTRIES:
        oldest_key = min(_NAMED_LIST_CACHE, key=lambda k: _NAMED_LIST_CACHE[k][0])
        _NAMED_LIST_CACHE.pop(oldest_key, None)
    _NAMED_LIST_CACHE[cache_key] = (now + _CACHE_TTL_SECONDS, lists)
    return lists


# ---------------------------------------------------------------------------
# Usage-shape validation
# ---------------------------------------------------------------------------

def _check_in_context(sql: str, span_start: int, dialect: str) -> bool:
    """Verify that the placeholder at ``span_start`` appears inside ``IN (...)``
    or ``NOT IN (...)`` context.

    Scans the token stream to check that the tokens immediately before the
    ``@`` span include an ``IN`` keyword and an opening parenthesis.
    """
    import sqlglot

    tokens = sqlglot.Dialect.get_or_raise(dialect).tokenize(sql)
    # Find the token at or just before span_start that is the PARAMETER token.
    # Then look backwards for L_PAREN and IN keyword.
    param_idx = None
    for i, tok in enumerate(tokens):
        if tok.token_type is TokenType.PARAMETER and tok.start == span_start:
            param_idx = i
            break
    if param_idx is None:
        return False

    # Walk backwards from param_idx to find L_PAREN, then IN.
    found_lparen = False
    j = param_idx - 1
    while j >= 0:
        tt = tokens[j].token_type
        # Skip whitespace/newline tokens if any
        if tt in (TokenType.L_PAREN,):
            found_lparen = True
            j -= 1
            break
        # If we hit anything that isn't whitespace or L_PAREN, the shape is wrong
        break
    if not found_lparen:
        return False

    # Now look for IN keyword (possibly preceded by NOT)
    while j >= 0:
        tt = tokens[j].token_type
        if tt is TokenType.IN:
            return True
        # NOT IN — skip NOT
        if tt is TokenType.NOT:
            j -= 1
            continue
        break

    return False


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _is_safe_number(v: Any) -> bool:
    """Return True only if ``v`` is a numeric type safe for ``exp.Literal.number``.

    Defense-in-depth (F-029-01): ``exp.Literal.number()`` does NOT quote its
    argument — it renders it as a bare literal. If a non-numeric string leaks
    through (crafted import bundle, legacy data, direct DB write), it would be
    spliced raw into the SQL, violating the typed-literal guarantee. Reject
    anything that is not a genuine Python numeric type or a strictly numeric
    string.

    Additional guards beyond basic type checking:
      - Booleans are rejected (bool is a subclass of int).
      - ``float('inf')``, ``float('nan')`` are rejected (not finite).
      - String values ``"inf"``, ``"nan"``, ``"Infinity"``, etc. are rejected.
      - Underscore-separated strings (``"1_000"``) are rejected (valid Python
        but not valid SQL numeric literal on all dialects).
      - Whitespace-padded strings (``" 12 "``) are rejected.
    """
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        try:
            return math.isfinite(v)
        except OverflowError:
            # Arbitrarily large Python int (e.g. 10**400) overflows float
            # conversion inside math.isfinite(). A huge int IS finite and
            # renders safely as a bare SQL numeric literal.
            return True
    if isinstance(v, str):
        # Reject whitespace-padded or underscore-separated values — they are
        # valid in Python's float() but not valid SQL numeric literals.
        stripped = v.strip()
        if stripped != v or "_" in v:
            return False
        try:
            parsed = float(v)
        except (ValueError, OverflowError):
            return False
        return math.isfinite(parsed)
    return False


def _render_members(
    members: list[Any], data_type: str, dialect: str, list_name: str = "",
) -> str:
    """Render a list of members as comma-joined sqlglot typed literals.

    Same rendering contract as ``resolver._render_scalar``:
      - ``data_type == "string"`` -> ``exp.Literal.string``
      - ``data_type == "number"`` -> ``exp.Literal.number`` (validated)

    F-029-01 guarantee: for number-typed members, each value is validated as
    a genuine numeric before rendering. A non-numeric value raises
    ``ParameterError`` rather than being spliced raw into the SQL.
    """
    parts: list[str] = []
    for v in members:
        if data_type == "number":
            if not _is_safe_number(v):
                raise ParameterError(
                    f"Named list '{list_name}' declares data_type 'number' but "
                    f"contains non-numeric member {v!r}. All members must be "
                    f"numeric for a number-typed list."
                )
            parts.append(exp.Literal.number(v).sql(dialect=dialect))
        else:
            parts.append(exp.Literal.string(str(v)).sql(dialect=dialect))
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Public API — single-pass integration with parameter resolution
# ---------------------------------------------------------------------------

def expand_named_lists(
    sql: str,
    named_lists: dict[str, _ResolvedList],
    dialect: str = "postgres",
    *,
    declared_param_names: set[str] | None = None,
) -> tuple[str, list[str]]:
    """Expand ``@ListName`` placeholders that match named lists.

    Must be called AFTER ``substitute_parameters`` has consumed model parameters
    (or in a coordinated pass where parameter names are excluded). Any remaining
    ``@name`` placeholders are candidates for named list expansion.

    Returns ``(expanded_sql, audit_entries)`` where ``audit_entries`` is a list
    of strings like ``"TopChannels(3 members)"`` for query-log auditing.

    Raises ``ParameterError`` (mapped to 400 by the route layer) on:
      - A placeholder matching both a parameter and a named list (namespace collision).
      - A placeholder matching an MDX-type named set (not sql_fixed).
      - A placeholder in an unsupported position (not ``IN`` / ``NOT IN``).
      - A list exceeding the member cap.
      - An empty list (zero members).
    """
    if not named_lists:
        return sql, []

    spans = placeholder_spans(sql, dialect)
    if not spans:
        return sql, []

    # Build case-insensitive param name lookup for collision detection.
    param_lower: set[str] = set()
    if declared_param_names is not None:
        param_lower = {n.lower() for n in declared_param_names}

    max_members = _max_members()
    audit: list[str] = []

    # Collect spans to expand (validate first, expand right-to-left).
    expansions: list[tuple[int, int, str, _ResolvedList]] = []
    for start, end, name in spans:
        lower_name = name.lower()
        nlist = named_lists.get(lower_name)
        if nlist is None:
            continue

        # Namespace collision check: if this name also matches a declared
        # parameter, it is ambiguous — fail loud.
        if lower_name in param_lower:
            raise ParameterError(
                f"'{name}' matches both a model parameter and a named list. "
                f"Rename one to avoid ambiguity."
            )

        # MDX-type set referenced on the SQL path — specific 400.
        #
        # Bug-9219: every non-``sql_fixed`` list type (``advanced_mdx``,
        # ``dynamic_top_n``, ``filtered``) stores an MDX EXPRESSION
        # (``TopCount(...)`` / ``Filter(...)``) and no member list, so there is
        # literally nothing for the SQL path to expand — evaluating one needs an
        # MDX engine the SQL path does not have. The refusal is correct; what
        # was wrong is that it named only the XMLA escape hatch and never the
        # in-product fix, so an analyst hitting it in the SPA Query Panel had no
        # next step. Both routes forward are now stated. The machine-readable
        # form of this same verdict is ``sql_usable=false`` /
        # ``unusable_reason="mdx_only"`` on the deployed named-object catalogue,
        # which lets a client avoid the error entirely.
        if nlist.list_type != "sql_fixed":
            raise ParameterError(
                f"'{nlist.name}' is an MDX named set ({nlist.list_type}), so a "
                f"SQL query cannot expand it: it stores an MDX expression, not "
                f"a list of members. Either query it from a BI tool over XMLA, "
                f"or open the model builder and republish it as a SQL list "
                f"(fixed members or top-N), then redeploy the model."
            )

        # Empty list — dynamic types may be empty if never refreshed.
        if not nlist.members:
            if nlist.builder_type in ("topN", "filter", "sql_query"):
                # Bug-7941: direct users to Refresh AND redeploy — refreshed
                # members only reach queries after a model redeploy.
                raise ParameterError(
                    f"Named list '{nlist.name}' has no members. "
                    f"Open the model builder, click Refresh to compute "
                    f"members from the source data, then redeploy the model."
                )
            raise ParameterError(
                f"Named list '{nlist.name}' has no members. "
                f"Add at least one member before using it in a query."
            )

        # Size cap — defense in depth (also enforced at create time).
        if len(nlist.members) > max_members:
            raise ParameterError(
                f"Named list '{nlist.name}' has {len(nlist.members)} members, "
                f"exceeding the maximum of {max_members}."
            )

        # Usage-shape whitelist: must appear inside IN (...) or NOT IN (...).
        if not _check_in_context(sql, start, dialect):
            raise ParameterError(
                f"Named list '{nlist.name}' can only be used inside "
                f"IN (@{nlist.name}) or NOT IN (@{nlist.name}). "
                f"Other positions (=, bare reference) are not supported."
            )

        expansions.append((start, end, name, nlist))

    # Expand right-to-left so earlier byte offsets stay valid.
    # Audit entries are appended in reverse order, then reversed at the end
    # so the log reads in query order (left to right).
    out = sql
    for start, end, name, nlist in sorted(expansions, key=lambda s: s[0], reverse=True):
        rendered = _render_members(nlist.members, nlist.data_type, dialect, nlist.name)
        out = out[:start] + rendered + out[end:]
        audit.append(f"{nlist.name}({len(nlist.members)} members)")

    audit.reverse()
    return out, audit
