"""Shared sqlglot dialect patches for gaps in sqlglot 30.x."""
from __future__ import annotations

from sqlglot import exp
from sqlglot.dialects.bigquery import BigQuery as _BigQueryDialect


def _bq_splitpart_sql(
    self: _BigQueryDialect.Generator, expression: exp.SplitPart
) -> str:
    """SPLIT_PART(s, d, n) -> SPLIT(s, d)[OFFSET(n - 1)]"""
    s = self.sql(expression, "this")
    d = self.sql(expression, "delimiter")
    idx = expression.args.get("part_index")
    if isinstance(idx, exp.Literal) and idx.is_int:
        offset = str(int(idx.this) - 1)
    else:
        offset = f"{self.sql(idx)} - 1"
    return f"SPLIT({s}, {d})[OFFSET({offset})]"


def _bq_within_group_sql(
    self: _BigQueryDialect.Generator, expression: exp.WithinGroup
) -> str:
    """Bug-982: ``PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY col)`` ->
    ``APPROX_QUANTILES(col, 100)[OFFSET(ROUND(p * 100))]`` for BigQuery.

    BigQuery's ``PERCENTILE_CONT`` / ``PERCENTILE_DISC`` are analytic-only
    (they require an ``OVER()`` clause) and are therefore invalid inside a
    GROUP BY aggregate query — sqlglot's default ``WITHIN GROUP`` rendering
    (``PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY col)``) errors on real
    BigQuery. The supported aggregate quantile is ``APPROX_QUANTILES(col, N)``
    which returns the N+1 ascending quantile boundaries; the requested
    percentile is the boundary at ``ROUND(p * 100)``. This is approximate
    (exact GROUP-BY quantiles are not available on BigQuery) but valid SQL.

    ORDER direction: ``APPROX_QUANTILES`` always sorts ASCENDING, so a
    ``WITHIN GROUP (ORDER BY col DESC)`` requests the percentile counted from
    the top — i.e. the ascending boundary at ``100 - ROUND(p * 100)``. The
    offset is inverted for a DESC order so e.g. the 90th percentile DESC maps
    to ascending OFFSET(10).

    Rounding: BigQuery's ``ROUND`` is half-up; a folded literal offset uses
    Decimal ``ROUND_HALF_UP`` to match (Python ``round`` is banker's rounding
    and disagrees on ``.5`` ties, e.g. ``ROUND(12.5)`` = 13 on BigQuery but 12
    in Python). For canonical pNN percentiles ``p * 100`` is already integral,
    so this only matters for arbitrary hand-written fractions.

    Falls through to the default ``WITHIN GROUP`` rendering for any non-
    percentile aggregate (e.g. ``STRING_AGG ... WITHIN GROUP``).
    """
    inner = expression.this
    if not isinstance(inner, (exp.PercentileCont, exp.PercentileDisc)):
        return self.withingroup_sql(expression)

    fraction_node = inner.this
    order = expression.args.get("expression")
    ordered = None
    if isinstance(order, exp.Order):
        exprs = order.expressions
        if exprs:
            ordered = exprs[0]
    if ordered is None:
        # Cannot identify the ordered column — defer to default rendering.
        return self.withingroup_sql(expression)

    is_desc = bool(isinstance(ordered, exp.Ordered) and ordered.args.get("desc"))
    col_node = ordered.this if isinstance(ordered, exp.Ordered) else ordered
    col_sql = self.sql(col_node)

    # Resolve the OFFSET index. ASCENDING => ROUND(p * 100); DESCENDING =>
    # 100 - ROUND(p * 100) (APPROX_QUANTILES always sorts ascending). A literal
    # fraction folds to a constant (half-up to match BigQuery ROUND); a
    # non-literal fraction stays a runtime ROUND(... * 100) expression so the
    # construct remains valid for parameterised percentiles.
    if isinstance(fraction_node, exp.Literal) and not fraction_node.is_string:
        try:
            from decimal import Decimal, ROUND_HALF_UP

            idx = int(
                (Decimal(str(fraction_node.this)) * 100).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            offset = str(100 - idx if is_desc else idx)
        except (TypeError, ValueError, ArithmeticError):
            offset = _bq_round_offset_expr(self.sql(fraction_node), is_desc)
    else:
        offset = _bq_round_offset_expr(self.sql(fraction_node), is_desc)

    return f"APPROX_QUANTILES({col_sql}, 100)[OFFSET({offset})]"


def _bq_round_offset_expr(fraction_sql: str, is_desc: bool) -> str:
    """Runtime OFFSET expression for a non-literal percentile fraction:
    ``ROUND(p * 100)`` ascending, ``100 - ROUND(p * 100)`` descending."""
    rounded = f"CAST(ROUND({fraction_sql} * 100) AS INT64)"
    return f"100 - {rounded}" if is_desc else rounded


def register_bigquery_patches() -> None:
    """Register all BigQuery dialect patches. Safe to call multiple times."""
    if exp.SplitPart not in _BigQueryDialect.Generator.TRANSFORMS:
        _BigQueryDialect.Generator.TRANSFORMS[exp.SplitPart] = _bq_splitpart_sql
    if exp.WithinGroup not in _BigQueryDialect.Generator.TRANSFORMS:
        _BigQueryDialect.Generator.TRANSFORMS[exp.WithinGroup] = _bq_within_group_sql
