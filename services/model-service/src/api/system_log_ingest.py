"""Existing system-admin authority accepts bounded, repeat-safe raw log batches."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config.settings import get_settings
from shared.db.models import SystemLog
from shared.db.session import get_system_db
from shared.system_logs.config import CONFIG
from shared.system_logs.metrics import (
    LOG_EVENTS,
    LOG_ENABLED,
    LOG_FAILURES,
    LOG_HEARTBEAT,
    LOG_TRUNCATED,
    record_error_timestamp,
)
from shared.system_logs.redaction import redact
from shared.system_logs.retention import retention_cutoff
from src.auth.middleware import require_system_admin

router = APIRouter(
    prefix="/admin/system-logs",
    tags=["system logs"],
    dependencies=[Depends(require_system_admin)],
)
LOG_ENABLED.set(int(get_settings().SYSTEM_LOGS_ENABLED))


class IncomingLog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    timestamp: datetime
    service: str = Field(max_length=64)
    level: str = Field(max_length=16)
    logger: str = Field(default="", max_length=255)
    instance: str = Field(max_length=255)
    message: str = Field(max_length=CONFIG["message_max_chars"])

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Log timestamps must include a timezone")
        if value > datetime.now(timezone.utc) + timedelta(
            seconds=CONFIG["clock_skew_seconds"]
        ):
            raise ValueError("Log timestamp is too far in the future")
        return value

    @field_validator("service", "level")
    @classmethod
    def fixed_vocabulary(cls, value: str, info) -> str:
        if (
            value
            not in CONFIG["services" if info.field_name == "service" else "levels"]
        ):
            raise ValueError("Unsupported log " + info.field_name)
        return value


class LogBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[IncomingLog] = Field(max_length=CONFIG["batch_size"])
    overflows: int = Field(default=0, ge=0, le=1)


@router.post("/ingest")
async def ingest_logs(batch: LogBatch, db: AsyncSession = Depends(get_system_db)):
    if not get_settings().SYSTEM_LOGS_ENABLED:
        raise HTTPException(409, "System log collection is disabled")
    cutoff = retention_cutoff()
    rows = []
    for item in batch.items:
        if cutoff is not None and item.timestamp < cutoff:
            continue
        row = item.model_dump()
        row["message"] = redact(item.message)
        row["logger"] = redact(item.logger)[:255]
        row["instance"] = redact(item.instance)[:255]
        rows.append(row)
    try:
        inserted = []
        if rows:
            statement = (
                insert(SystemLog)
                .values(rows)
                .on_conflict_do_nothing(index_elements=[SystemLog.id])
            )
            inserted = (
                await db.execute(
                    statement.returning(
                        SystemLog.service, SystemLog.level, SystemLog.timestamp
                    )
                )
            ).all()
        await db.commit()
    except Exception:
        await db.rollback()
        LOG_FAILURES.inc()
        raise HTTPException(
            503, "System logs could not be stored; the collector will retry"
        ) from None
    for service, level, timestamp in inserted:
        LOG_EVENTS.labels(service, level).inc()
        if level in {"ERROR", "CRITICAL"}:
            record_error_timestamp(timestamp.timestamp())
    LOG_HEARTBEAT.set(time.time())
    LOG_TRUNCATED.inc(batch.overflows)
    return {"stored": len(inserted), "accepted": len(batch.items)}
