"""Saved Query Library API — CRUD endpoints for saved queries per model."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from shared.db.models import SavedQuery
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    SavedQueryCreate,
    SavedQueryResponse,
    SavedQueryUpdate,
)
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import caller_has_role, require_role
from src.api._scope import ensure_model_in_project

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/saved-queries",
    tags=["saved-queries"],
)


async def _require_owner_or_modeler(
    db,
    current_user: CurrentUser,
    project_id: UUID,
    model_id: UUID,
    query: SavedQuery,
) -> None:
    """Authorise a mutation (edit/delete) on a shared saved query.

    Saved queries are a model-shared library, but only the query's owner or a
    modeler+ may change or destroy one — a viewer cannot rewrite or delete a
    colleague's query (F-029-03). ``created_by`` records the owner's email at
    create time; we compare it to the caller's email, and otherwise require the
    modeler role via the same binding precedence as ``require_role``.
    """
    caller_identity = current_user.email or current_user.user_id
    if query.created_by == caller_identity:
        return
    if await caller_has_role(
        db, current_user, project_id, "modeler", model_id=model_id
    ):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Only the query owner or a modeler can modify this saved query",
    )


@router.get("", response_model=list[SavedQueryResponse])
async def list_saved_queries(
    project_id: UUID,
    model_id: UUID,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> list[SavedQueryResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        result = await db.execute(
            select(SavedQuery)
            .where(SavedQuery.model_id == model_id)
            .order_by(SavedQuery.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [SavedQueryResponse.model_validate(q) for q in result.scalars().all()]


@router.get("/{query_id}", response_model=SavedQueryResponse)
async def get_saved_query(
    project_id: UUID,
    model_id: UUID,
    query_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> SavedQueryResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        q = await db.get(SavedQuery, query_id)
        if q is None or q.model_id != model_id:
            raise HTTPException(status_code=404, detail="Saved query not found")
        return SavedQueryResponse.model_validate(q)


@router.post(
    "",
    response_model=SavedQueryResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("viewer")],
)
async def create_saved_query(
    project_id: UUID,
    model_id: UUID,
    body: SavedQueryCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> SavedQueryResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        q = SavedQuery(
            model_id=model_id,
            name=body.name,
            description=body.description,
            query_text=body.query_text,
            query_type=body.query_type,
            created_by=current_user.email,
        )
        db.add(q)
        await db.commit()
        await db.refresh(q)
        return SavedQueryResponse.model_validate(q)


@router.patch(
    "/{query_id}",
    response_model=SavedQueryResponse,
    dependencies=[require_role("viewer")],
)
async def update_saved_query(
    project_id: UUID,
    model_id: UUID,
    query_id: UUID,
    body: SavedQueryUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> SavedQueryResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        q = await db.get(SavedQuery, query_id)
        if q is None or q.model_id != model_id:
            raise HTTPException(status_code=404, detail="Saved query not found")
        await _require_owner_or_modeler(db, current_user, project_id, model_id, q)
        updates = body.model_dump(exclude_unset=True)
        for key, val in updates.items():
            setattr(q, key, val)
        await db.commit()
        await db.refresh(q)
        return SavedQueryResponse.model_validate(q)


@router.delete(
    "/{query_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("viewer")],
)
async def delete_saved_query(
    project_id: UUID,
    model_id: UUID,
    query_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        q = await db.get(SavedQuery, query_id)
        if q is None or q.model_id != model_id:
            raise HTTPException(status_code=404, detail="Saved query not found")
        await _require_owner_or_modeler(db, current_user, project_id, model_id, q)
        await db.delete(q)
        await db.commit()
