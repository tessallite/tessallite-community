"""System-admin-only raw log search, settings and expired-record purge."""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config.settings import get_settings
from shared.db.models import SystemLog
from shared.db.session import get_system_db
from shared.system_logs.config import CONFIG
from shared.system_logs.retention import purge_expired_logs, retention_cutoff
from src.auth.middleware import require_system_admin

router = APIRouter(
    prefix="/admin/system-logs",
    tags=["system logs"],
    dependencies=[Depends(require_system_admin)],
)
logger = logging.getLogger(__name__)


class LogRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    timestamp: datetime
    service: str
    level: str
    logger: str
    instance: str
    message: str


class LogPage(BaseModel):
    items: list[LogRow]
    next_cursor: str | None


def _aware(value: datetime | None) -> None:
    if value is not None and value.utcoffset() is None:
        raise HTTPException(422, "Dates must include a timezone")


@router.get("/settings")
async def settings():
    s = get_settings()
    return {
        "enabled": s.SYSTEM_LOGS_ENABLED,
        "retention_days": s.SYSTEM_LOG_RETENTION_DAYS,
        "cutoff": retention_cutoff(),
        "levels": CONFIG["levels"],
        "services": CONFIG["services"],
        "poll_seconds": CONFIG["poll_seconds"],
        "page_size": CONFIG["page_size"],
    }


@router.get("", response_model=LogPage)
async def list_logs(
    q: str = Query("", max_length=500),
    service: str | None = Query(None, max_length=64),
    level: str | None = Query(None, max_length=16),
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    cursor: str | None = Query(None, max_length=250),
    limit: int = Query(CONFIG["page_size"], ge=1, le=200),
    db: AsyncSession = Depends(get_system_db),
) -> LogPage:
    _aware(from_date)
    _aware(to_date)
    if from_date and to_date and from_date > to_date:
        raise HTTPException(422, "Start must not be after end")
    if level and level not in CONFIG["levels"]:
        raise HTTPException(422, "Unknown severity")
    if service and service not in CONFIG["services"]:
        raise HTTPException(422, "Unknown service")
    stmt = select(SystemLog)
    if q:
        pattern = (
            "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        )
        stmt = stmt.where(
            or_(
                *(
                    col.ilike(pattern, escape="\\")
                    for col in (SystemLog.message, SystemLog.logger, SystemLog.instance)
                )
            )
        )
    if service:
        stmt = stmt.where(SystemLog.service == service)
    if level:
        stmt = stmt.where(SystemLog.level == level)
    if from_date:
        stmt = stmt.where(SystemLog.timestamp >= from_date)
    if to_date:
        stmt = stmt.where(SystemLog.timestamp <= to_date)
    if cursor:
        try:
            pair = json.loads(base64.urlsafe_b64decode(cursor))
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not all(isinstance(value, str) for value in pair)
            ):
                raise ValueError("Cursor must contain a timestamp and ID")
            timestamp, row_id = pair
            timestamp, row_id = datetime.fromisoformat(timestamp), UUID(row_id)
            _aware(timestamp)
        except (ValueError, TypeError, KeyError):
            raise HTTPException(422, "Invalid log cursor") from None
        stmt = stmt.where(
            or_(
                SystemLog.timestamp < timestamp,
                and_(SystemLog.timestamp == timestamp, SystemLog.id < row_id),
            )
        )
    rows = list(
        (
            await db.scalars(
                stmt.order_by(SystemLog.timestamp.desc(), SystemLog.id.desc()).limit(
                    limit + 1
                )
            )
        ).all()
    )
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = base64.urlsafe_b64encode(
            json.dumps([last.timestamp.isoformat(), str(last.id)]).encode()
        ).decode()
    return LogPage(
        items=[LogRow.model_validate(row) for row in rows[:limit]],
        next_cursor=next_cursor,
    )


@router.post("/purge")
async def purge_logs(db: AsyncSession = Depends(get_system_db)):
    cutoff = retention_cutoff()
    try:
        deleted = await purge_expired_logs(db, cutoff)
    except Exception:
        # ``exc_info`` is the point of this branch. The caller is told to retry,
        # which is right for a transient database fault and useless for anything
        # else, so the operator needs the cause in the log to tell the two
        # apart. Without it a permanent failure reads as a transient one and
        # leaves nothing to diagnose. The response body stays generic: the
        # exception text can carry schema and statement detail.
        logger.error(
            "System log purge failed; expired records could not all be removed",
            exc_info=True,
        )
        raise HTTPException(503, "Log purge failed; refresh and retry") from None
    logger.warning(
        "System administrator purged %d expired log records; cutoff=%s", deleted, cutoff
    )
    return {"deleted": deleted, "cutoff": cutoff}
