"""Model-scoped schema change endpoints for the builder UI."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import SchemaChangeEvent
from shared.db.session import get_tenant_db
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/schema-changes",
    tags=["schema-changes"],
)


class SchemaChangeResponse(BaseModel):
    id: UUID
    model_id: UUID
    table_name: Optional[str] = None
    change_type: str
    is_breaking: bool
    detail: dict
    detected_at: Optional[datetime] = None
    acknowledged_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


async def _list_schema_changes(
    db: AsyncSession, model_id: UUID
) -> list[SchemaChangeEvent]:
    result = await db.execute(
        select(SchemaChangeEvent)
        .where(SchemaChangeEvent.model_id == model_id)
        .order_by(SchemaChangeEvent.detected_at.desc())
    )
    return list(result.scalars().all())


async def _acknowledge_schema_change(db: AsyncSession, event_id: UUID) -> None:
    event = await db.get(SchemaChangeEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Schema change event not found")
    event.acknowledged_at = datetime.now(timezone.utc)
    await db.commit()


@router.get(
    "",
    response_model=list[SchemaChangeResponse],
    dependencies=[require_role("viewer")],
)
async def list_schema_changes(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[SchemaChangeResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        events = await _list_schema_changes(db, model_id)
        return [SchemaChangeResponse.model_validate(e) for e in events]
    return []


@router.post(
    "/{event_id}/acknowledge",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def acknowledge_schema_change(
    project_id: UUID,
    model_id: UUID,
    event_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        await _acknowledge_schema_change(db, event_id)
