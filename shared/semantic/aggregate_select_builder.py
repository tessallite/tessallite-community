"""Shared SELECT-list assembly for aggregate DDL (F-012-23).

Before this module the grain/measure/row-count portion of the aggregate
SELECT list was re-implemented four times — once in each scheduler DDL
builder (``postgres_ddl``, ``bigquery_ddl``, ``spark_ddl``) and once in
``incremental_refresh._build_select_parts`` — each with subtly different
skip rules. That divergence was the main drift risk in the refresh
feature: a fix to one copy (e.g. the variant pre-render skip, or the
quantile/stat exclusion) could silently miss the others.

This module owns the single, dialect-parameterised implementation. The
per-dialect DDL wrappers stay (CTAS / CREATE OR REPLACE / STORED AS
PARQUET structure is genuinely connector-specific and is the documented
sqlglot exemption), but they delegate the grain → measure → row-count
emission here.

Byte-identical guarantee: the helpers reproduce exactly what the four
copies emitted. A dialect is described by a small :class:`SelectListDialect`
spec (identifier quote char, the AGG template table, and the alias
separator the grain/measure refs use). Golden tests
(``tests/unit/test_aggregate_select_builder.py`` and the scheduler DDL
suites) assert the assembled SQL is unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Optional
from uuid import UUID

from shared.aggregate_quantiles import is_quantile_stat_type
from shared.aggregate_stats import is_stat_type
from shared.semantic.numeric_scale import cast_for_agg
from shared.semantic.grain_resolver import (
    AGG_TEMPLATES,
    ResolvedAggregateLayout,
    ResolvedGrainCol,
    ResolvedMeasureCol,
    qualify_grain_expression,
)

# Synthetic row-count column name shared by every builder and the matcher.
ROW_COUNT_COL = "__row_count__count"


@dataclass(frozen=True)
class SelectListDialect:
    """Per-dialect knobs for SELECT-list emission.

    - ``quote``: identifier quote character (``"`` for PostgreSQL-family,
      `` ` `` for BigQuery/Spark).
    - ``agg_templates``: aggregation-function → SQL template map. PostgreSQL
      and BigQuery share :data:`shared.semantic.grain_resolver.AGG_TEMPLATES`;
      Spark passes its own (functionally identical) table.
    - ``qualify_dialect``: the dialect token passed to
      :func:`qualify_grain_expression` for UDA grain expressions
      (``"postgres"`` / ``"bigquery"`` / ``"spark"``).
    """

    quote: str
    agg_templates: Mapping[str, str]
    qualify_dialect: str

    def q(self, ident: str) -> str:
        """Quote a single identifier."""
        return f"{self.quote}{ident}{self.quote}"


# Canonical dialects. The AGG templates are identical across all three (the
# previous Spark copy used a hand-rolled but byte-equal table); they are kept
# distinct objects only to preserve each module's original template source.
PG_DIALECT = SelectListDialect(quote='"', agg_templates=AGG_TEMPLATES, qualify_dialect="postgres")
BQ_DIALECT = SelectListDialect(quote="`", agg_templates=AGG_TEMPLATES, qualify_dialect="bigquery")
SPARK_AGG_TEMPLATES: dict[str, str] = {
    "sum": "SUM({ref})",
    "avg": "AVG({ref})",
    "min": "MIN({ref})",
    "max": "MAX({ref})",
    "count": "COUNT({ref})",
    "count_distinct": "COUNT(DISTINCT {ref})",
}
SPARK_DIALECT = SelectListDialect(quote="`", agg_templates=SPARK_AGG_TEMPLATES, qualify_dialect="spark")


def grain_source_ref(
    grain: ResolvedGrainCol,
    alias_by_table_id: Mapping[UUID, str],
    dialect: SelectListDialect,
) -> str:
    """SQL expression that reads a grain column in the FROM clause.

    Reproduces the per-builder ``_grain_source_ref`` exactly: UDA
    expressions are alias-qualified via :func:`qualify_grain_expression`;
    plain columns are ``alias."col"`` (dialect quote) when an alias exists,
    else bare-quoted; otherwise the logical name.
    """
    if grain.source_expression is not None:
        if grain.source_table_id is not None:
            alias = alias_by_table_id.get(grain.source_table_id)
            if alias:
                return qualify_grain_expression(
                    grain.source_expression, alias, dialect.qualify_dialect
                )
        return grain.source_expression
    if grain.source_table_id is not None and grain.source_column_name is not None:
        alias = alias_by_table_id.get(grain.source_table_id)
        if alias:
            return f"{alias}.{dialect.q(grain.source_column_name)}"
        return dialect.q(grain.source_column_name)
    return dialect.q(grain.logical_name)


def measure_source_ref(
    measure: ResolvedMeasureCol,
    alias_by_table_id: Mapping[UUID, str],
    dialect: SelectListDialect,
) -> str:
    """SQL expression that reads a measure's source column. Mirrors the
    per-builder ``_measure_source_ref`` exactly."""
    if measure.source_table_id is not None and measure.source_column_name is not None:
        alias = alias_by_table_id.get(measure.source_table_id)
        if alias:
            return f"{alias}.{dialect.q(measure.source_column_name)}"
        return dialect.q(measure.source_column_name)
    return dialect.q(measure.measure_name)


def measure_agg_expr(
    measure: ResolvedMeasureCol,
    alias_by_table_id: Mapping[UUID, str],
    dialect: SelectListDialect,
    source_numeric_types: Optional[Mapping[UUID, str]] = None,
) -> str:
    """The plain aggregation expression for a non-variant, non-calculated,
    non-quantile, non-stat measure column (the AGG-template path).

    Bug-5454 durability: when ``source_numeric_types`` maps this measure to a
    constrained ``numeric(p,s)`` and the aggregation is scale-preserving
    (sum/min/max), the expression is wrapped in ``ROUND(<agg>, s)`` so a
    same-engine refresh CTAS keeps the source scale ("0.00", not "0") instead
    of recomputing an unconstrained numeric that undoes the optimizer's typed
    create. The map is supplied ONLY by the same-engine PostgreSQL/Redshift
    refresh path; BigQuery/Spark and cross-DB callers pass ``None`` and are
    byte-identical to before. ROUND (not CAST to numeric(p,s)) keeps a large
    SUM from overflowing the source precision.
    """
    agg = (measure.aggregation_function or measure.stat_type or "sum").lower()
    template = dialect.agg_templates.get(agg, "SUM({ref})")
    source_ref = measure_source_ref(measure, alias_by_table_id, dialect)
    expr = template.format(ref=source_ref)
    if source_numeric_types:
        expr = cast_for_agg(expr, agg, source_numeric_types.get(measure.measure_id))
    return expr


def is_skippable_stat_column(measure: ResolvedMeasureCol) -> bool:
    """True for quantile (pNN) and dispersion-stat (stddev/var) columns,
    which are emitted by the dedicated quantile/stat blocks — never by the
    plain AGG template (which would corrupt them into SUMs)."""
    return is_quantile_stat_type(measure.stat_type) or is_stat_type(measure.stat_type)


def build_select_parts(
    *,
    layout: ResolvedAggregateLayout,
    alias_by_table_id: Mapping[UUID, str],
    dialect: SelectListDialect,
    emit: Callable[[str, str], str],
    rendered_calculated: Optional[Mapping[str, str]] = None,
    rendered_variants: Optional[Mapping[str, str]] = None,
    include_calculated_via_stat_type: bool = True,
    on_measure_emitted: Optional[Callable[[ResolvedMeasureCol, str, bool], None]] = None,
    on_grain_emitted: Optional[Callable[[ResolvedGrainCol], None]] = None,
    source_numeric_types: Optional[Mapping[UUID, str]] = None,
) -> list[str]:
    """Assemble the grain + measure + row-count SELECT parts.

    This is the single implementation that the three full-refresh DDL
    builders and the incremental ``_build_select_parts`` share. Quantile
    and stat blocks (which are genuinely dialect-specific in expression
    form) are appended by the caller after this returns.

    Parameters:
    - ``emit(expr, out_name)``: returns one formatted ``"  expr AS <quoted>"``
      part. Supplied by the caller so each builder keeps its exact indent
      and spacing (PostgreSQL/BigQuery use ``"  {expr} AS ..."``; Spark uses
      a bare ``"{expr} AS ..."`` with a different join separator).
    - ``rendered_variants`` / ``rendered_calculated``: pre-rendered window /
      expression SQL keyed by physical_col_name (full-refresh paths). When
      absent (incremental), variant/calculated/stat columns are already
      excluded upstream.
    - ``include_calculated_via_stat_type``: full-refresh paths recognise a
      calculated column by ``stat_type == "calculated"`` *and* a non-empty
      ``rendered_calculated`` entry; the incremental path has neither and
      skips via the stat-column check. Kept as a flag so behaviour matches
      each original copy exactly.
    - ``on_measure_emitted(measure, expr, is_plain_agg)``: optional hook the
      cross-DB builders use to collect column_defs. ``is_plain_agg`` is True
      only for the AGG-template branch (so the caller can pick the right
      result type).
    - ``on_grain_emitted(grain)``: optional hook for grain column_defs.
    - ``source_numeric_types``: optional ``measure_id -> "numeric(p,s)"`` map
      (Bug-5454). When supplied (same-engine PG/Redshift refresh only), a
      scale-preserving plain AGG over a constrained-numeric source column is
      wrapped in ``ROUND(<agg>, s)`` so the refresh CTAS keeps the source
      scale and does not undo the optimizer's typed create. ``None`` (the
      default for BigQuery/Spark and cross-DB callers) leaves every expression
      byte-identical to before.

    Returns the ordered list of formatted SELECT parts (grain, then
    measures, then the row count).
    """
    parts: list[str] = []

    for grain in layout.grain_cols:
        source_ref = grain_source_ref(grain, alias_by_table_id, dialect)
        parts.append(emit(source_ref, grain.physical_col_name))
        if on_grain_emitted is not None:
            on_grain_emitted(grain)

    for measure in layout.measure_cols:
        # Variant measure columns carry a pre-rendered window expression
        # (F-009-01) — the plain AGG template would silently rebuild them as
        # plain aggregates and destroy the variant semantics on refresh.
        if rendered_variants and measure.physical_col_name in rendered_variants:
            expr = rendered_variants[measure.physical_col_name]
            parts.append(emit(expr, measure.physical_col_name))
            if on_measure_emitted is not None:
                on_measure_emitted(measure, expr, False)
            continue
        if (
            include_calculated_via_stat_type
            and measure.stat_type == "calculated"
            and rendered_calculated
        ):
            expr = rendered_calculated.get(measure.physical_col_name)
            if expr:
                parts.append(emit(expr, measure.physical_col_name))
                if on_measure_emitted is not None:
                    on_measure_emitted(measure, expr, False)
                continue
        # Quantile (pNN) / stat (stddev,var) columns are emitted by the
        # dedicated blocks the caller appends — skip them here so the main
        # loop does not re-emit them as SUM (corruption: every pNN/stat == SUM).
        if is_skippable_stat_column(measure):
            continue
        expr = measure_agg_expr(
            measure, alias_by_table_id, dialect, source_numeric_types
        )
        parts.append(emit(expr, measure.physical_col_name))
        if on_measure_emitted is not None:
            on_measure_emitted(measure, expr, True)

    parts.append(emit("COUNT(*)", ROW_COUNT_COL))
    return parts
