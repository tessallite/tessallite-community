"""Schema drift management API.

GET  /api/v1/admin/schema-drift          — list unacknowledged drift events
     ?model_id=<uuid>                       filter to one model
     ?include_acknowledged=true             include acknowledged events
PATCH /api/v1/admin/schema-drift/{event_id}/acknowledge — mark acknowledged
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select  # noqa: F401

from shared.db.models import ModelAlert, SchemaChangeEvent
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    SchemaChangeEventListResponse,
    SchemaChangeEventResponse,
)
from src.auth.middleware import CurrentUser, require_tenant_admin

router = APIRouter(prefix="/admin/schema-drift", tags=["schema-drift"])


@router.get("", response_model=SchemaChangeEventListResponse)
async def list_schema_drift_events(
    model_id: Optional[UUID] = Query(default=None),
    include_acknowledged: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> SchemaChangeEventListResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        stmt = select(SchemaChangeEvent)
        if model_id is not None:
            stmt = stmt.where(SchemaChangeEvent.model_id == model_id)
        if not include_acknowledged:
            stmt = stmt.where(SchemaChangeEvent.acknowledged_at.is_(None))

        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = (await db.execute(count_stmt)).scalar_one()

        stmt = stmt.order_by(SchemaChangeEvent.detected_at.desc()).offset(offset).limit(limit)
        result = await db.execute(stmt)
        events = list(result.scalars().all())

        return SchemaChangeEventListResponse(
            items=[SchemaChangeEventResponse.model_validate(e) for e in events],
            total=total,
        )


@router.patch("/{event_id}/acknowledge", response_model=SchemaChangeEventResponse)
async def acknowledge_drift_event(
    event_id: UUID,
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> SchemaChangeEventResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        event = await db.get(SchemaChangeEvent, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Schema change event not found")

        if event.acknowledged_at is None:
            event.acknowledged_at = datetime.now(timezone.utc)

            # Clear any open ModelAlert tied to this event
            alerts_result = await db.execute(
                select(ModelAlert).where(
                    ModelAlert.related_object_type == "schema_change_event",
                    ModelAlert.related_object_id == event_id,
                    ModelAlert.resolved_at.is_(None),
                    ModelAlert.dismissed_at.is_(None),
                )
            )
            for alert in alerts_result.scalars().all():
                alert.resolved_at = datetime.now(timezone.utc)

            await db.commit()
            await db.refresh(event)

        return SchemaChangeEventResponse.model_validate(event)
