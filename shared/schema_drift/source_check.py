"""Live source reachability check for a model (Bug-8484).

The Model Health tab's "Re-check model" button must do more than replay the
last scheduler scan: it must actually contact the source. This module provides
``queue_model_source_check``, a routed liveness probe over every source table a
model binds. Each probe runs THROUGH the query-router (never a direct source
connection — gateway-only access), so a manual re-check reflects the live
source rather than only cached metadata.

A confirmed-absent table is recorded as a durable ``table_removed``
``SchemaChangeEvent`` (deduped against an already-open one) so the signal
survives and the scheduler's deeper column diff picks it up on its next pass.
The return value tells the caller whether the live source was actually
contacted, which the revalidate endpoint stamps onto ``live_source_checked``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.connection_scope import (
    CrossProjectConnectionError,
    assert_connection_in_project,
)
from shared.db.models import DataSource, Model, ModelTable, SchemaChangeEvent
from shared.source_table_probe import (
    SourceProbeUnavailableError,
    SourceTableNotFoundError,
    verify_source_table_exists,
)

logger = logging.getLogger(__name__)


async def queue_model_source_check(
    db: AsyncSession,
    *,
    model_id,
    tenant_id: str = "",
) -> bool:
    """Probe every source table of ``model_id`` against the live source.

    Returns ``True`` when at least one table was actually contacted through the
    routed probe (the live source WAS checked), ``False`` when no table was
    reachable or the model binds no source. A confirmed-absent table is
    persisted as a durable ``table_removed`` ``SchemaChangeEvent``; the caller
    owns the commit.
    """
    model = await db.get(Model, model_id)
    if model is None:
        return False

    tables_result = await db.execute(
        select(ModelTable)
        .options(
            selectinload(ModelTable.source).selectinload(DataSource.project_connection)
        )
        .where(ModelTable.model_id == model_id)
    )

    contacted = False
    now = datetime.now(timezone.utc)
    for table in tables_result.scalars().all():
        source = table.source
        conn = source.project_connection if source is not None else None
        if conn is None:
            continue
        # Fail-closed cross-project guard: never probe a live source through a
        # connection that belongs to a different project than the owning model.
        try:
            assert_connection_in_project(conn, model.project_id)
        except CrossProjectConnectionError:
            logger.error(
                "Source check: skipping table %s — its connection belongs to a "
                "different project than model %s (rejected fail-closed)",
                table.physical_name, model_id,
            )
            continue
        try:
            await verify_source_table_exists(
                table.physical_name, conn,
                model_id=model_id, source_id=table.source_id, tenant_id=tenant_id,
            )
            contacted = True
        except SourceTableNotFoundError:
            # An authoritative "absent" answer IS live contact with the source.
            contacted = True
            await _record_missing_table(db, table, now)
        except SourceProbeUnavailableError as exc:
            # No authoritative answer — do not claim the live source was checked
            # and do not raise a false drift alarm; the scheduler retries.
            logger.warning(
                "Source check: probe unavailable for table %s: %s",
                table.physical_name, exc,
            )
        except Exception as exc:  # noqa: BLE001 — a probe fault must not break the re-check
            logger.warning(
                "Source check: probe failed for table %s: %s",
                table.physical_name, exc,
            )
    return contacted


async def _record_missing_table(db: AsyncSession, table, now: datetime) -> None:
    """Persist a deduped ``table_removed`` event for a confirmed-absent table."""
    existing = await db.execute(
        select(SchemaChangeEvent.id)
        .where(
            SchemaChangeEvent.model_id == table.model_id,
            SchemaChangeEvent.source_id == table.source_id,
            SchemaChangeEvent.table_name == table.physical_name,
            SchemaChangeEvent.change_type == "table_removed",
            SchemaChangeEvent.acknowledged_at.is_(None),
        )
        .limit(1)
    )
    if existing.scalar_one_or_none() is not None:
        return
    db.add(
        SchemaChangeEvent(
            model_id=table.model_id,
            source_id=table.source_id,
            table_name=table.physical_name,
            change_type="table_removed",
            is_breaking=True,
            detail={"reason": "source_table_not_found", "origin": "manual_revalidate"},
            detected_at=now,
        )
    )
    await db.flush()
