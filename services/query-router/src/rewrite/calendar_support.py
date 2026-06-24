"""Calendar / time-dimension support for the query rewriter.

Time-dimension detection, calendar binding resolution, calendar column mapping,
hierarchy calendar-rule lookup, the semi-additive grain rank, and semi-additive
aggregation rendering.

Extracted from query_rewriter.py (Phase 3 decomposition); behaviour-identical.
"""
from __future__ import annotations

import inspect
from typing import Any

import sqlglot
from sqlglot import exp

from shared.semantic.calendar_types import normalize_calendar_type
from src.ir.logical_query import SemanticBindingError


# Map CalendarTable physical column attributes to the keys the time-variant
# emitter expects in VariantBinding.calendar_columns.
_CALENDAR_COLUMN_ATTRS: tuple[tuple[str, str], ...] = (
    ("date", "date_column"),
    ("year", "year_column"),
    ("half", "half_column"),
    ("quarter", "quarter_column"),
    ("month", "month_column"),
    ("week", "week_column"),
    ("day", "day_column"),
)


def _is_time_dimension(d: object) -> bool:
    """Check whether a resolved dimension represents a time dimension.

    ORM Dimension objects carry ``is_time_dim`` (bool).  Virtual
    dimensions produced by ``_load_hierarchy_level_dimensions`` may
    carry ``dimension_kind``.  This helper checks both so the
    rewriter works with either representation.
    """
    if getattr(d, "is_time_dim", False):
        return True
    return getattr(d, "dimension_kind", None) == "time"


_SA_GRAIN_RANK = {"day": 0, "week": 1, "month": 2, "quarter": 3, "half": 4, "year": 5}


# F-016-04 / F-016-09: the expression-vs-table decision lives in
# shared.semantic.calendar_dialects (single source of truth). Imported lazily
# inside the helper to keep this module's import graph light and to mirror the
# rest of calendar_support's lazy ORM imports.
def _calendar_type_is_expression_capable(calendar_type: str | None) -> bool:
    """True when *calendar_type*'s period boundaries can be computed with
    date arithmetic alone (no physical calendar table required).

    A None / unknown type is treated as expression-capable: None means
    'standard' (Gregorian), and an unknown token would already have been
    rejected at hierarchy-save time. The canonical set covers standard,
    fiscal, iso_week and thai_buddhist; retail_445 and hijri require a bound
    calendar table.
    """
    from shared.semantic.calendar_dialects import (
        EXPRESSION_CAPABLE_CALENDAR_TYPES,
        TABLE_BOUND_CALENDAR_TYPES,
    )

    norm = normalize_calendar_type(calendar_type) or "standard"
    if norm in TABLE_BOUND_CALENDAR_TYPES:
        return False
    # Expression-capable when explicitly listed; anything else (unknown,
    # already-validated) defaults to expression-capable.
    return norm in EXPRESSION_CAPABLE_CALENDAR_TYPES or norm not in TABLE_BOUND_CALENDAR_TYPES


def _semi_additive_agg(
    behavior: str, col_expr: str, time_expr: str, target_dialect: str = "postgres",
) -> str:
    """Build a semi-additive aggregation expression, transpiled to *target_dialect*.

    Architectural note (Bug-906): the expression is first built in PostgreSQL
    syntax (``pg``), then transpiled via SQLGlot for non-postgres targets.
    The early return for ``target_dialect == "postgres"`` skips a redundant
    parse-and-emit round-trip; it is not dialect branching without
    transpilation.
    """
    b = behavior.lower()
    if b == "by_account":
        # F-015-06: by_account is rejected upstream (source_sql) because it
        # needs per-account dispatch the flat GROUP BY cannot express. Reaching
        # here means the upstream guard was bypassed; fail loud rather than
        # emit a silently-wrong plain last-non-empty (the account column is
        # not consulted here).
        raise SemanticBindingError(
            "by_account semi-additive behaviour is not supported at query "
            "time (per-account aggregation dispatch is unimplemented)."
        )
    if b == "last_non_empty":
        pg = (
            f"(ARRAY_AGG({col_expr} ORDER BY {time_expr} DESC)"
            f" FILTER (WHERE {col_expr} IS NOT NULL))[1]"
        )
    elif b == "first_non_empty":
        pg = (
            f"(ARRAY_AGG({col_expr} ORDER BY {time_expr} ASC)"
            f" FILTER (WHERE {col_expr} IS NOT NULL))[1]"
        )
    elif b == "avg_of_children":
        pg = f"AVG({col_expr})"
    elif b == "min":
        pg = f"MIN({col_expr})"
    elif b == "max":
        pg = f"MAX({col_expr})"
    else:
        pg = f"SUM({col_expr})"

    # Optimisation: skip parse-and-emit for the postgres target.
    if target_dialect == "postgres":
        return pg

    tree = sqlglot.parse_one(pg, read="postgres")
    for node in tree.walk():
        if isinstance(node, exp.Bracket) and node.args.get("offset") is None:
            node.set("offset", 0)
    return tree.sql(dialect=target_dialect)



async def _resolve_calendar_binding(db: Any, period_aware_measures: list[Any]) -> Any | None:
    """
    Resolve the CalendarTable used by a query's period-aware measures.

    New path (denormalized snapshot): Measure.resolved_calendar_id → CalendarTable
    in one hop. The snapshot is set at variant creation time.

    Legacy fallback: Measure.calendar_model_table_id → ModelTable.calendar_table_id
    → CalendarTable (for variants created before the multi-calendar migration).

    Returns None if no measure has a calendar pinned. Raises
    SemanticBindingError when measures in the same query disagree on
    which calendar to use.
    """
    from shared.db.models import CalendarTable, ModelTable

    resolved_cal_ids: set[Any] = set()
    legacy_alias_ids: set[Any] = set()

    for m in period_aware_measures:
        rcal = getattr(m, "resolved_calendar_id", None)
        if rcal is not None:
            resolved_cal_ids.add(rcal)
        else:
            cal_id = getattr(m, "calendar_model_table_id", None)
            if cal_id is not None:
                legacy_alias_ids.add(cal_id)

    if resolved_cal_ids:
        if len(resolved_cal_ids) > 1:
            raise SemanticBindingError(
                "Period-aware measures in this query reference more than one "
                "calendar; split the query so each pivot uses a single calendar."
            )
        cal_id = next(iter(resolved_cal_ids))
        calendar = await db.get(CalendarTable, cal_id)
        if inspect.isawaitable(calendar):
            calendar = await calendar
        return calendar

    if not legacy_alias_ids:
        return None
    if len(legacy_alias_ids) > 1:
        raise SemanticBindingError(
            "Period-aware measures in this query reference more than one "
            "calendar alias; split the query so each pivot uses a single "
            "calendar."
        )
    alias_id = next(iter(legacy_alias_ids))
    alias_table = await db.get(ModelTable, alias_id)
    if inspect.isawaitable(alias_table):
        alias_table = await alias_table
    if alias_table is None or alias_table.calendar_table_id is None:
        return None
    calendar = await db.get(CalendarTable, alias_table.calendar_table_id)
    if inspect.isawaitable(calendar):
        calendar = await calendar
    return calendar


def _build_calendar_columns(calendar: Any) -> dict[str, str]:
    """Convert a CalendarTable row to the calendar_columns dict expected
    by the time-variant emitter. Skips attributes that are NULL."""
    return {
        key: value
        for key, attr in _CALENDAR_COLUMN_ATTRS
        if (value := getattr(calendar, attr, None))
    }


async def _resolve_hierarchy_calendar_rules(
    db: Any, time_dim: Any, model_id: Any,
) -> tuple[str, int | None]:
    """Load calendar_type and fiscal_year_start_month from the time
    hierarchy that owns the query's time dimension.

    Resolution order (F-016-03):

    1. ``time_dim.hierarchy_id`` — virtual level dimensions produced by
       ``load_hierarchy_level_dimensions`` carry the parent hierarchy id
       directly. Generated date hierarchies key their levels on UDAs, so
       ``source_column_id`` is None and the legacy column-match below can
       never resolve them — this is the production-default configuration and
       must be honoured, otherwise fiscal / ISO / Hijri configuration is
       silently ignored and Gregorian numbers are returned.
    2. ``time_dim.source_column_id`` — plain (non-hierarchy) date dimensions
       resolve by matching a physical-column hierarchy level.

    Returns ('standard', None) when no explicit calendar_type is set. The
    returned type is normalised to the canonical vocabulary so a legacy
    ``iso`` token aligns with an ``iso_week`` calendar table (F-016-04).
    """
    from sqlalchemy import select as sa_select
    from shared.db.models import HierarchyDefinition, HierarchyLevel

    # Path 1: resolve straight off the hierarchy the dimension belongs to.
    hierarchy_id = getattr(time_dim, "hierarchy_id", None)
    if hierarchy_id is not None:
        result = await db.execute(
            sa_select(
                HierarchyDefinition.calendar_type,
                HierarchyDefinition.fiscal_year_start_month,
            ).where(HierarchyDefinition.id == hierarchy_id)
        )
        row = result.first()
        if row is not None:
            return (normalize_calendar_type(row[0]) or "standard", row[1])
        # The dimension names a hierarchy that no longer exists; fall through
        # to the column match rather than guessing.

    # Path 2: plain date dimension — match a physical-column hierarchy level.
    src_col_id = getattr(time_dim, "source_column_id", None)
    if src_col_id is None:
        return ("standard", None)

    result = await db.execute(
        sa_select(
            HierarchyDefinition.calendar_type,
            HierarchyDefinition.fiscal_year_start_month,
        )
        .join(HierarchyLevel, HierarchyLevel.hierarchy_id == HierarchyDefinition.id)
        .where(
            HierarchyDefinition.model_id == model_id,
            HierarchyDefinition.dimension_kind == "time",
            HierarchyLevel.key_attribute_source == "physical_column",
            HierarchyLevel.key_attribute_id == src_col_id,
        )
        .limit(1)
    )
    row = result.first()
    if row is None:
        return ("standard", None)
    return (normalize_calendar_type(row[0]) or "standard", row[1])

