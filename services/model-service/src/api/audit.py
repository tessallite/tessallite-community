"""Audit log API — list, filter, CSV export.

GET  /admin/audit-events           — paginated, filterable
GET  /admin/audit-events/export    — CSV download

All endpoints require tenant_admin role.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from shared.db.models import AuditEvent
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    AuditEventListResponse,
    AuditEventResponse,
)
from src.auth.middleware import CurrentUser, require_tenant_admin

router = APIRouter(prefix="/admin/audit-events", tags=["audit"])

# F-022-09: CSV exports are capped for safety. The cap is surfaced to the
# caller via the ``X-Tessallite-Export-Truncated`` response header and a final
# CSV notice row, so a compliance admin is never silently handed a partial set.
_EXPORT_ROW_CAP = 10000


def _build_query(
    *,
    actor_email: Optional[str] = None,
    action: Optional[str] = None,
    target_type: Optional[str] = None,
    severity: Optional[str] = None,
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
):
    stmt = select(AuditEvent)
    if actor_email:
        stmt = stmt.where(AuditEvent.actor_email == actor_email)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    if target_type:
        stmt = stmt.where(AuditEvent.target_type == target_type)
    if severity:
        stmt = stmt.where(AuditEvent.severity == severity)
    if from_date:
        stmt = stmt.where(AuditEvent.timestamp >= from_date)
    if to_date:
        stmt = stmt.where(AuditEvent.timestamp <= to_date)
    return stmt


@router.get("", response_model=AuditEventListResponse)
async def list_audit_events(
    current_user: CurrentUser = Depends(require_tenant_admin),
    actor_email: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    target_type: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    from_date: Optional[datetime] = Query(None),
    to_date: Optional[datetime] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> AuditEventListResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        base = _build_query(
            actor_email=actor_email,
            action=action,
            target_type=target_type,
            severity=severity,
            from_date=from_date,
            to_date=to_date,
        )
        count_result = await db.execute(
            select(func.count()).select_from(base.subquery())
        )
        total = count_result.scalar() or 0

        rows_result = await db.execute(
            base.order_by(AuditEvent.timestamp.desc())
            .limit(limit)
            .offset(offset)
        )
        items = [
            AuditEventResponse.model_validate(r) for r in rows_result.scalars().all()
        ]
        return AuditEventListResponse(
            items=items, total=total, limit=limit, offset=offset
        )


@router.get("/export")
async def export_audit_events(
    current_user: CurrentUser = Depends(require_tenant_admin),
    actor_email: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    target_type: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    from_date: Optional[datetime] = Query(None),
    to_date: Optional[datetime] = Query(None),
) -> StreamingResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        base = _build_query(
            actor_email=actor_email,
            action=action,
            target_type=target_type,
            severity=severity,
            from_date=from_date,
            to_date=to_date,
        )
        # Fetch one beyond the cap so we can tell whether the result was
        # truncated and tell the caller, rather than silently returning a
        # partial set (F-022-09). Narrow the range with from_date/to_date to
        # keep an export bounded by the admin's intent, not by the row cap.
        rows_result = await db.execute(
            base.order_by(AuditEvent.timestamp.desc()).limit(_EXPORT_ROW_CAP + 1)
        )
        events = list(rows_result.scalars().all())
        truncated = len(events) > _EXPORT_ROW_CAP
        if truncated:
            events = events[:_EXPORT_ROW_CAP]

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "timestamp", "actor_email", "action", "target_type",
            "target_name", "severity", "ip_address", "detail",
        ])
        for e in events:
            writer.writerow([
                e.timestamp.isoformat() if e.timestamp else "",
                e.actor_email or "",
                e.action,
                e.target_type or "",
                e.target_name or "",
                e.severity,
                e.ip_address or "",
                str(e.detail) if e.detail else "",
            ])
        if truncated:
            # A trailing notice row makes the truncation visible even to a
            # viewer who opens the CSV without reading the headers.
            writer.writerow([
                f"NOTE: export truncated to the most recent {_EXPORT_ROW_CAP} "
                "events. Narrow the date range (from_date/to_date) to export "
                "the full set for a period.",
            ])

        buf.seek(0)
        headers = {
            "Content-Disposition": "attachment; filename=audit_events.csv",
            "X-Tessallite-Export-Truncated": "true" if truncated else "false",
            "X-Tessallite-Export-Row-Cap": str(_EXPORT_ROW_CAP),
        }
        return StreamingResponse(buf, media_type="text/csv", headers=headers)
