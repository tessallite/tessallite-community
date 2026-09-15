"""One retention cutoff and bounded purge for scheduler and manual actions."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config.settings import get_settings
from shared.db.models import SystemLog
from shared.system_logs.config import CONFIG
from shared.system_logs.metrics import LOG_PURGED


def retention_cutoff(now: datetime | None = None) -> datetime | None:
    days = get_settings().SYSTEM_LOG_RETENTION_DAYS
    return (now or datetime.now(timezone.utc)) - timedelta(days=days) if days else None


async def purge_expired_logs(db: AsyncSession, cutoff: datetime | None) -> int:
    if cutoff is None:
        return 0
    total = 0
    for _ in range(CONFIG["purge_max_batches"]):
        ids = list(
            (
                await db.scalars(
                    select(SystemLog.id)
                    .where(SystemLog.timestamp < cutoff)
                    .order_by(SystemLog.timestamp, SystemLog.id)
                    .limit(CONFIG["purge_batch_size"])
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        if not ids:
            await db.rollback()
            break
        result = await db.execute(delete(SystemLog).where(SystemLog.id.in_(ids)))
        await db.commit()
        total += result.rowcount
        LOG_PURGED.inc(result.rowcount)
        if len(ids) < CONFIG["purge_batch_size"]:
            break
    return total
