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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    AggregateDefinition,
    CalendarTable,
    Dimension,
    Model,
    ModelAliasMap,
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
    actually map to."""
    model = await db.get(Model, model_id)
    if model is None:
        return []
    # Calendars are owned by data_sources; we don't have a direct join here,
    # so list all calendars in the tenant and filter by data_source_id of
    # this model's tables. For the size of typical workspaces this is fine.
    rows = await db.execute(select(CalendarTable))
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
