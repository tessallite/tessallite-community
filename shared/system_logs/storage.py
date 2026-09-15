"""Measure log storage independently of collection; the scheduler publishes it."""

import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.system_logs.metrics import (
    LOG_BYTES,
    LOG_ROWS,
    LOG_STORAGE_MEASURED_AT,
    record_error_timestamp,
)


async def refresh_storage_metrics(db: AsyncSession) -> None:
    row = (
        await db.execute(
            text("""
                SELECT pg_total_relation_size('tess_system.system_logs'),
                       COALESCE(n_live_tup, 0),
                       (SELECT extract(epoch FROM timestamp)
                        FROM tess_system.system_logs
                        WHERE level IN ('ERROR', 'CRITICAL')
                        ORDER BY timestamp DESC LIMIT 1)
                FROM pg_stat_user_tables
                WHERE schemaname='tess_system' AND relname='system_logs'
            """)
        )
    ).one()
    LOG_BYTES.set(row[0])
    LOG_ROWS.set(row[1])
    if row[2] is not None:
        record_error_timestamp(float(row[2]))
    # Set this only after the complete read succeeds. A zero or stale value
    # lets Prometheus distinguish a measured empty table from no observation.
    LOG_STORAGE_MEASURED_AT.set(time.time())
