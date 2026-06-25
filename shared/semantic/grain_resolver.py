"""Grain + measure resolution for aggregate definitions.

Given a model's dimensions, measures, tables and columns, produce a
resolved layout that the DDL builders and query rewriter can consume:

- Every grain entry carries its source ``ModelTable.id`` and the physical
  ``ModelColumn.column_name`` so CTAS SQL can qualify the reference with
  the correct table alias (root-cause fix for aggregate ambiguity under
  multi-table joins — Bug-053).
- Every output column name is guaranteed unique across the aggregate.
  When two entries would resolve to the same local name, both are
  prefixed with their source table's slug so the aggregate table never
  produces a ``column ... specified more than once`` error (Rule 5).

This module is import-safe from any service and must not touch the
database — callers hand in already-loaded ORM rows.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional
from uuid import UUID

from shared.db.models import (
    Dimension,
    Measure,
    ModelColumn,
    ModelTable,
    UserDefinedAttribute,
)


# Postgres identifier limit. Other dialects (BigQuery: 300, Spark: 767)
# are more permissive, so the tightest wins.
_MAX_IDENT_LEN = 63

# Strips the common dim/fact prefix from a physical table name when
# deriving a stable human-readable slug for collision prefixes.
_TABLE_PREFIX = re.compile(r"^(dim|fact|aggregate|agg)_")


@dataclass(frozen=True)
class ResolvedGrainCol:
    """A single grain entry, resolved to its physical source and output name."""

    logical_name: str
    dimension_id: UUID
    source_table_id: Optional[UUID]
    source_column_name: Optional[str]
    physical_col_name: str
    source_expression: Optional[str] = None

    @property
    def is_resolved(self) -> bool:
        return (
            (self.source_table_id is not None and self.source_column_name is not None)
            or self.source_expression is not None
        )


@dataclass(frozen=True)
class ResolvedMeasureCol:
    """A single measure output column, with its source and aggregation function."""

    measure_id: UUID
    measure_name: str
    stat_type: str
    aggregation_function: Optional[str]
    source_table_id: Optional[UUID]
    source_column_name: Optional[str]
    physical_col_name: str

    @property
    def is_resolved(self) -> bool:
        return self.source_table_id is not None and self.source_column_name is not None


@dataclass(frozen=True)
class ResolvedAggregateLayout:
    grain_cols: list[ResolvedGrainCol]
    measure_cols: list[ResolvedMeasureCol]

    @property
    def grain_logical_names(self) -> list[str]:
        return [g.logical_name for g in self.grain_cols]

    @property
    def grain_physical_cols(self) -> list[str]:
        return [g.physical_col_name for g in self.grain_cols]


class GrainResolutionError(ValueError):
    """Raised when a grain or measure reference cannot be resolved at all."""


def qualify_grain_expression(
    expression: str, alias: str, dialect: str = "postgres",
) -> str:
    """Qualify bare column references in a UDA SQL expression with a table alias.

    Stored UDA expressions use canonical PostgreSQL format with bare
    double-quoted identifiers, e.g. ``EXTRACT(YEAR FROM ("date_key"))``.
    When the refresh FROM clause aliases tables (``base``, ``t1``, …),
    these bare references can be ambiguous.  This function rewrites them
    to ``alias."col"`` (or the dialect-appropriate equivalent) so the
    CTAS is unambiguous.
    """
    import sqlglot
    from sqlglot import exp

    tree = sqlglot.parse_one(expression, read="postgres")
    for col_node in tree.find_all(exp.Column):
        if col_node.table:
            continue
        col_node.set("table", exp.to_identifier(alias))
    return tree.sql(dialect=dialect)


def resolve_aggregate_layout(
    *,
    grain_names: Iterable[str],
    measure_specs: Iterable[tuple[str, str, Optional[str]]],
    dimensions: Iterable[Dimension],
    measures: Iterable[Measure],
    tables: Iterable[ModelTable],
    columns: Iterable[ModelColumn],
    user_defined_attributes: Iterable[UserDefinedAttribute] = (),
) -> ResolvedAggregateLayout:
    """Resolve a grain list and a set of measure specs into a layout.

    ``measure_specs`` is an iterable of ``(measure_name, stat_type, agg_function)``
    triples; ``agg_function`` defaults to the measure's ``default_agg`` when
    None. Unknown measures raise :class:`GrainResolutionError`. Unknown grain
    names also raise. Dimensions or measures without a ``source_column_id``
    (e.g. user-defined attributes or expression-based measures) are returned
    with ``source_table_id=None`` so the DDL builder can fall back to a safe
    default; they are not considered a hard failure.
    """
    dim_by_name = {d.name: d for d in dimensions}
    measure_by_name = {m.name: m for m in measures}
    table_by_id = {t.id: t for t in tables}
    column_by_id = {c.id: c for c in columns}
    uda_by_id = {u.id: u for u in user_defined_attributes}

    grain_cols: list[ResolvedGrainCol] = []
    for name in grain_names:
        dim = dim_by_name.get(name)
        if dim is None:
            raise GrainResolutionError(f"Unknown dimension: {name!r}")
        src_table_id: Optional[UUID] = None
        src_col_name: Optional[str] = None
        src_expression: Optional[str] = None
        if dim.source_column_id is not None:
            col = column_by_id.get(dim.source_column_id)
            if col is not None:
                src_table_id = col.model_table_id
                src_col_name = col.column_name
        elif dim.user_defined_attribute_id is not None:
            uda = uda_by_id.get(dim.user_defined_attribute_id)
            if uda is not None:
                src_expression = uda.expression
                src_table_id = uda.table_id
        grain_cols.append(
            ResolvedGrainCol(
                logical_name=dim.name,
                dimension_id=dim.id,
                source_table_id=src_table_id,
                source_column_name=src_col_name,
                physical_col_name=bound_ident(dim.name),
                source_expression=src_expression,
            )
        )

    measure_cols: list[ResolvedMeasureCol] = []
    for measure_name, stat_type, agg_fn in measure_specs:
        measure = measure_by_name.get(measure_name)
        if measure is None:
            raise GrainResolutionError(f"Unknown measure: {measure_name!r}")
        src_table_id = None
        src_col_name = None
        if measure.source_column_id is not None:
            col = column_by_id.get(measure.source_column_id)
            if col is not None:
                src_table_id = col.model_table_id
                src_col_name = col.column_name
        physical = bound_ident(f"{measure.name}__{stat_type}")
        measure_cols.append(
            ResolvedMeasureCol(
                measure_id=measure.id,
                measure_name=measure.name,
                stat_type=stat_type,
                # Fall back to the column's own stat_type (max/min/count/...)
                # before the measure default_agg. Existing AggregateColumn rows
                # store aggregation_function=NULL, so using default_agg here
                # rebuilt EVERY stat column as the default (e.g. SUM) on refresh,
                # corrupting max/min/count columns.
                aggregation_function=agg_fn or stat_type or measure.default_agg,
                source_table_id=src_table_id,
                source_column_name=src_col_name,
                physical_col_name=physical,
            )
        )

    grain_cols, measure_cols = _resolve_physical_name_collisions(
        grain_cols, measure_cols, table_by_id
    )
    return ResolvedAggregateLayout(grain_cols=grain_cols, measure_cols=measure_cols)


def derive_table_slug(table: ModelTable) -> str:
    """Return a stable, human-readable slug for a model table.

    Priority:
    1. Last dotted segment of ``physical_name`` with ``dim_``/``fact_``
       prefix stripped.
    2. Fallback to the raw last segment.
    3. Fallback to the UUID hex prefix.
    """
    raw = table.physical_name or ""
    last = raw.split(".")[-1] if raw else ""
    cleaned = _TABLE_PREFIX.sub("", last)
    if cleaned:
        return cleaned
    if last:
        return last
    return table.id.hex[:12]


def _resolve_physical_name_collisions(
    grain_cols: list[ResolvedGrainCol],
    measure_cols: list[ResolvedMeasureCol],
    table_by_id: Mapping[UUID, ModelTable],
) -> tuple[list[ResolvedGrainCol], list[ResolvedMeasureCol]]:
    """Apply the collision-only output-alias prefix rule.

    Group all output names across grain and measures. When the same
    ``physical_col_name`` appears in two or more entries AND the entries
    resolve to different source tables, prefix every colliding entry with
    its table slug. Entries that have no resolved source table (UDA /
    expression) keep their original name — the DDL builder handles those
    via fallback.
    """
    name_index: dict[str, list[tuple[str, int]]] = {}
    for i, g in enumerate(grain_cols):
        name_index.setdefault(g.physical_col_name, []).append(("grain", i))
    for i, m in enumerate(measure_cols):
        name_index.setdefault(m.physical_col_name, []).append(("measure", i))

    new_grain = list(grain_cols)
    new_measures = list(measure_cols)

    for name, entries in name_index.items():
        if len(entries) < 2:
            continue
        resolved = [e for e in entries if _source_table_id(new_grain, new_measures, e) is not None]
        if len({_source_table_id(new_grain, new_measures, e) for e in resolved}) < 2:
            # Either all unresolved or all from the same table — no real conflict.
            continue
        for kind, idx in entries:
            source_id = _source_table_id(new_grain, new_measures, (kind, idx))
            if source_id is None:
                continue
            table = table_by_id.get(source_id)
            if table is None:
                continue
            slug = derive_table_slug(table)
            prefixed = _bound_ident(f"{slug}_{name}")
            if kind == "grain":
                g = new_grain[idx]
                new_grain[idx] = ResolvedGrainCol(
                    logical_name=g.logical_name,
                    dimension_id=g.dimension_id,
                    source_table_id=g.source_table_id,
                    source_column_name=g.source_column_name,
                    physical_col_name=prefixed,
                    source_expression=g.source_expression,
                )
            else:
                m = new_measures[idx]
                new_measures[idx] = ResolvedMeasureCol(
                    measure_id=m.measure_id,
                    measure_name=m.measure_name,
                    stat_type=m.stat_type,
                    aggregation_function=m.aggregation_function,
                    source_table_id=m.source_table_id,
                    source_column_name=m.source_column_name,
                    physical_col_name=prefixed,
                )
    return new_grain, new_measures


def _source_table_id(
    grain: list[ResolvedGrainCol],
    measures: list[ResolvedMeasureCol],
    entry: tuple[str, int],
) -> Optional[UUID]:
    kind, idx = entry
    if kind == "grain":
        return grain[idx].source_table_id
    return measures[idx].source_table_id


# Maps a measure's default_agg to the set of additional physical columns
# materialised alongside the primary stat.  For each additive measure the
# CTAS scan always computes the primary aggregation function PLUS these
# extra stat types, because the marginal cost of backpacking them onto the
# same full-table scan is near zero while the hit-rate gain is substantial.
#
# Physical column naming:  {measure}__{stat_type}
#   e.g. a measure "revenue" with default_agg "sum" produces:
#     revenue__sum, revenue__count, revenue__min, revenue__max
#
# AVG rule (F-004-01):
#   An avg-default measure DOES materialise a primary {measure}__avg column,
#   but a stored average is not re-aggregatable, so the router reads it at
#   EXACT grain only.  At any coarser grain the router always rewrites
#   AVG(measure) → SUM(sum_col) * 1.0 / NULLIF(SUM(count_col), 0) — correct
#   because SUM and COUNT are both additive across grains.
#
# Global row-count column:
#   Every aggregate unconditionally includes __row_count__count so that
#   COUNT(*) queries can be served without a source hit.
#
# Passthrough functions:
#   Functions classified as "passthrough" in sql_functions.json (STDDEV,
#   VARIANCE, PERCENTILE, etc.) are never materialised and always route
#   to the source table.  They are not additive and cannot be derived
#   from pre-computed stat columns.
MULTI_STAT_MAP: dict[str, list[str]] = {
    "sum":   ["count", "min", "max"],
    "avg":   ["sum", "count", "min", "max"],
    "count": ["min", "max"],
    "min":   ["count", "max"],
    "max":   ["count", "min"],
}

AGG_TEMPLATES: dict[str, str] = {
    "sum": "SUM({ref})",
    "avg": "AVG({ref})",
    "min": "MIN({ref})",
    "max": "MAX({ref})",
    "count": "COUNT({ref})",
    "count_distinct": "COUNT(DISTINCT {ref})",
}


def expand_additive_measure_specs(
    name: str,
    default_agg: str,
) -> list[tuple[str, str, str]]:
    """Return all ``(name, stat_type, agg_fn)`` triples for an additive measure.

    The primary stat type (``default_agg``) is always first, followed by
    additional type-compatible stat types from ``MULTI_STAT_MAP``.
    For ``avg`` the primary column is exact-grain-readable only; coarser
    grains derive from the ``sum``/``count`` extras (F-004-01).
    """
    agg = default_agg.lower()
    specs: list[tuple[str, str, str]] = [(name, agg, agg)]
    for extra in MULTI_STAT_MAP.get(agg, []):
        specs.append((name, extra, extra))
    return specs


def bound_ident(name: str) -> str:
    """Truncate an identifier to the tightest dialect limit, keeping a hash
    suffix so truncated names stay unique.

    F-009-16: every emitted physical column/alias name routes through this so
    the name stored in ``AggregateColumn.physical_col_name`` and the name in
    the DDL identifier agree — PostgreSQL silently truncates DDL identifiers at
    63 bytes, and an untruncated stored name would then mismatch the physical
    table, breaking the matcher's column lookup. The reserved ``__row_count``
    synthetic and any name already within the limit pass through unchanged.
    """
    if len(name) <= _MAX_IDENT_LEN:
        return name
    import hashlib

    digest = hashlib.md5(name.encode("utf-8")).hexdigest()[:6]
    keep = _MAX_IDENT_LEN - len(digest) - 1
    return f"{name[:keep]}_{digest}"


# Backwards-compatible private alias (the collision resolver and existing
# call sites used the underscore name).
_bound_ident = bound_ident
