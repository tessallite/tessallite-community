"""System-plane audit writer (G-022-01 / F-022-03).

Tenant ``audit_events`` cannot survive tenant-schema drop and cannot record
system-admin actions that have no tenant session. Callers pass the system
DB session and emit **before** ``commit`` so a flush failure rolls back the
mutation.

Pre-migration bootstrap: ``tess_system.audit_events`` is created by migration
0212, so a system whose schema is behind that migration has no audit store.
The login-gated migration path must still authenticate and apply migrations,
so ``system_audit`` skips the event when the table is absent (probe only —
it never touches the caller's transaction). Post-migration behaviour is
unchanged: the event is added and flushed before the caller's commit.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import SystemAuditEvent

_AUDIT_TABLE_PROBE = "SELECT to_regclass('tess_system.audit_events')"


async def _audit_table_present(db: AsyncSession) -> bool:
    """True when the system audit table exists.

    The probe must never break auditing, so any probe error reports present
    (fail closed) and the normal insert path raises as before.
    """
    try:
        result = await db.execute(text(_AUDIT_TABLE_PROBE))
        return result.scalar() is not None
    except Exception:
        return True


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
) -> Optional[SystemAuditEvent]:
    if not await _audit_table_present(db):
        return None  # pre-migration bootstrap: no audit store yet
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
