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
from src.api._scope import ensure_model_in_project
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


async def _acknowledge_schema_change(
    db: AsyncSession, model_id: UUID, event_id: UUID
) -> None:
    """Acknowledge one event, proving it belongs to ``model_id`` first.

    Bug-8862: this looked the event up by ``event_id`` alone. The route's
    ``model_id`` path parameter was never referenced, so even once the
    project -> model link is proven the model -> event link was not: a caller
    could acknowledge (mutate) an event belonging to a different model, and
    therefore a different project, of the same tenant. 404 rather than 403 —
    confirming the event exists under another model is itself a leak.
    """
    event = await db.get(SchemaChangeEvent, event_id)
    if event is None or event.model_id != model_id:
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
        # Bug-8862: require_role gates the caller's ROLE on the PATH project;
        # it never proves the model named in the path belongs to it.
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
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
        # Bug-8862: prove project -> model, then model -> event, before the
        # write. This is a mutation route, so an unbound chain is a
        # cross-project WRITE, not just a read.
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await _acknowledge_schema_change(db, model_id, event_id)
        return

    # FAIL CLOSED, mirroring stream_refresh_runs. Falling out of the loop means
    # no session was available, so nothing was written — but this route is
    # declared 204 NO CONTENT, so an implicit ``return None`` would answer a
    # bare 204 and tell the caller the acknowledgement SUCCEEDED when no write
    # happened. Of the three handlers in this lane sharing the
    # ``async for db in get_tenant_db(...)`` idiom, list_schema_changes falls
    # through to ``[]`` and validate_model to a response-model failure — both
    # safe. This one was the only one whose silent fall-through was a false
    # success, so it gets an explicit failure. Unreachable today
    # (``get_tenant_db`` always yields exactly once).
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Tenant database unavailable",
    )
