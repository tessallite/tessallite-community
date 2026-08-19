"""Calculated-measure column rendering helpers shared across services.

Originally lived in optimizer/src/ddl/_calculated_columns.py. Moved to shared
so the scheduler can render calculated-measure expressions during aggregate
refresh without importing across service boundaries.

A calculated measure carries a SQL-flavoured expression (parsed by
:func:`shared.semantic.calculated_expression.parse_expression`) and a
``calc_agg_mode`` that decides how it is materialised:

* ``per_row_then_aggregate`` — substitute each ``measure("x")`` reference
  with the raw source column, then wrap the full expression in the
  calculated measure's own ``default_agg`` (e.g. ``SUM(price * qty)``).
* ``expression_as_written`` — substitute each ``measure("x")`` reference
  with its aggregated form (e.g. ``SUM("revenue") / SUM("cost")``). The
  materialised value is not re-aggregatable; aggregate matcher requires
  exact grain match.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import sqlglot
from sqlglot import exp

from shared.semantic.calculated_expression import (
    ExpressionValidationError,
    expand_safe_helpers,
    parse_expression,
)


_DIALECT_TO_SQLGLOT: dict[str, str] = {
    "postgresql": "postgres",
    "bigquery": "bigquery",
    "hadoop_spark": "spark",
    "snowflake": "snowflake",
}


def is_calculated(measure: Any) -> bool:
    return getattr(measure, "measure_type", None) == "calculated"


def render_calculated_column_sql(
    measure: Any,
    *,
    dialect: str,
    ref_measures_by_name: dict[str, Any],
    source_column_names: dict[UUID, str],
    identifier_quote: str = '"',
    table_aliases: dict | None = None,
    source_table_ids: dict[UUID, UUID] | None = None,
) -> str:
    """Render the SELECT-list expression for a calculated measure column.

    Parameters
    ----------
    measure
        The calculated Measure ORM row being materialised.
    dialect
        Dialect token (``postgresql``, ``bigquery``, ``hadoop_spark``).
    ref_measures_by_name
        Mapping of referenced measure name → Measure ORM row.
    source_column_names
        Mapping of referenced measure's ``id`` → its physical source column name.
    table_aliases
        Optional ``source_table_id → alias`` mapping for qualified references.
    source_table_ids
        Optional ``measure_id → source_table_id`` mapping for alias qualification.

    Returns
    -------
    str
        The physical SQL expression (no trailing ``AS``).

    Raises
    ------
    ValueError
        When the expression references an unknown / calculated / variant measure,
        when a per-row reference is non-additive, or when outer count_distinct
        would operate on a COUNT reference's 0/1 row contributions.
    """
    del identifier_quote  # canonical-Postgres input always; sqlglot transpiles
    expression = getattr(measure, "expression", None) or ""
    try:
        parsed = parse_expression(expression)
    except ExpressionValidationError as exc:
        raise ValueError(
            f"calculated measure {measure.name!r} has an invalid expression: {exc}"
        ) from exc

    mode = getattr(measure, "calc_agg_mode", None) or "expression_as_written"
    outer_agg = (
        (getattr(measure, "default_agg", None) or "sum").upper()
        if mode == "per_row_then_aggregate"
        else None
    )

    replacements: dict[str, str] = {}
    for ref in parsed.references:
        ref_meas = ref_measures_by_name.get(ref.name)
        if ref_meas is None:
            raise ValueError(
                f"calculated measure {measure.name!r} references unknown "
                f"measure {ref.name!r}"
            )
        if is_calculated(ref_meas):
            raise ValueError(
                f"calculated measure {measure.name!r} cannot be materialised: "
                f"it references another calculated measure {ref.name!r} "
                f"(nested calculation — v2 feature)"
            )
        # Reject variant measures (check variant_kind without importing _variant_columns)
        if getattr(ref_meas, "variant_kind", None) is not None:
            raise ValueError(
                f"calculated measure {measure.name!r} cannot be materialised: "
                f"it references variant measure {ref.name!r} "
                f"(nested variant — out of scope for CTAS)"
            )
        # Bug-7183: reject semi-additive measures — their aggregation
        # semantics (LAST/FIRST_NON_EMPTY) cannot be expressed as a
        # plain AGG(col) in a CTAS SELECT.  The previous code silently
        # emitted the raw default_agg tag (e.g. "LAST_NON_EMPTY(col)")
        # which is not valid SQL, or fell back to SUM — wrong numbers.
        _ref_sa_behavior = getattr(ref_meas, "semi_additive_behavior", None)
        if _ref_sa_behavior:
            raise ValueError(
                f"calculated measure {measure.name!r} cannot be materialised: "
                f"it references semi-additive measure {ref.name!r} "
                f"(behavior: {_ref_sa_behavior}); semi-additive aggregation "
                f"semantics cannot be preserved in a calculated expression"
            )
        ref_id = getattr(ref_meas, "id", None)
        col = source_column_names.get(ref_id)
        if not col:
            raise ValueError(
                f"calculated measure {measure.name!r}: referenced measure "
                f"{ref.name!r} has no resolvable source column"
            )
        ref_table_id = (source_table_ids or {}).get(ref_id)
        if table_aliases and ref_table_id is not None:
            alias = table_aliases.get(ref_table_id)
            quoted_col = f'"{alias}"."{col}"' if alias else f'"{col}"'
        else:
            quoted_col = f'"{col}"'
        if mode == "per_row_then_aggregate":
            # Bug-8257 (deep-review R4 finding 3): gate on what raw-column
            # inlining actually DESTROYS, not on ``is_additive``.
            #
            # In this mode the reference is inlined as a bare column and the
            # whole expression is aggregated once, so the reference's own
            # ``default_agg`` is never used — which is precisely what
            # ``is_additive`` now reports after the Bug-8257 coercion. Gating on
            # the flag therefore started rejecting a perfectly valid reference
            # to an ordinary ``avg``/``min``/``max`` measure the moment the
            # coercion landed (and, since the coercion marks EVERY calculated
            # measure False, a calc-referencing-calc combination unconditionally)
            # — a hard ValueError that aborts aggregate creation and refresh,
            # not merely a lost optimisation.
            #
            # What raw inlining genuinely cannot preserve is a value that is a
            # property of the SET rather than of the ROW:
            #   * COUNT(DISTINCT col) -- the per-row "value" of a distinct count
            #     is not a number at all; inlining a user_id column and summing
            #     it is nonsense, not an approximation;
            #   * a quantile (pNN) -- likewise a statistic over the set;
            # (A semi-additive reduction, a time-variant window and another
            # calculated expression are equally un-inlinable, but each is
            # already refused earlier in this function with its own message.)
            # For sum / avg / min / max the per-row value IS the column. COUNT
            # is the exception: COUNT(column) contributes 1 for a non-NULL row
            # and 0 for a NULL row, never the raw column value (Bug-8834).
            from shared.aggregate_quantiles import is_quantile_agg_token

            _ref_agg = (getattr(ref_meas, "default_agg", None) or "sum").lower()
            _set_statistic = (
                _ref_agg == "count_distinct" or is_quantile_agg_token(_ref_agg)
            )
            # Only the set-statistic leg is live here: a variant, a calculated
            # reference and a semi-additive reference are all already rejected
            # further up this function with more specific messages (deep-review
            # R5 finding 7 -- keeping unreachable conditions would misdescribe
            # where the rule actually lives).
            if _set_statistic:
                raise ValueError(
                    f"calculated measure {measure.name!r} uses "
                    f"per_row_then_aggregate but references non-additive "
                    f"measure {ref.name!r}, whose value is a property of the "
                    f"row set (a distinct count or a quantile) and cannot be "
                    f"evaluated at fact grain; switch to expression_as_written"
                )
            if outer_agg == "COUNT_DISTINCT" and _ref_agg == "count":
                # Bug-8834 gate follow-up: COUNT has a valid row contribution,
                # but COUNT(DISTINCT <that 0/1 contribution>) counts the distinct
                # contribution VALUES rather than preserving the referenced
                # COUNT measure's meaning. Refuse the construct instead of
                # silently materialising a different measure.
                raise ValueError(
                    f"calculated measure {measure.name!r} uses count_distinct "
                    f"with per_row_then_aggregate but references COUNT measure "
                    f"{ref.name!r}; distinct-counting COUNT's per-row 0/1 "
                    f"contributions would change the requested measure semantics; "
                    f"switch to expression_as_written"
                )
            if _ref_agg == "count":
                replacements[ref.placeholder] = (
                    f"CASE WHEN {quoted_col} IS NULL THEN 0 ELSE 1 END"
                )
            else:
                replacements[ref.placeholder] = quoted_col
        else:
            agg = (getattr(ref_meas, "default_agg", None) or "sum").upper()
            if agg == "COUNT_DISTINCT":
                replacements[ref.placeholder] = f"COUNT(DISTINCT {quoted_col})"
            else:
                replacements[ref.placeholder] = f"{agg}({quoted_col})"

    def _substitute(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column) and node.name in replacements:
            return sqlglot.parse_one(replacements[node.name], read="postgres")
        return node

    expanded_ast = parsed.ast.copy().transform(_substitute)
    expanded_ast = expand_safe_helpers(expanded_ast)

    sqlglot_dialect = _DIALECT_TO_SQLGLOT.get(dialect, "postgres")
    try:
        expanded_sql = expanded_ast.sql(dialect=sqlglot_dialect)
    except Exception as exc:
        raise ValueError(
            f"calculated measure {measure.name!r}: failed to render SQL: {exc}"
        ) from exc

    if mode == "per_row_then_aggregate":
        if outer_agg == "COUNT_DISTINCT":
            expanded_sql = f"COUNT(DISTINCT {expanded_sql})"
        else:
            expanded_sql = f"{outer_agg}({expanded_sql})"

    return expanded_sql


def calculated_physical_col_name(measure: Any) -> str:
    """Physical column name for a calculated measure in the CTAS."""
    from shared.semantic.grain_resolver import bound_ident

    mode = getattr(measure, "calc_agg_mode", None) or "expression_as_written"
    if mode == "per_row_then_aggregate":
        agg = (getattr(measure, "default_agg", None) or "sum").lower()
        return bound_ident(f"{measure.name}__{agg}")
    return bound_ident(f"{measure.name}__calculated")


def calculated_stat_type(measure: Any) -> str:
    """``stat_type`` recorded on the ``AggregateColumn`` row."""
    mode = getattr(measure, "calc_agg_mode", None) or "expression_as_written"
    if mode == "per_row_then_aggregate":
        return (getattr(measure, "default_agg", None) or "sum").lower()
    return "calculated"
