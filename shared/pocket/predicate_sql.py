"""Shared pocket predicate -> SQL clause rendering (Bug-5897 / F-005-03).

Two writers turn a canonical predicate dict (``column_name`` / ``operator`` /
``value``) into a pocket ``WHERE`` clause fragment: the optimizer's
auto-create renderer (``optimizer/src/lifecycle/pocket_creator.py``) and its
suggestion-preview renderer (``optimizer/src/advisor/pocket_suggester.py``).
Before this module existed the two renderers carried independent, narrower
operator vocabularies than the canonical filter contract
(``query-router/src/api/filter_contract.py::CANONICAL_OPERATORS``), and the
suggestion renderer silently dropped any predicate it could not render
instead of failing loud — so a suggestion could display a narrow slice
(e.g. ``status not_in (...)``) while the SQL handed to "Create" covered a
broader one.

This module is the single source of truth for that rendering so both
writers stay in lockstep with the canonical operator vocabulary and cannot
drift apart again. An operator this module cannot render raises
``UnsupportedPocketOperatorError`` — callers must not swallow that into an
empty/missing clause.
"""
from __future__ import annotations

from typing import Any

from shared.connector_qualify import safe_ident

# Mirrors query-router's api/filter_contract.py::CANONICAL_OPERATORS. Kept as
# an explicit local copy (not imported) because optimizer does not depend on
# query-router at runtime; the vocabulary is duplicated intentionally and
# should be updated in lockstep if the canonical contract changes.
CANONICAL_POCKET_OPERATORS: frozenset[str] = frozenset({
    "eq", "neq", "gt", "gte", "lt", "lte",
    "in", "not_in", "between", "like", "not_like",
    "is_null", "is_not_null",
})

_SCALAR_OPERATOR_SQL: dict[str, str] = {
    "eq": "=",
    "neq": "<>",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}


class UnsupportedPocketOperatorError(ValueError):
    """Raised when a predicate cannot be rendered as pocket SQL.

    Callers must propagate this (or convert it to their own domain error)
    rather than treating it as "render nothing" — an omitted clause
    silently broadens the cached slice past what was validated/shown to
    the user (Bug-5897).
    """


def render_predicate_clause(column_name: object, operator: object, value: Any) -> str:
    """Render one predicate as a pocket SQL ``WHERE``-clause fragment.

    Supports the full canonical operator vocabulary. Raises
    ``UnsupportedPocketOperatorError`` for anything outside it, for an
    empty column name, or for a structurally invalid value (empty
    in/not_in list, non-2-element between range).
    """
    col = str(column_name or "").strip()
    if not col:
        raise UnsupportedPocketOperatorError("Pocket predicate has an empty column name")
    quoted_col = safe_ident(col)
    op = str(operator or "eq").strip().lower()

    if op not in CANONICAL_POCKET_OPERATORS:
        raise UnsupportedPocketOperatorError(
            f"Pocket predicate {col} uses unsupported operator {operator!r}"
        )

    if op in ("in", "not_in"):
        values = list(value) if isinstance(value, (list, tuple)) else (
            [value] if value is not None else []
        )
        if not values:
            raise UnsupportedPocketOperatorError(
                f"Pocket predicate {col} has an empty {op.upper()} value list"
            )
        sql_op = "NOT IN" if op == "not_in" else "IN"
        return f"{quoted_col} {sql_op} ({', '.join(_literal(v) for v in values)})"

    if op == "between":
        values = list(value) if isinstance(value, (list, tuple)) else []
        if len(values) != 2:
            raise UnsupportedPocketOperatorError(
                f"Pocket predicate {col} has an invalid BETWEEN value"
            )
        return f"{quoted_col} BETWEEN {_literal(values[0])} AND {_literal(values[1])}"

    if op in ("like", "not_like"):
        if value is None:
            raise UnsupportedPocketOperatorError(
                f"Pocket predicate {col} operator {op!r} requires a value"
            )
        sql_op = "NOT LIKE" if op == "not_like" else "LIKE"
        return f"{quoted_col} {sql_op} {_literal(value)}"

    if op in ("is_null", "is_not_null"):
        return f"{quoted_col} IS {'NOT ' if op == 'is_not_null' else ''}NULL"

    # Remaining canonical operators are the scalar comparison set.
    sql_op = _SCALAR_OPERATOR_SQL[op]
    if value is None and sql_op in ("=", "<>"):
        return f"{quoted_col} IS {'NOT ' if sql_op == '<>' else ''}NULL"
    return f"{quoted_col} {sql_op} {_literal(value)}"


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"
