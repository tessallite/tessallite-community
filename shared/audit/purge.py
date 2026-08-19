"""Retention purge for audit events and query logs.

Deletes records older than the tenant-configured retention threshold.
A retention of 0 means indefinite -- no purge runs for that tenant.

Bug-7335: purges now use the same bounded-batch pattern as the webhook
retention purge (``shared/webhooks/retention.py``): id-batch SELECT +
bounded DELETE + per-batch COMMIT, so a single sweep never opens an
unbounded DELETE that locks the table or builds a huge transaction.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config.resolver import get_setting
from shared.db.models import (
    AuditEvent,
    KPIUsage,
    NamedSetUsage,
    QueryLog,
    QueryMissLog,
    RouteLog,
)

logger = logging.getLogger(__name__)

# Batch parameters matching the webhook retention pattern.
_BATCH_SIZE = 5000
_MAX_BATCHES = 20


async def _batched_purge(
    db: AsyncSession,
    model,
    timestamp_col,
    cutoff: datetime,
    *,
    label: str,
    retention_days: int,
    pre_delete_fn=None,
) -> int:
    """Bug-7335: generic batched purge for any time-stamped table.

    Selects up to ``_BATCH_SIZE`` primary-key IDs per batch, deletes them
    in a bounded DELETE, and commits after each batch. Runs up to
    ``_MAX_BATCHES`` batches per call so a single sweep never opens an
    unbounded transaction. Returns the total number of rows deleted.

    ``pre_delete_fn``, if provided, is called with ``(db, ids)`` before
    the main delete -- used for child-table cleanup (e.g. RouteLog rows
    referencing QueryLog).
    """
    total = 0
    for _ in range(_MAX_BATCHES):
        ids = (
            await db.execute(
                select(model.id)
                .where(timestamp_col < cutoff)
                .limit(_BATCH_SIZE)
            )
        ).scalars().all()
        if not ids:
            break

        if pre_delete_fn is not None:
            await pre_delete_fn(db, ids)

        result = await db.execute(
            sa_delete(model).where(model.id.in_(ids))
        )
        await db.commit()
        deleted = getattr(result, "rowcount", None)
        total += deleted if deleted is not None and deleted >= 0 else len(ids)
        if len(ids) < _BATCH_SIZE:
            break

    if total:
        logger.info("Purged %d %s older than %d days", total, label, retention_days)
    return total


async def purge_audit_events(db: AsyncSession) -> int:
    retention_days = await get_setting("audit.retention_days", tenant_session=db)

    # 0 means indefinite -- never purge (registry: "0 for indefinite retention").
    if retention_days == 0:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    total = await _batched_purge(
        db, AuditEvent, AuditEvent.timestamp, cutoff,
        label="audit events", retention_days=retention_days,
    )
    if total:
        from shared.audit.logger import audit_required
        await audit_required(
            db,
            action="audit.purge",
            severity="warn",
            detail={"deleted": total, "retention_days": retention_days},
        )
        await db.commit()
    return total


async def purge_query_logs(db: AsyncSession) -> int:
    retention_days = await get_setting("query_log.retention_days", tenant_session=db)

    # 0 means indefinite -- never purge.
    if retention_days == 0:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    async def _delete_route_logs(db_: AsyncSession, query_log_ids) -> None:
        """Remove child RouteLog rows before deleting their parent QueryLogs."""
        await db_.execute(
            sa_delete(RouteLog).where(RouteLog.query_log_id.in_(query_log_ids))
        )

    return await _batched_purge(
        db, QueryLog, QueryLog.created_at, cutoff,
        label="query logs", retention_days=retention_days,
        pre_delete_fn=_delete_route_logs,
    )


async def purge_query_miss_logs(db: AsyncSession) -> int:
    """Purge miss-log rows whose last_seen_at predates the query-log retention.

    F-030-17: miss rows dedupe by fingerprint so their count is bounded by
    distinct query shapes, but stale shapes from deleted dashboards otherwise
    persist forever and keep influencing the optimizer's last_seen_at ordering.
    They share the ``query_log.retention_days`` setting (same observability data
    class); a retention of 0 means indefinite, matching the other purges.
    """
    retention_days = await get_setting("query_log.retention_days", tenant_session=db)

    if retention_days == 0:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    return await _batched_purge(
        db, QueryMissLog, QueryMissLog.last_seen_at, cutoff,
        label="query miss logs", retention_days=retention_days,
    )


async def purge_entity_usage(db: AsyncSession) -> int:
    """Purge kpi_usage / named_set_usage rows older than the retention window.

    Bug-8382: both tables accrue ONE ROW PER USAGE REPORT — every Excel
    scorecard insert and every frontend usage ping — and nothing ever removed
    them. Unlike ``kpi_snapshots`` (purged to each KPI's ``snapshot_retention``)
    they had no lifecycle at all, so on a mature tenant they reach tens of
    thousands of rows per model. That became more than a storage concern once a
    model revert started CAPTURING and REINSERTING these rows inside the
    transaction that holds the per-model advisory lock (Bug-7982 preservation):
    unbounded growth turns a revert into a long lock-holding operation that
    blocks every other write to the model.

    They share ``query_log.retention_days`` — the same setting and the same
    reasoning as ``purge_query_miss_logs``: this is observability telemetry
    about how entities are used, not model configuration, and giving it its own
    knob would add a second retention control an admin has to reason about for
    no behavioural gain. A retention of 0 means indefinite, matching every
    other purge here.

    Returns the total rows deleted across both tables.
    """
    retention_days = await get_setting("query_log.retention_days", tenant_session=db)

    # 0 means indefinite -- never purge.
    if retention_days == 0:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    total = await _batched_purge(
        db, KPIUsage, KPIUsage.reported_at, cutoff,
        label="KPI usage reports", retention_days=retention_days,
    )
    total += await _batched_purge(
        db, NamedSetUsage, NamedSetUsage.reported_at, cutoff,
        label="named-set usage reports", retention_days=retention_days,
    )
    return total
