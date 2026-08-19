"""Auto-derived per-model context fields (Phase Agent-B4.6).

Three fields on `project_agent_model_contexts` are computed from the
semantic-layer state, not hand-edited:

  - aggregates_summary: a compact list of [{name, grain, status}] entries
  - calendar_aliases:   shape of any calendar table the model joins
  - dimension_aliases:  ModelAliasMap pairs already authored against the
                        model's dimensions (alias -> canonical name)

The model-service publishes / undeploys models through versions.py; the
agent-service exposes a refresh endpoint which the publish flow can
call. Until the publish-side hook is wired, the prompt assembler can
also call `derive_model_context` directly when `derived_at` is null.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    AggregateDefinition,
    CalendarTable,
    Dimension,
    Model,
    ModelAliasMap,
    ModelTable,
    ProjectAgentModelContext,
)

logger = logging.getLogger(__name__)


async def _aggregates_summary(
    db: AsyncSession, model_id: UUID
) -> list[dict[str, Any]]:
    rows = await db.execute(
        select(AggregateDefinition).where(
            AggregateDefinition.model_id == model_id,
            AggregateDefinition.retired_at.is_(None),
        )
    )
    out: list[dict[str, Any]] = []
    for agg in rows.scalars().all():
        out.append(
            {
                "name": agg.physical_table_name,
                "status": agg.status,
                "grain": list(agg.grain or []),
                "creation_reason": agg.creation_reason,
            }
        )
    return out


async def _calendar_aliases(
    db: AsyncSession, model_id: UUID
) -> list[dict[str, Any]]:
    """Find calendar tables reachable from the model's data sources and
    return their column-shape so the LLM understands what 'month'/'quarter'
    actually map to.

    Bug-5956 — previously this function loaded ALL calendar tables in the
    tenant, causing cross-model alias leakage.  Now scoped to calendars
    that are either (a) explicitly linked via ModelTable.calendar_table_id,
    or (b) owned by data sources used by the model's tables."""
    model = await db.get(Model, model_id)
    if model is None:
        return []

    # Collect calendar IDs explicitly linked via model tables AND the
    # data source IDs of the model's tables (for calendar tables that are
    # not explicitly linked but share the same data source).
    mt_rows = await db.execute(
        select(ModelTable.calendar_table_id, ModelTable.source_id).where(
            ModelTable.model_id == model_id,
        )
    )
    explicit_cal_ids: set[UUID] = set()
    model_source_ids: set[UUID] = set()
    for cal_id, source_id in mt_rows.all():
        if cal_id is not None:
            explicit_cal_ids.add(cal_id)
        if source_id is not None:
            model_source_ids.add(source_id)

    if not explicit_cal_ids and not model_source_ids:
        return []

    # Build a combined filter: calendars explicitly linked OR owned by
    # any of the model's data sources.
    conditions = []
    if explicit_cal_ids:
        conditions.append(CalendarTable.id.in_(explicit_cal_ids))
    if model_source_ids:
        conditions.append(CalendarTable.data_source_id.in_(model_source_ids))

    rows = await db.execute(
        select(CalendarTable).where(or_(*conditions))
    )
    out: list[dict[str, Any]] = []
    for cal in rows.scalars().all():
        out.append(
            {
                "table_name": cal.table_name,
                "dialect": cal.dialect,
                "date_column": cal.date_column,
                "year_column": cal.year_column,
                "half_column": cal.half_column,
                "quarter_column": cal.quarter_column,
                "month_column": cal.month_column,
                "week_column": cal.week_column,
                "day_column": cal.day_column,
            }
        )
    return out


async def _dimension_aliases(
    db: AsyncSession, model_id: UUID
) -> list[dict[str, Any]]:
    rows = await db.execute(
        select(ModelAliasMap).where(ModelAliasMap.model_id == model_id)
    )
    out: list[dict[str, Any]] = []
    for row in rows.scalars().all():
        for phrase, canonical in (row.alias_map or {}).items():
            out.append({"alias": phrase, "canonical": canonical})
    # Also surface base dimensions so the LLM knows what's queryable.
    dim_rows = await db.execute(
        select(Dimension).where(
            Dimension.model_id == model_id,
            Dimension.is_invalid.is_(False),
        )
    )
    for d in dim_rows.scalars().all():
        out.append({"alias": d.name, "canonical": d.name, "is_base": True})
    return out


async def derive_model_context(
    db: AsyncSession,
    project_id: UUID,
    model_id: UUID,
    persist: bool = True,
) -> ProjectAgentModelContext | None:
    """Recompute the three auto-derived fields. Returns the upserted row.
    Returns None if the (project, model) is not allow-listed (no row to
    write into and we don't create one — derivation only refreshes
    existing context rows)."""
    record = await db.get(ProjectAgentModelContext, (project_id, model_id))
    if record is None:
        logger.warning(
            "No ProjectAgentModelContext row for project_id=%s model_id=%s — "
            "derivation skipped; the planner will operate with empty "
            "aggregates/calendar/alias context for this model.",
            project_id,
            model_id,
        )
        return None

    record.aggregates_summary = await _aggregates_summary(db, model_id)
    record.calendar_aliases = await _calendar_aliases(db, model_id)
    record.dimension_aliases = await _dimension_aliases(db, model_id)
    record.derived_at = datetime.now(timezone.utc)
    if persist:
        await db.commit()
        await db.refresh(record)
    return record


async def derive_all_for_project(
    db: AsyncSession, project_id: UUID
) -> list[ProjectAgentModelContext]:
    """Refresh every (project, model) context row for this project."""
    rows = await db.execute(
        select(ProjectAgentModelContext).where(
            ProjectAgentModelContext.project_id == project_id,
        )
    )
    out: list[ProjectAgentModelContext] = []
    for ctx in rows.scalars().all():
        refreshed = await derive_model_context(
            db, ctx.project_id, ctx.model_id, persist=False
        )
        if refreshed is not None:
            out.append(refreshed)
    await db.commit()
    return out
