"""Shared variant-measure column rendering for aggregate CTAS bodies.

Single source of truth for HOW a variant measure (lag / trailing_n /
moving_avg_n) materialises as a physical aggregate column. Used by BOTH
generations of DDL builders:

  - create time  — optimizer ``src/ddl/{postgres,bigquery,spark}_ddl.py``
    (via the ``src.ddl._variant_columns`` re-export);
  - refresh time — scheduler ``src/jobs/full_refresh.py`` pre-renders
    ``rendered_variants`` for its layout-based builders.

Moved here from ``optimizer/src/ddl/_variant_columns.py`` for B6 / F-009-01:
the scheduler's refresh builders had no variant branch, so the first
scheduled refresh silently rebuilt every variant column as a plain
aggregate (wrong business data). Refresh now renders variant columns with
exactly this code, so create and refresh can never drift again.

Decision tree (Phase C of the variant pivot, Bug-091):

  - period-aware variants (those in TIME_VARIANTS_NEEDING_CALENDAR)
    are rejected: per docs/archive/archive_optimizer-variant-ctas-questions.md
    Q3=C, only window-based variants (lag, trailing_n, moving_avg_n)
    are materialised in CTAS pre-aggregates. Period-aware variants
    keep computing on top of the base measure CTAS or the source.

  - window-based variants require a time grain column in the
    aggregate's grain (the "shape" constraint from Q1).

  - the variant SQL itself is delegated to
    shared.semantic.time_variants_sql.emit_variant_expression, which
    handles dialect transpilation via sqlglot. The doubled-aggregate
    pattern (e.g. ``SUM(SUM("amount")) OVER (...)``) emerges
    automatically when ``base_expression`` already contains the inner
    aggregate, matching the rewriter's calling convention.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import CalendarTable, Dimension, Measure, ModelColumn
from shared.connector_qualify import safe_ident
from shared.schemas.measure_formats import (
    TIME_VARIANT_NAMES,
    TIME_VARIANTS_NEEDING_CALENDAR,
)
from shared.semantic.time_variants_sql import (
    VariantBinding,
    VariantSqlError,
    emit_variant_expression,
    resolve_effective_variant_anchor,
    select_finest_time_dimension,
)

# Variants that can be safely materialised inside a CTAS without a
# calendar JOIN. Computed once at import time.
CTAS_ALLOWED_VARIANTS: frozenset[str] = TIME_VARIANT_NAMES - TIME_VARIANTS_NEEDING_CALENDAR


class VariantContextError(ValueError):
    """A variant measure cannot be resolved into a CTAS column shape.

    Raised by ``resolve_variant_context`` for the *recoverable* shapes the
    optimizer create path drops-and-continues on (no time dimension in grain,
    or a variant with no resolvable base source column / snapshot). Catching
    this by type — instead of matching exception message substrings (F-009-15)
    — means rewording the messages can never silently turn the graceful
    exclusion into a hard create failure. Subclasses ``ValueError`` so existing
    ``except ValueError`` callers keep working.
    """


@dataclass(frozen=True)
class VariantContext:
    """Resolved CTAS context shared by create and refresh.

    The iterator deliberately yields the historical three values so patched
    callers and older integrations remain source-compatible while the fourth
    value, ``time_grain``, keeps source and aggregate window ordering aligned.
    """

    time_grain_column: str | None
    source_column_names: dict
    source_table_ids: dict
    time_grain: str | None = None
    calendar_bindings: dict | None = None

    def __iter__(self) -> Iterator:
        yield self.time_grain_column
        yield self.source_column_names
        yield self.source_table_ids


@dataclass(frozen=True, slots=True)
class VariantCalendarBinding:
    """Calendar metadata needed to reproduce source window ordering in CTAS."""

    calendar_type: str
    fiscal_year_start_month: int | None
    calendar_columns: dict[str, str]
    calendar_model_table_id: object | None


_CALENDAR_COLUMN_ATTRS: tuple[tuple[str, str], ...] = (
    ("date", "date_column"),
    ("year", "year_column"),
    ("half", "half_column"),
    ("quarter", "quarter_column"),
    ("month", "month_column"),
    ("week", "week_column"),
    ("day", "day_column"),
)


def _calendar_binding(calendar: Any, measure: Any) -> VariantCalendarBinding:
    return VariantCalendarBinding(
        calendar_type=getattr(calendar, "calendar_type", None) or "standard",
        fiscal_year_start_month=getattr(
            calendar, "fiscal_year_start_month", None,
        ),
        calendar_columns={
            key: value
            for key, attr in _CALENDAR_COLUMN_ATTRS
            if (value := getattr(calendar, attr, None))
        },
        calendar_model_table_id=getattr(
            measure, "calendar_model_table_id", None,
        ),
    )


def is_variant(measure: Any) -> bool:
    return getattr(measure, "variant_kind", None) is not None


def _vq(
    col_name: str,
    table_id: UUID | None,
    table_aliases: dict | None,
) -> str:
    """Qualify a column name with its table alias when joins are present."""
    if table_aliases and table_id is not None:
        alias = table_aliases.get(table_id)
        if alias:
            return f"{safe_ident(alias)}.{safe_ident(col_name)}"
    return safe_ident(col_name)


def render_variant_column_sql(
    measure: Any,
    *,
    dialect: str,
    grain: list[str],
    time_grain_column: str | None,
    source_column_names: dict[UUID, str] | None,
    quote: str = '"',
    table_aliases: dict | None = None,
    base_source_table_id: UUID | None = None,
    time_grain_source_table_id: UUID | None = None,
    grain_cols: Any | None = None,
    time_grain: str | None = None,
    calendar_binding: VariantCalendarBinding | None = None,
) -> str:
    """Return the SELECT-list expression for a variant measure column.

    Internally, identifiers are always rendered with the canonical
    Postgres double-quote because ``emit_variant_expression`` authors
    SQL in canonical Postgres and lets sqlglot transpile to the target
    dialect. ``quote`` is accepted for callers that want to express
    intent at their layer but is currently ignored by the emitter
    boundary — sqlglot owns the dialect rewrite.

    When ``table_aliases`` is provided (joined CTAS), column references
    are qualified with the resolved table alias to avoid ambiguity.

    Raises ValueError when the variant cannot be materialised.
    """
    del quote  # canonical-Postgres input always; sqlglot handles dialect
    kind = measure.variant_kind
    if kind in TIME_VARIANTS_NEEDING_CALENDAR:
        raise ValueError(
            f"variant {kind!r} is period-aware and cannot be materialised in "
            f"a CTAS pre-aggregate; rewrite over the base measure instead"
        )
    if kind not in CTAS_ALLOWED_VARIANTS:
        raise ValueError(f"variant {kind!r} is not supported in CTAS pre-aggregates")
    if not time_grain_column:
        raise ValueError(
            f"variant measure {measure.name!r} requires a time dimension in the "
            f"aggregate grain (got {grain!r})"
        )
    if time_grain_column not in grain:
        raise ValueError(
            f"variant measure {measure.name!r} requires time grain column "
            f"{time_grain_column!r} to be in grain {grain!r}"
        )
    base_col = (source_column_names or {}).get(getattr(measure, "id", None))
    if not base_col:
        raise ValueError(
            f"variant measure {measure.name!r} has no resolved source column; "
            f"creator must pass source_column_names for variant measures"
        )

    base_agg = (measure.default_agg or "sum").upper()
    base_expression = f'{base_agg}({_vq(base_col, base_source_table_id, table_aliases)})'

    grain_expr_map: dict[str, str] = {}
    if grain_cols:
        from shared.semantic.grain_resolver import qualify_grain_expression
        for gc in grain_cols:
            if gc.source_expression is not None:
                expr = gc.source_expression
                if gc.source_table_id is not None and table_aliases:
                    alias = table_aliases.get(gc.source_table_id)
                    if alias:
                        expr = qualify_grain_expression(expr, alias)
                grain_expr_map[gc.logical_name] = expr
            else:
                col = gc.source_column_name or gc.logical_name
                grain_expr_map[gc.logical_name] = _vq(col, gc.source_table_id, table_aliases)

    tg_expr = grain_expr_map.get(time_grain_column)
    if tg_expr is None:
        tg_expr = _vq(time_grain_column, time_grain_source_table_id, table_aliases)
    tg_expr = f"MIN({tg_expr})"

    partition_names = [g for g in grain if g != time_grain_column]
    partition_exprs: list[str] = []
    for p in partition_names:
        expr = grain_expr_map.get(p)
        if expr is None:
            expr = _vq(p, None, table_aliases)
        partition_exprs.append(f"MIN({expr})")

    calendar_alias = "cal"
    calendar_columns = None
    if calendar_binding is not None:
        calendar_columns = calendar_binding.calendar_columns or None
        calendar_table_id = calendar_binding.calendar_model_table_id
        if calendar_columns:
            calendar_alias = (
                table_aliases.get(calendar_table_id)
                if table_aliases and calendar_table_id is not None
                else None
            )
        if calendar_columns and calendar_alias is None:
            raise ValueError(
                f"variant measure {measure.name!r} requires mapped calendar "
                f"columns but the calendar table is absent from the CTAS join graph"
            )

    binding = VariantBinding(
        base_expression=base_expression,
        fact_date_column=tg_expr,
        dialect=dialect,
        n=getattr(measure, "variant_n", None),
        partition_by=tuple(partition_exprs),
        time_grain=time_grain,
        calendar_alias=calendar_alias,
        calendar_columns=calendar_columns,
        calendar_type=(
            calendar_binding.calendar_type if calendar_binding else None
        ),
        fiscal_year_start_month=(
            calendar_binding.fiscal_year_start_month if calendar_binding else None
        ),
        aggregate_calendar_columns=bool(calendar_columns),
    )
    try:
        return emit_variant_expression(kind, binding).sql
    except VariantSqlError as exc:
        raise ValueError(str(exc)) from exc


def variant_physical_col_name(measure: Any) -> str:
    """Physical column name for a variant in the CTAS.

    Uses the ``{name}__{default_agg}`` convention so the rewriter's
    lookup ``(measure.name, default_agg)`` resolves correctly. The
    snapshot ``default_agg`` on the variant matches the base, which
    is the aggregation flavour the rewriter passes when binding the
    variant measure.
    """
    from shared.semantic.grain_resolver import bound_ident

    agg = (measure.default_agg or "sum").lower()
    return bound_ident(f"{measure.name}__{agg}")


def variant_stat_type(measure: Any) -> str:
    return (measure.default_agg or "sum").lower()


def resolve_time_grain_table_id(grain_cols, time_grain_column):
    """Source table id of the time-grain column in a resolved layout."""
    if grain_cols and time_grain_column:
        for gc in grain_cols:
            if gc.logical_name == time_grain_column:
                return gc.source_table_id
    return None


async def resolve_variant_context(
    *,
    model_id: object,
    grain: list[str],
    measures: list[Measure],
    all_dims: list | None = None,
    db: AsyncSession,
) -> VariantContext:
    """Resolve the time-grain column and base source-column names needed
    by the DDL emitters when variant measures are part of the request.

    Shared by the optimizer create path and the scheduler refresh path
    (F-009-01) so both resolve variant shape identically.

    Returns a :class:`VariantContext`.  Iterating it yields the historical
    ``(time_grain_column, source_column_names, source_table_ids)`` values;
    ``time_grain`` is available as a named field for window-order parity.

    When ``all_dims`` is provided (includes hierarchy-level virtual dims),
    it is used for time-dimension detection instead of querying only the
    flat ``Dimension`` table.  This ensures hierarchy-level time
    dimensions (e.g. ``nps_survey_date_calendar.Month``) are found.

    Raises ``ValueError`` when a variant measure is requested but the
    aggregate's grain does not contain a time dimension (the shape
    constraint from docs/archive/archive_optimizer-variant-ctas-questions.md Q1).
    """
    variant_measures = [m for m in measures if is_variant(m)]
    if not variant_measures:
        return VariantContext(None, {}, {}, None, {})

    # Time-grain column: the logical dimension name for the time
    # dimension in ``grain``.  The renderer resolves the physical
    # source column from grain_cols at render time.
    time_grain_column: str | None = None
    time_grain_source_column_id: object = None
    if grain:
        grain_set = set(grain)
        if all_dims:
            time_dims = [
                d for d in all_dims
                if (
                    (getattr(d, "is_time_dim", False)
                     or getattr(d, "dimension_kind", None) == "time")
                    and d.name in grain_set
                )
            ]
        else:
            result = await db.execute(
                select(Dimension).where(
                    Dimension.model_id == model_id,
                    Dimension.is_time_dim.is_(True),
                    Dimension.name.in_(grain),
                )
            )
            time_dims = list(result.scalars().all())
        selected_time_dim = select_finest_time_dimension(time_dims, grain_set)
        if selected_time_dim is not None:
            time_grain_column = selected_time_dim.name
            time_grain_source_column_id = getattr(
                selected_time_dim, "source_column_id", None
            )

    if time_grain_column is None:
        names = [m.name for m in variant_measures]
        raise VariantContextError(
            f"variant measures {names!r} require a time dimension in the "
            f"aggregate grain {grain!r}; none found"
        )

    # Source column names for each variant measure (the base column the
    # window function reads). Variant rows snapshot the base measure's
    # source_column_id at create time. Checked before the anchor gate so a
    # variant with no snapshot fails with the snapshot diagnostic first
    # (Bug-3591/Bug-1091 API contract); both gates fail closed.
    source_column_names: dict = {}
    source_table_ids: dict = {}
    col_ids = {m.source_column_id for m in variant_measures if m.source_column_id}
    if col_ids:
        result = await db.execute(
            select(ModelColumn).where(ModelColumn.id.in_(list(col_ids)))
        )
        col_by_id = {c.id: c for c in result.scalars().all()}
        for m in variant_measures:
            col = col_by_id.get(m.source_column_id) if m.source_column_id else None
            if col is None:
                raise VariantContextError(
                    f"variant measure {m.name!r} has no resolvable source column "
                    f"(source_column_id={m.source_column_id!r})"
                )
            source_column_names[m.id] = col.column_name
            source_table_ids[m.id] = col.model_table_id

    missing = [m.name for m in variant_measures if m.id not in source_column_names]
    if missing:
        raise VariantContextError(
            f"variant measures {missing!r} have no source column snapshot"
        )

    # Bug-8293 [WRONG NUMBERS / fail closed]: an anchor with no physical
    # identity is UNPROVEN. When a variant carries no configured anchor AND
    # the selected time-grain dimension exposes no source column, nothing
    # proves the CTAS window would order by the same physical date as the
    # source route — refuse the CTAS and serve the variant from source.
    unproven = [
        m.name
        for m in variant_measures
        if not resolve_effective_variant_anchor(m, selected_time_dim).is_proven
    ]
    if unproven:
        raise VariantContextError(
            f"variant measures {unproven!r} have no proven physical "
            f"date-anchor identity (no configured anchor and the selected "
            f"time-grain dimension exposes no source column); they cannot be "
            f"materialised in this CTAS without route-dependent wrong "
            f"numbers. Serve them from source."
        )

    # Bug-8293/F-015-01 [WRONG NUMBERS]: source, CTAS, refresh and serve-time
    # admission consume the same effective-anchor authority.  This covers the
    # window family's date_dimension_column_id and the parallel-period family's
    # resolved_date_col_id (notably pct_change/cagr).  A mismatch means the
    # source route orders by one physical date while the materialised window
    # orders by the selected grain anchor, so fail closed before any CTAS.
    mismatched = [
        m.name
        for m in variant_measures
        if not resolve_effective_variant_anchor(m, selected_time_dim).matches_grain
    ]
    if mismatched:
        raise VariantContextError(
            f"variant measures {mismatched!r} resolve a date anchor that "
            f"differs from the aggregate's selected time-grain column; they "
            f"cannot be materialised in this CTAS without route-dependent "
            f"wrong numbers. Serve them from source."
        )

    calendar_bindings: dict = {}
    for measure in variant_measures:
        calendar_id = getattr(measure, "resolved_calendar_id", None)
        if calendar_id is None:
            continue
        calendar = await db.get(CalendarTable, calendar_id)
        if calendar is None:
            raise VariantContextError(
                f"variant measure {measure.name!r} resolves calendar "
                f"{calendar_id!r}, but that calendar is missing"
            )
        calendar_bindings[measure.id] = _calendar_binding(calendar, measure)

    return VariantContext(
        time_grain_column,
        source_column_names,
        source_table_ids,
        getattr(selected_time_dim, "time_grain", None),
        calendar_bindings,
    )
