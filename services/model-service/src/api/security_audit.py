"""Row-security audit trail API.

GET /api/v1/admin/security-audit
    ?model_id=<uuid>     filter to one model
    ?from=<iso>          start of time window (inclusive)
    ?to=<iso>            end of time window (inclusive)
    &limit=50&offset=0   pagination
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from shared.db.models import QueryLog
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import SecurityAuditEntry, SecurityAuditListResponse
from src.auth.middleware import CurrentUser, require_tenant_admin

router = APIRouter(prefix="/admin/security-audit", tags=["security-audit"])


@router.get("", response_model=SecurityAuditListResponse)
async def list_security_audit(
    model_id: Optional[UUID] = Query(default=None),
    from_: Optional[datetime] = Query(default=None, alias="from"),
    to: Optional[datetime] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> SecurityAuditListResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        stmt = select(QueryLog).where(QueryLog.security_rules_applied.isnot(None))

        if model_id is not None:
            stmt = stmt.where(QueryLog.model_id == model_id)
        if from_ is not None:
            stmt = stmt.where(QueryLog.created_at >= from_)
        if to is not None:
            stmt = stmt.where(QueryLog.created_at <= to)

        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = (await db.execute(count_stmt)).scalar_one()

        stmt = stmt.order_by(QueryLog.created_at.desc()).offset(offset).limit(limit)
        result = await db.execute(stmt)
        rows = list(result.scalars().all())

        items = [
            SecurityAuditEntry(
                query_log_id=r.id,
                model_id=r.model_id,
                user_identity=r.user_identity,
                protocol=r.protocol,
                route_type=r.route_type,
                security_rules_applied=r.security_rules_applied or [],
                created_at=r.created_at,
            )
            for r in rows
        ]
        return SecurityAuditListResponse(items=items, total=total)
