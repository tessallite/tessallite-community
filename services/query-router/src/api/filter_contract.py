"""Canonical filter-operator contract for the JSON query APIs.

This module is the SINGLE source of truth for the filter vocabulary
accepted by every JSON query surface (``/api/v1/plugin/execute``,
``/api/v1/headless/query``) and emitted by every JSON client (Excel
add-in Report Builder — batch B11 adopts this contract client-side).

Wire shape (one filter object)::

    {
      "dimension": "<semantic dimension name>",   # required
      "operator":  "<operator>",                  # default "eq"
      "value":     <scalar>,                      # scalar operators
      "values":    [<v1>, <v2>, ...]              # list operators
    }

Canonical operators (must match the rewriter vocabulary in
``src/rewrite/conditions.py::_render_condition`` and the persona
default-filter vocabulary in ``persona_gate._SUPPORTED_OPERATORS``):

    ==============  =======  ===========================================
    operator        payload  semantics
    ==============  =======  ===========================================
    eq              scalar   equal
    neq             scalar   not equal
    gt gte lt lte   scalar   numeric / lexicographic comparison
    like            scalar   SQL LIKE — caller supplies the pattern
                             (wildcards ``%`` / ``_`` are NOT escaped)
    not_like        scalar   SQL NOT LIKE — caller supplies the pattern
    in              list     member of (>= 1 value required)
    not_in          list     not member of (>= 1 value required)
    between         list     inclusive range (exactly 2 values required)
    is_null         none     IS NULL
    is_not_null     none     IS NOT NULL
    ==============  =======  ===========================================

Accepted aliases, normalized server-side (Cube-style vocabulary used by
the Excel add-in Report Builder):

    ne          -> neq
    equals      -> eq
    notEquals   -> neq
    set         -> in
    inDateRange -> between
    contains    -> like, value wrapped as ``%<value>%``; any percent,
                   underscore or backslash INSIDE the user's value is
                   backslash-escaped so literal text matches literally
                   (``like`` is the raw pattern-passthrough operator for
                   callers who want wildcards)
    notContains -> not_like, value wrapped as ``%<value>%`` with the same
                   wildcard escaping as ``contains`` ("Not Contains").

Scalar operators accept the value in either ``value`` or ``values[0]``
(the add-in only sends ``values``); a scalar operator with neither is a
422 — use ``is_null`` / ``is_not_null`` for null checks.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from src.ir.logical_query import LogicalFilter

# Canonical vocabulary — mirrors src/rewrite/conditions.py::_render_condition.
CANONICAL_OPERATORS: frozenset[str] = frozenset({
    "eq", "neq", "gt", "gte", "lt", "lte",
    "in", "not_in", "between", "like", "not_like",
    "is_null", "is_not_null",
})

# Aliases accepted on the wire and normalized before binding.
# "contains"/"notContains" additionally wrap the value in %...% (see
# _scalar_value).
OPERATOR_ALIASES: dict[str, str] = {
    "ne": "neq",
    "equals": "eq",
    "notEquals": "neq",
    "set": "in",
    "inDateRange": "between",
    "contains": "like",
    "notContains": "not_like",
}

ACCEPTED_OPERATORS: list[str] = sorted(CANONICAL_OPERATORS | set(OPERATOR_ALIASES))

_SCALAR_OPERATORS = frozenset({"eq", "neq", "gt", "gte", "lt", "lte", "like", "not_like"})
_LIST_OPERATORS = frozenset({"in", "not_in"})
_NULL_OPERATORS = frozenset({"is_null", "is_not_null"})


class SemanticFilter(BaseModel):
    """One filter predicate in the canonical JSON query contract."""

    dimension: str = Field(description="Semantic dimension name to filter on.")
    operator: str = Field(
        default="eq",
        description=(
            "Filter operator. Canonical: "
            + ", ".join(sorted(CANONICAL_OPERATORS))
            + ". Accepted aliases (normalized server-side): "
            + ", ".join(sorted(OPERATOR_ALIASES))
            + "."
        ),
        json_schema_extra={"enum": ACCEPTED_OPERATORS},
    )
    value: Any = Field(
        default=None,
        description="Scalar payload for eq/neq/gt/gte/lt/lte/like.",
    )
    values: list[Any] = Field(
        default_factory=list,
        description=(
            "List payload for in/not_in (>= 1 value) and between "
            "(exactly 2 values). For scalar operators values[0] is "
            "accepted as a fallback when value is absent."
        ),
    )


def _reject(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=detail,
    )


def _scalar_value(f: SemanticFilter, op: str, raw_op: str) -> Any:
    """Resolve the scalar payload: ``value`` first, then ``values[0]``."""
    value = f.value if f.value is not None else (f.values[0] if f.values else None)
    if value is None:
        raise _reject(
            f"Filter operator {raw_op!r} on {f.dimension!r} requires a value "
            "(use 'is_null' / 'is_not_null' for null checks)."
        )
    if raw_op in ("contains", "notContains"):
        # "contains"/"notContains" carry end-user TEXT (not a pattern): escape
        # LIKE wildcards inside the value so e.g. "100%" matches the literal
        # string "100%" instead of any string starting with "100".
        # Backslash is PostgreSQL's default LIKE escape character (the
        # primary execution dialect of this stack); callers who want raw
        # wildcard patterns use the "like" / raw-pattern operators instead.
        escaped = (
            str(value)
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        return f"%{escaped}%"
    return value


def build_logical_filters(filters: list[SemanticFilter]) -> list[LogicalFilter]:
    """Validate and translate wire filters into ``LogicalFilter`` objects.

    Fail-loud contract: unknown operators, missing scalar values, empty
    in/not_in lists, and non-2-element between ranges are all 422 —
    never a silently degenerate predicate (F-027-03 / F-027-10).
    """
    result: list[LogicalFilter] = []
    for f in filters:
        raw_op = f.operator
        op = OPERATOR_ALIASES.get(raw_op, raw_op)
        if op not in CANONICAL_OPERATORS:
            raise _reject(
                f"Unsupported filter operator: {raw_op!r}. "
                f"Accepted operators: {', '.join(ACCEPTED_OPERATORS)}."
            )
        value: Any
        if op in _LIST_OPERATORS:
            value = f.values if f.values else ([f.value] if f.value is not None else [])
            if not value:
                raise _reject(
                    f"Filter operator {raw_op!r} requires at least one value"
                )
        elif op == "between":
            bounds = f.values if f.values else (
                list(f.value) if isinstance(f.value, (list, tuple)) else []
            )
            if len(bounds) != 2:
                raise _reject(
                    "Filter operator 'between' requires exactly two values"
                )
            value = tuple(bounds)
        elif op in _NULL_OPERATORS:
            value = None
        else:  # scalar
            value = _scalar_value(f, op, raw_op)
        result.append(LogicalFilter(
            dimension_name=f.dimension,
            operator=op,
            value=value,
        ))
    return result


# QueryLog.raw_query is a preview surface (query history in the frontend
# and the MCP get_query_history tool) — cap the canonical representation
# so a pathological filter list cannot bloat the log row.
_RAW_QUERY_MAX_CHARS = 2000

_VALID_ORDER_DIRECTIONS = frozenset({"asc", "desc"})


def normalize_order_by(order_by: list[Any]) -> list[tuple[str, str]]:
    """Validate and normalize semantic JSON ``order_by`` entries.

    F-027-17: both the headless and plugin JSON query surfaces accept the
    same small wire object (``field`` + ``direction``). Keeping direction
    validation here is the single source of truth so one surface cannot
    accept an unsafe or misspelled direction the other rejects (the drift
    that produced F-027-04 / F-027-10). An invalid direction is a 422 —
    only ``asc`` / ``desc`` (case-insensitive) are allowed, so a SQL
    fragment in the direction slot can never reach the rewriter.
    """
    normalized: list[tuple[str, str]] = []
    for item in order_by:
        field = getattr(item, "field", None)
        direction = str(getattr(item, "direction", "asc")).lower()
        if direction not in _VALID_ORDER_DIRECTIONS:
            raise _reject(
                f"Invalid order_by direction: {getattr(item, 'direction', None)!r}. "
                "Must be 'asc' or 'desc'."
            )
        normalized.append((field, direction))
    return normalized


def semantic_fingerprint(
    *,
    model_id: str,
    measures: list[str],
    dimensions: list[str],
    filters: list[SemanticFilter],
    order_by: list[tuple[str, str]] | None = None,
    limit: Any = None,
    offset: Any = None,
) -> str:
    """Stable semantic-query fingerprint shared by the JSON query APIs.

    F-027-17: headless and plugin previously each hand-rolled an identical
    SHA-256 over ``{model_id, measures, dimensions, filters}``. Centralising
    it here removes the duplication that let the two surfaces drift.

    Paging / sort controls (``order_by`` / ``limit`` / ``offset``) are
    OPTIONAL and omitted by default: the QueryLog / miss-log dedup
    fingerprint must stay page-INDEPENDENT so pages of one query group
    together (F-027-15). Callers that need a page-aware identity pass the
    paging args explicitly (or layer them on top, as ``_compute_page_id``
    does).
    """
    payload: dict[str, Any] = {
        "model_id": model_id,
        "measures": sorted(measures),
        "dimensions": sorted(dimensions),
        "filters": [
            {"d": f.dimension, "op": f.operator, "v": f.value, "vs": f.values}
            for f in filters
        ],
    }
    if order_by:
        payload["order_by"] = order_by
    if limit is not None:
        payload["limit"] = limit
    if offset:
        payload["offset"] = offset
    text = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def canonical_raw_query(
    *,
    measures: list[str],
    dimensions: list[str],
    filters: list[SemanticFilter],
    order_by: list[tuple[str, str]] | None = None,
    limit: Any = None,
    offset: Any = None,
) -> str:
    """Compact canonical representation of a JSON semantic query.

    B10 round-1 finding 3: headless/plugin queries logged
    ``raw_query=""`` so query history showed blank previews — an admin
    could not see WHAT was asked. This renders the request's semantic
    payload as one compact JSON object (truncated to a sane size; filter
    values are user data of the same sensitivity class as the SQL text
    JDBC queries already log).
    """
    payload = {
        "measures": measures,
        "dimensions": dimensions,
        "filters": [
            {"dimension": f.dimension, "operator": f.operator,
             **({"value": f.value} if f.value is not None else {}),
             **({"values": f.values} if f.values else {})}
            for f in filters
        ],
    }
    if order_by:
        payload["order_by"] = [list(o) for o in order_by]
    if limit is not None:
        payload["limit"] = limit
    if offset:
        payload["offset"] = offset
    text = json.dumps(payload, default=str, separators=(",", ":"))
    if len(text) > _RAW_QUERY_MAX_CHARS:
        text = text[:_RAW_QUERY_MAX_CHARS - 3] + "..."
    return text


__all__ = [
    "ACCEPTED_OPERATORS",
    "CANONICAL_OPERATORS",
    "OPERATOR_ALIASES",
    "SemanticFilter",
    "build_logical_filters",
    "canonical_raw_query",
    "normalize_order_by",
    "semantic_fingerprint",
]
