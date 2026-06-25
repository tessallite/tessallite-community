"""Validator for calculated dimension expressions.

Parses the expression with sqlglot, extracts column references, validates
them against the model's columns, and checks for disallowed constructs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import sqlglot
from sqlglot import exp


class CalcDimensionValidationError(Exception):
    pass


_ALLOWED_FUNCTIONS = frozenset({
    "coalesce", "nullif", "cast",
    "concat", "upper", "lower", "trim", "substring", "length",
    "abs", "round", "floor", "ceil", "greatest", "least",
    "left", "right", "replace", "lpad", "rpad",
    "date_trunc", "extract", "to_char",
})

_DISALLOWED_NODE_TYPES = (
    exp.Subquery, exp.Select, exp.Insert, exp.Update, exp.Delete,
    exp.Create, exp.Drop, exp.Alter,
)


@dataclass
class ColumnRef:
    table: str | None
    column: str


@dataclass
class ValidationResult:
    expression: str
    column_refs: list[ColumnRef]
    table_ids: list[str]


def validate_calc_expression(
    expression: str,
    model_columns: Sequence | None = None,
    model_tables: Sequence | None = None,
) -> ValidationResult:
    """Validate a calculated dimension expression.

    Args:
        expression: Raw SQL expression (e.g., ``CASE WHEN x > 0 THEN 'A' ELSE 'B' END``)
        model_columns: Optional sequence of model column objects with ``column_name``,
            ``model_table_id`` attributes for reference resolution.
        model_tables: Optional sequence of model table objects with ``id``, ``alias``
            attributes for table alias resolution.

    Returns:
        ValidationResult with extracted column references and referenced table IDs.

    Raises:
        CalcDimensionValidationError: If the expression is syntactically invalid
            or contains disallowed constructs.
    """
    if not expression or not expression.strip():
        raise CalcDimensionValidationError("Expression cannot be empty")

    try:
        tree = sqlglot.parse_one(expression, read="postgres")
    except Exception as exc:
        raise CalcDimensionValidationError(
            f"Invalid SQL syntax: {exc}"
        ) from exc

    for node in tree.walk():
        if isinstance(node, _DISALLOWED_NODE_TYPES):
            raise CalcDimensionValidationError(
                f"Disallowed construct: {type(node).__name__}. "
                "Subqueries and DML statements are not allowed."
            )

        if isinstance(node, exp.Anonymous):
            func_name = node.name.lower()
            if func_name not in _ALLOWED_FUNCTIONS:
                raise CalcDimensionValidationError(
                    f"Disallowed function: {node.name}. "
                    f"Allowed functions: {', '.join(sorted(_ALLOWED_FUNCTIONS))}"
                )

        if isinstance(node, exp.Func) and not isinstance(node, (exp.Anonymous, exp.Case, exp.If, exp.Cast)):
            func_name = type(node).__name__.lower()
            func_sql_name = getattr(node, "sql_name", lambda: func_name)()
            if func_sql_name.lower() not in _ALLOWED_FUNCTIONS:
                raise CalcDimensionValidationError(
                    f"Disallowed function: {func_sql_name}. "
                    f"Allowed functions: {', '.join(sorted(_ALLOWED_FUNCTIONS))}"
                )

    column_refs: list[ColumnRef] = []
    for node in tree.find_all(exp.Column):
        table_ref = node.table if node.table else None
        column_refs.append(ColumnRef(table=table_ref, column=node.name))

    table_ids: list[str] = []
    if model_columns and model_tables:
        table_alias_to_id = {
            getattr(t, "alias", None) or "": str(getattr(t, "id", ""))
            for t in model_tables
        }
        col_lookup = {}
        for mc in model_columns:
            col_name = getattr(mc, "column_name", None)
            table_id = str(getattr(mc, "model_table_id", ""))
            if col_name:
                col_lookup.setdefault(col_name.lower(), []).append(table_id)

        referenced_table_ids: set[str] = set()
        for ref in column_refs:
            if ref.table:
                tid = table_alias_to_id.get(ref.table)
                if tid:
                    referenced_table_ids.add(tid)
            else:
                matches = col_lookup.get(ref.column.lower(), [])
                referenced_table_ids.update(matches)

        table_ids = sorted(referenced_table_ids)

    return ValidationResult(
        expression=expression,
        column_refs=column_refs,
        table_ids=table_ids,
    )
