"""Audit event writer with severity-level gating.

Usage:
    from shared.audit.logger import audit

    await audit(
        db,
        action="model.deploy",
        severity="warn",
        actor_id=user.id,
        actor_email=user.email,
        target_type="model",
        target_id=model.id,
        target_name=model.display_name,
        detail={"version": version_number},
        ip_address=request_ip,
    )

Severity levels (ascending):
    critical — auth failures, security changes, destructive deletes
    warn     — deploy/undeploy, user changes, settings changes
    info     — all CRUD, query execution, connection changes

The tenant-level setting ``audit.log_level`` gates which events are written.
An event is written only if its severity >= the configured level. ``off``
suppresses all writes.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import AuditEvent, TenantSetting

logger = logging.getLogger(__name__)

_SEVERITY_RANK = {
    "off": -1,
    "critical": 3,
    "warn": 2,
    "info": 1,
}


async def _get_audit_level(db: AsyncSession) -> str:
    from sqlalchemy import select
    result = await db.execute(
        select(TenantSetting.value_json).where(TenantSetting.key == "audit.log_level")
    )
    row = result.scalar_one_or_none()
    if row and isinstance(row, dict):
        return row.get("value", "info")
    return "info"


def _should_log(event_severity: str, configured_level: str) -> bool:
    if configured_level == "off":
        return False
    # F-022-10: an unknown/misspelled severity (e.g. "warning" instead of
    # "warn") must NOT be silently dropped. Treat it as the most severe level
    # so a call-site typo always produces a recorded event rather than a
    # vanished one — audit completeness fails closed (never drop).
    if event_severity not in _SEVERITY_RANK or event_severity == "off":
        logger.warning(
            "audit() called with unrecognised severity %r — recording it as "
            "critical so the event is never silently dropped. Fix the call site "
            "to use one of: critical, warn, info.",
            event_severity,
        )
        event_rank = _SEVERITY_RANK["critical"]
    else:
        event_rank = _SEVERITY_RANK[event_severity]
    threshold_rank = _SEVERITY_RANK.get(configured_level, 1)
    return event_rank >= threshold_rank


async def audit(
    db: AsyncSession,
    *,
    action: str,
    severity: str,
    actor_id: Optional[uuid.UUID] = None,
    actor_email: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[uuid.UUID] = None,
    target_name: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
    ip_address: Optional[str] = None,
) -> Optional[AuditEvent]:
    try:
        level = await _get_audit_level(db)
        if not _should_log(severity, level):
            return None

        event = AuditEvent(
            id=uuid.uuid4(),
            timestamp=datetime.now(timezone.utc),
            actor_id=actor_id,
            actor_email=actor_email,
            action=action,
            target_type=target_type,
            target_id=target_id,
            target_name=target_name,
            severity=severity,
            detail=detail,
            ip_address=ip_address,
        )
        db.add(event)
        await db.flush()
        return event
    except Exception:
        logger.exception("Failed to write audit event: action=%s", action)
        return None
