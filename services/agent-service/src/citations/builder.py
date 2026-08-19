"""Citation chip data emission per spec §5 / B2.3.

After a query returns rows, we emit one citation per resolved measure /
dimension involved in the answer. Stable ids (per H5) come from the
Measure/Dimension UUIDs in tess; display name is the canonical name the
binder accepted. Each citation may carry a `value` (the measure value
from the first row) so the chip can render the headline figure inline.

Bug-8181 (checkable citations) / Bug-8370 lane L3 — a citation chip that only
carries kind/id/name/value is a semantic label, not a *checkable* citation: the
external review (F-104-04) found a user "cannot use them to verify the metric
definition ... or the exact supporting slice." Each citation now also carries:

- ``definition``: the measure/dimension's business definition (description,
  falling back to its formula/expression when no description was authored),
  so the provenance dialog can answer "what does this field mean" without a
  second lookup.
- ``route_type``: the route (aggregate | pocket | source) that served the
  value, threaded through from ``QueryExecution.route_type`` by the caller.
- ``filter_grain``: a human-readable summary of the WHERE filters and GROUP
  BY grain that produced the value (``describe_filter_grain`` below), so the
  citation states the exact supporting slice rather than a bare number.

``filter_grain`` is generated, data-dependent prose describing THIS query
(e.g. "Filtered by country = US · Grouped by month") — the same class of
server-generated content as narration text or a refusal message, not static
UI chrome. It is intentionally plain English (v1 is English-only product-wide,
see architecture_conversational-agent.md §"Out of scope") and is NOT an i18n
UI key; the surrounding dialog LABELS ("Definition", "Filters and grouping
applied", ...) are the i18n-governed strings, added to shared-chat.json.

Round-2 fix (F-L3-R1-02): the first cut only described the legacy flat
``{name, op, value}`` WHERE shape. Structured predicates
(``QueryToolCall.where_refs`` — function-on-column, column-to-column, OR/NOT,
ratio-of-aggregates) are an equally supported production input
(``tools/spec.py``); silently omitting them let a genuinely filtered query
render as "unfiltered" in the provenance dialog — a false statement about
the business, not merely an incomplete one. ``describe_filter_grain`` now
walks the SAME typed AST the query executes from (``tools/expressions.py``)
with a human-readable formatter (never raw SQL — ``render``/``render_predicate``
quote identifiers and emit SQL keywords, which is a technical detail, not a
consumer-facing summary), and both HAVING clauses now widen the described
slice for the same reason a WHERE clause does. The one invariant that must
never break: if a WHERE/HAVING entry EXISTS (flat or structured) but this
formatter cannot describe it, the result must say so explicitly rather than
silently reading as "no filter."
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Dimension, Measure
from src.tools.expressions import (
    Arith,
    BoolOp,
    Case,
    Comparison,
    ExprNode,
    FieldRef,
    FuncCall,
    Literal,
    NotPred,
    PredNode,
    PredRef,
)

# Symbols for the flat {name, op, value} WHERE shape built by exec/query.py's
# build_sql (op defaults to "eq"), and reused below for the structured
# Comparison.op vocabulary (both use the same op names). Kept local to this
# module rather than imported from exec/query.py: that module's
# `_filter_to_sql` renders SQL fragments (quoted identifiers, SQL literals);
# this renders a short English phrase for a human. Same vocabulary, different
# job — importing one into the other would couple a UI-facing formatter to a
# SQL-quoting internal.
_FILTER_OP_SYMBOLS: dict[str, str] = {
    "eq": "=",
    "neq": "≠",
    "gt": ">",
    "gte": "≥",
    "lt": "<",
    "lte": "≤",
    "like": "like",
}

_ARITH_SYMBOLS: dict[str, str] = {"add": "+", "sub": "-", "mul": "*", "div": "/"}

# Bug-8181 (F-L3-R1-02) — the safety net. Any WHERE/HAVING entry that exists
# but that this formatter cannot describe (a malformed flat entry, or,
# defensively, a future/unrecognised structured node shape) renders as this
# marker rather than being dropped, so the citation NEVER reads as
# "unfiltered" when a filter genuinely applies. "view trace" points the user
# at the routed SQL / semantic query, which always has the exact predicate.
UNDESCRIBED_FILTER_TEXT = "additional filters applied — view trace"


def _describe_filter(f: dict[str, Any]) -> str | None:
    """Render one flat ``{name, op, value}`` WHERE/HAVING entry as a short
    phrase. Returns ``None`` for an entry with no usable field name (a
    malformed entry, not a describable filter)."""
    name = f.get("name")
    if not isinstance(name, str) or not name:
        return None
    op = str(f.get("op") or "eq").lower()
    value = f.get("value")
    if op == "is_null":
        return f"{name} is null"
    if op == "is_not_null":
        return f"{name} is not null"
    if op == "between" and isinstance(value, (list, tuple)) and len(value) == 2:
        return f"{name} between {value[0]} and {value[1]}"
    if op == "in" and isinstance(value, list) and value:
        return f"{name} in ({', '.join(str(v) for v in value)})"
    symbol = _FILTER_OP_SYMBOLS.get(op, op)
    return f"{name} {symbol} {value}"


def _describe_literal(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class _UndescribableNode(Exception):
    """Internal signal only — never escapes this module. Raised when a
    structured expression/predicate node is a shape ``_describe_expr``/
    ``_describe_predicate`` does not recognise (Python's ``Union`` alias
    for ``ExprNode``/``PredNode`` is not enforced at runtime, so a future
    node type or a construction bug could reach here). ``_describe_clause``
    catches this and falls the WHOLE clause back to
    ``UNDESCRIBED_FILTER_TEXT`` — never a partial description that silently
    drops the one predicate it could not name, and never ``None`` (which
    would read as "no filter" for a clause that demonstrably has one)."""


def _describe_expr(node: ExprNode) -> str:
    """Render a structured scalar expression node as human-readable text —
    NEVER SQL (no quoted identifiers, no SQL keywords): this is a
    consumer-facing citation summary, not a technical trace."""
    if isinstance(node, FieldRef):
        return node.name
    if isinstance(node, Literal):
        return _describe_literal(node.value)
    if isinstance(node, FuncCall):
        args = ", ".join(_describe_expr(a) for a in node.args)
        return f"{node.fn}({args})"
    if isinstance(node, Arith):
        symbol = _ARITH_SYMBOLS.get(node.op, node.op)
        return f"({_describe_expr(node.left)} {symbol} {_describe_expr(node.right)})"
    if isinstance(node, Case):
        return "a computed expression"
    raise _UndescribableNode(f"unrecognised expression node: {node!r}")


def _describe_predicate(node: PredNode) -> str:
    """Render a structured WHERE/HAVING predicate node as human-readable
    text. Same contract as ``_describe_expr`` — raises ``_UndescribableNode``
    rather than guessing at an unrecognised shape."""
    if isinstance(node, BoolOp):
        joiner = " and " if node.op == "and" else " or "
        return "(" + joiner.join(_describe_predicate(a) for a in node.args) + ")"
    if isinstance(node, NotPred):
        return f"not ({_describe_predicate(node.arg)})"
    if isinstance(node, Comparison):
        left = _describe_expr(node.left)
        op = node.op
        if op == "is_null":
            return f"{left} is null"
        if op == "is_not_null":
            return f"{left} is not null"
        if op == "in":
            items = ", ".join(_describe_expr(v) for v in (node.right or ()))
            return f"{left} in ({items})"
        if op == "between":
            lo, hi = node.right
            return f"{left} between {_describe_expr(lo)} and {_describe_expr(hi)}"
        symbol = _FILTER_OP_SYMBOLS.get(op, op)
        return f"{left} {symbol} {_describe_expr(node.right)}"
    raise _UndescribableNode(f"unrecognised predicate node: {node!r}")


def _describe_clause(
    flat: list[dict[str, Any]] | None,
    structured: list[PredRef] | None,
) -> str | None:
    """Describe one clause (WHERE or HAVING): every flat entry plus every
    structured predicate, joined with "; ". Returns ``None`` only when the
    clause has NO entries at all (flat or structured) — never when entries
    exist but some could not be described (see ``UNDESCRIBED_FILTER_TEXT``).
    If ANY structured predicate cannot be described, the WHOLE clause falls
    back to the explicit marker rather than a partial description that
    silently drops the one it could not name."""
    had_entry = bool(flat) or bool(structured)
    phrases: list[str] = []

    for f in flat or []:
        d = _describe_filter(f)
        if d:
            phrases.append(d)

    for ref in structured or []:
        try:
            phrases.append(_describe_predicate(ref.node))
        except _UndescribableNode:
            return UNDESCRIBED_FILTER_TEXT

    if phrases:
        return "; ".join(phrases)
    if had_entry:
        return UNDESCRIBED_FILTER_TEXT
    return None


def describe_filter_grain(
    where: list[dict[str, Any]] | None,
    dimensions: list[str] | None,
    *,
    where_refs: list[PredRef] | None = None,
    having: list[dict[str, Any]] | None = None,
    having_refs: list[PredRef] | None = None,
) -> str | None:
    """Human-readable summary of the WHERE/HAVING filters and the GROUP BY
    grain that produced a citation's value (Bug-8181 — "the exact supporting
    slice"). Returns ``None`` only when the query had no filter, no HAVING,
    and no grain at all (a genuinely unfiltered, ungrouped total) — a query
    that WAS filtered always says so, even via the ``UNDESCRIBED_FILTER_TEXT``
    fallback for a shape this formatter cannot fully describe.

    Both the legacy flat ``{name, op, value}`` shape and the structured
    predicate AST (``where_refs``/``having_refs`` — function-on-column,
    column-to-column, OR/NOT, ratio-of-aggregates) are described. HAVING is
    included because it narrows the same result rows a WHERE clause narrows
    (e.g. ``HAVING SUM(amount) > 1000`` is as much "the exact supporting
    slice" as a WHERE predicate)."""
    parts: list[str] = []

    where_desc = _describe_clause(where, where_refs)
    if where_desc:
        parts.append("Filtered by " + where_desc)

    having_desc = _describe_clause(having, having_refs)
    if having_desc:
        parts.append("Having " + having_desc)

    if dimensions:
        parts.append("Grouped by " + ", ".join(dimensions))

    return " · ".join(parts) if parts else None


def _measure_definition(m: Measure) -> str | None:
    return m.description or m.expression or None


def _dimension_definition(d: Dimension) -> str | None:
    return d.description or d.calc_expression or None


async def build_citations(
    db: AsyncSession,
    model_id: UUID,
    measure_names: list[str],
    dimension_names: list[str],
    rows: list[dict[str, Any]],
    *,
    route_type: str | None = None,
    filter_grain: str | None = None,
) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []

    if measure_names:
        m_q = await db.execute(
            select(Measure).where(
                Measure.model_id == model_id,
                Measure.name.in_(measure_names),
            )
        )
        first_row = rows[0] if rows else {}
        for m in m_q.scalars().all():
            citations.append(
                {
                    "kind": "measure",
                    "id": str(m.id),
                    "name": m.name,
                    "display_name": m.display_name or m.name,
                    "value": first_row.get(m.name),
                    "definition": _measure_definition(m),
                    "route_type": route_type,
                    "filter_grain": filter_grain,
                }
            )

    if dimension_names:
        d_q = await db.execute(
            select(Dimension).where(
                Dimension.model_id == model_id,
                Dimension.name.in_(dimension_names),
            )
        )
        for d in d_q.scalars().all():
            citations.append(
                {
                    "kind": "dimension",
                    "id": str(d.id),
                    "name": d.name,
                    "display_name": d.display_name or d.name,
                    "value": None,
                    "definition": _dimension_definition(d),
                    "route_type": route_type,
                    "filter_grain": filter_grain,
                }
            )

    return citations
