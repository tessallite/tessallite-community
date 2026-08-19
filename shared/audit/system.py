"""System-plane audit writer (G-022-01 / F-022-03).

Tenant ``audit_events`` cannot survive tenant-schema drop and cannot record
system-admin actions that have no tenant session. Callers pass the system
DB session and emit **before** ``commit`` so a flush failure rolls back the
mutation.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import SystemAuditEvent


async def system_audit(
    db: AsyncSession,
    *,
    action: str,
    severity: str,
    actor_email: Optional[str] = None,
    actor_id: Optional[uuid.UUID] = None,
    target_type: Optional[str] = None,
    target_id: Optional[uuid.UUID] = None,
    target_name: Optional[str] = None,
    tenant_slug: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
    ip_address: Optional[str] = None,
) -> SystemAuditEvent:
    event = SystemAuditEvent(
        timestamp=datetime.now(timezone.utc),
        actor_id=actor_id,
        actor_email=actor_email,
        action=action,
        target_type=target_type,
        target_id=target_id,
        target_name=target_name,
        tenant_slug=tenant_slug,
        severity=severity,
        detail=detail,
        ip_address=ip_address,
    )
    db.add(event)
    await db.flush()
    return event
