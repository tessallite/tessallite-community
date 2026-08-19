"""Period-variant aggregate route (Bug-8043 / F-015-03, Option A).

Period-aware variants (YoY / YTD / QTD / prior-year / cumulative) are computed at
QUERY TIME over an aggregate that stores their BASE measure — NOT by materialising
physical variant columns. This module resolves ONE variant's period-window SQL
expression; it is reached ONLY after ``routing/aggregate_matcher.py`` has proven the
admission rules (``PeriodVariantPlan``) and is consumed by the ORDINARY aggregate
rewriter (``rewrite/aggregate.py`` ``rewrite_for_aggregate``).

WHY it plugs into the ordinary rewriter rather than emitting a parallel statement
(Codex live-Postgres gate, 2026-07-27): the period-variant answer must be identical to
the source route for the WHOLE query shape — not just the window value. The source route
emits the variant as ONE GROUP-BY query: the base is re-aggregated to the query grain and
the window is the doubled-aggregate ``AGG(SUM(base)) OVER (...)``, alongside the query's
own WHERE / GROUP BY / HAVING / ORDER BY / LIMIT / projection positions / aliases /
literals. The aggregate route reproduces that EXACT shape by letting the ordinary
aggregate envelope build everything (it already re-aggregates the stored base via
``SUM(<stored sum>)`` at coarser grain, groups by the query grain, projects the grain
DIMENSION's stored value as the key, maps HAVING onto the stored columns BEFORE the
window, and renders ORDER BY + pagination) and injecting ONLY the variant measure's
projected column as the period window.

Correctness (prove-or-fall-back): the window is produced by the SAME emitter the source
route uses (``shared.semantic.time_variants_sql.emit_variant_expression``) with the SAME
``calendar_type / fiscal_year_start_month / time_grain / partition_by``. Only two inputs
change and both are byte-equivalent to source:

  - ``base_expression`` = the ordinary envelope's re-aggregation of the stored base sum
    (``SUM(<stored sum>)`` at coarser grain, the stored column itself at exact grain),
    which equals the source route's ``SUM(<base source column>)`` because the stored
    column IS that sum and SUM is additive;
  - ``fact_date_column`` = ``MIN(<query time-dimension column>)`` (or the column itself
    at exact grain), which equals the source route's ``MIN(<raw fact date>)`` at the
    query grain — the matcher proved the time dimension's own source column IS the
    variant's configured date anchor, and at grain U the emitter prunes sub-U position
    components so ``EXTRACT``/ordering are invariant between the stored grain value and
    the raw fact date (``MIN(trunc(x)) = trunc(MIN(x))``).

Any shape a target cannot express raises ``AggregateRewriteUnsupported`` so the router
falls back to the always-correct source route.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from shared.semantic.time_variants_sql import (
    VariantBinding,
    VariantSqlError,
    emit_variant_expression,
)

from src.rewrite.aggregate import AggregateRewriteUnsupported


# Dialect tokens the variant emitter can produce (mirrors
# time_variants_sql.SUPPORTED_DIALECTS). ``_dialect_to_connector`` maps a target sqlglot
# dialect onto exactly one of these; a target outside the set falls back to source.
_EMITTER_SUPPORTED_DIALECTS: frozenset[str] = frozenset(
    {"postgresql", "bigquery", "hadoop_spark", "snowflake", "redshift", "sqlserver"}
)

# Query time-grain units the route supports. Mirrors the emitter's grain ranks;
# anything outside this set is not a recognised period unit and falls back.
_SUPPORTED_TIME_GRAINS: frozenset[str] = frozenset(
    {"day", "week", "month", "quarter", "half", "year"}
)


@dataclass
class PeriodVariantItem:
    """One admitted period variant to emit as a projected window column."""

    measure_name: str
    variant_kind: str
    variant_n: int | None
    alias: str
    # Physical column of the stored base statistic (base_measure, 'sum').
    base_sum_col: str


@dataclass
class PeriodVariantPlan:
    """Proven admission certificate for a period-variant aggregate route.

    Resolved by the matcher (async, deployed-snapshot authority) and consumed by the
    ordinary aggregate rewriter via ``emit_period_variant_column`` (sync, no DB). Every
    field is a proven fact; the emitter does no further admissibility reasoning.
    """

    items: list[PeriodVariantItem]
    # Physical column in the aggregate grain that stores the query time dimension's own
    # value — the window anchor (proven by the matcher to be the variant's configured
    # date anchor), plus its data_type.
    anchor_phys_col: str
    anchor_data_type: str | None
    # Query non-time partition dimensions: logical name -> aggregate physical column.
    partition_logical_to_phys: dict[str, str]
    # Query time grain (day/week/month/quarter/half/year) and the query time dimension's
    # logical name.
    time_grain_unit: str
    time_dim_logical: str
    # Expression-capable calendar identity (standard/fiscal/iso_week/thai_buddhist).
    calendar_type: str | None
    fiscal_year_start_month: int | None = None
    # Distinct base-sum columns (deduplicated across items).
    base_sum_cols: tuple[str, ...] = field(default_factory=tuple)
    # Every query filter dimension present in the aggregate grain -> physical column
    # (retained for diagnostics; the ordinary envelope maps filters itself).
    filter_logical_to_phys: dict[str, str] = field(default_factory=dict)


def emit_period_variant_column(
    item: PeriodVariantItem,
    plan: PeriodVariantPlan,
    *,
    base_sql: str,
    fact_date_sql: str,
    partition_exprs: tuple[str, ...],
    emit_dialect: str,
) -> str:
    """Return the target-dialect period-window SQL for ONE variant, to be projected as
    the variant measure's column inside the ordinary aggregate SELECT.

    ``base_sql`` / ``fact_date_sql`` / ``partition_exprs`` are built by the caller from
    the aggregate's physical columns EXACTLY as the ordinary envelope re-aggregates and
    groups (so the window's PARTITION BY / ORDER BY reference the same grouped columns).
    They are Postgres-canonical; the emitter transpiles the whole fragment to
    ``emit_dialect``. Raises ``AggregateRewriteUnsupported`` on any shape the emitter
    cannot serve, so the router falls back to source.
    """
    unit = plan.time_grain_unit.lower()
    if unit not in _SUPPORTED_TIME_GRAINS:
        raise AggregateRewriteUnsupported(
            f"period-variant route: unsupported query time grain "
            f"{plan.time_grain_unit!r}; falling back to source"
        )
    if emit_dialect not in _EMITTER_SUPPORTED_DIALECTS:
        raise AggregateRewriteUnsupported(
            f"period-variant route: emitter does not support dialect "
            f"{emit_dialect!r}; falling back to source"
        )
    binding = VariantBinding(
        base_expression=base_sql,
        base_unaggregated=None,
        fact_date_column=fact_date_sql,
        calendar_alias="cal",
        calendar_columns=None,  # expression-capable only (matcher enforces)
        dialect=emit_dialect,
        n=item.variant_n,
        partition_by=tuple(partition_exprs),
        calendar_type=plan.calendar_type,
        fiscal_year_start_month=plan.fiscal_year_start_month,
        time_grain=unit,
    )
    try:
        variant_result = emit_variant_expression(item.variant_kind, binding)
    except VariantSqlError as exc:
        raise AggregateRewriteUnsupported(
            f"period-variant route: emitter refused {item.variant_kind!r}: {exc}; "
            f"falling back to source"
        ) from exc
    # Defence-in-depth: an expression-capable calendar references NO calendar-table
    # column, so the emitter must not have tracked one. If it did, the plan/matcher
    # admitted a calendar this route cannot serve — fail closed.
    if variant_result.referenced_calendar_keys:
        raise AggregateRewriteUnsupported(
            "period-variant route: emitter referenced calendar-table columns "
            f"{sorted(variant_result.referenced_calendar_keys)!r}, which the "
            "expression-only aggregate route cannot provide; falling back to source"
        )
    return variant_result.sql
