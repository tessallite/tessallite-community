"""Retention purge for audit events and query logs.

Deletes records older than the tenant-configured retention threshold.
A retention of 0 means indefinite — no purge runs for that tenant.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config.resolver import get_setting
from shared.db.models import AuditEvent, QueryLog, QueryMissLog, RouteLog

logger = logging.getLogger(__name__)


async def purge_audit_events(db: AsyncSession) -> int:
    # Resolve through the settings registry (tenant -> system -> default) so
    # the value is read with the same shape it was written with (a raw scalar
    # int in TenantSetting.value_json) and coerced via the registry. The old
    # direct read assumed a dict shape, so an int row failed the type check and
    # an explicit 0 ("indefinite") failed the truthiness check — both silently
    # fell back to the default, purging data the tenant marked indefinite
    # (F-012-05).
    retention_days = await get_setting("audit.retention_days", tenant_session=db)

    # 0 means indefinite — never purge (registry: "0 for indefinite retention").
    if retention_days == 0:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    stmt = delete(AuditEvent).where(AuditEvent.timestamp < cutoff)
    result = await db.execute(stmt)
    await db.commit()
    deleted = result.rowcount or 0
    if deleted:
        logger.info("Purged %d audit events older than %d days", deleted, retention_days)
    return deleted


async def purge_query_logs(db: AsyncSession) -> int:
    retention_days = await get_setting("query_log.retention_days", tenant_session=db)

    # 0 means indefinite — never purge.
    if retention_days == 0:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    expired_ids = select(QueryLog.id).where(QueryLog.created_at < cutoff)
    route_stmt = delete(RouteLog).where(RouteLog.query_log_id.in_(expired_ids))
    await db.execute(route_stmt)

    stmt = delete(QueryLog).where(QueryLog.created_at < cutoff)
    result = await db.execute(stmt)
    await db.commit()
    deleted = result.rowcount or 0
    if deleted:
        logger.info("Purged %d query logs older than %d days", deleted, retention_days)
    return deleted


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
    stmt = delete(QueryMissLog).where(QueryMissLog.last_seen_at < cutoff)
    result = await db.execute(stmt)
    await db.commit()
    deleted = result.rowcount or 0
    if deleted:
        logger.info("Purged %d query miss logs older than %d days", deleted, retention_days)
    return deleted
