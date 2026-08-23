"""Saved Query Library API — CRUD endpoints for saved queries per model."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select

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


def _caller_identity(current_user: CurrentUser) -> str:
    return current_user.email or current_user.user_id


async def _load_visible_query(
    db,
    *,
    model_id: UUID,
    query_id: UUID,
    owner: str,
) -> SavedQuery:
    """Load through the ownership predicate so private rows never leak."""
    result = await db.execute(
        select(SavedQuery).where(
            SavedQuery.id == query_id,
            SavedQuery.model_id == model_id,
            or_(
                SavedQuery.created_by == owner,
                SavedQuery.is_shared.is_(True),
            ),
        )
    )
    query = result.scalar_one_or_none()
    if query is None:
        raise HTTPException(status_code=404, detail="Saved query not found")
    return query


async def _can_edit(
    db,
    current_user: CurrentUser,
    project_id: UUID,
    model_id: UUID,
    *,
    is_owner: bool,
) -> bool:
    """Bug-5983: mirrors ``_require_owner_or_modeler`` as a boolean check so
    the response contract can tell the frontend whether edit/delete controls
    are actually usable, not just whether the caller happens to be the
    owner. ``is_owner`` short-circuits the role lookup for the common case.
    """
    if is_owner:
        return True
    return await caller_has_role(
        db, current_user, project_id, "modeler", model_id=model_id
    )


async def _require_owner_or_modeler(
    db,
    current_user: CurrentUser,
    project_id: UUID,
    model_id: UUID,
    query: SavedQuery,
) -> None:
    """Authorise a mutation (edit/delete) on a shared saved query.

    Saved queries are personal by default. For a shared query, only its owner or
    a modeler+ may change or destroy it — a viewer cannot rewrite or delete a
    colleague's query. ``created_by`` records the owner's email at create time;
    we compare it to the caller's email, and otherwise require the modeler role
    via the same binding precedence as ``require_role``.
    """
    caller_identity = _caller_identity(current_user)
    if query.created_by == caller_identity:
        return
    if not query.is_shared:
        raise HTTPException(status_code=404, detail="Saved query not found")
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
        caller_identity = _caller_identity(current_user)
        result = await db.execute(
            select(SavedQuery)
            .where(
                SavedQuery.model_id == model_id,
                or_(
                    SavedQuery.created_by == caller_identity,
                    SavedQuery.is_shared.is_(True),
                ),
            )
            .order_by(SavedQuery.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = result.scalars().all()
        # Bug-5983: the modeler-role mutation grant is model-wide (not
        # per-row), so resolve it once per request rather than once per
        # saved query.
        is_modeler_plus = await caller_has_role(
            db, current_user, project_id, "modeler", model_id=model_id
        )
        responses = []
        for q in rows:
            resp = SavedQueryResponse.model_validate(q)
            resp.is_owner = q.created_by == caller_identity
            resp.can_edit = resp.is_owner or (q.is_shared and is_modeler_plus)
            responses.append(resp)
        return responses


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
        caller_identity = _caller_identity(current_user)
        q = await _load_visible_query(
            db, model_id=model_id, query_id=query_id, owner=caller_identity
        )
        resp = SavedQueryResponse.model_validate(q)
        resp.is_owner = q.created_by == caller_identity
        resp.can_edit = await _can_edit(
            db, current_user, project_id, model_id, is_owner=resp.is_owner
        )
        return resp


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
            created_by=_caller_identity(current_user),
            is_shared=body.is_shared,
        )
        db.add(q)
        await db.commit()
        await db.refresh(q)
        resp = SavedQueryResponse.model_validate(q)
        resp.is_owner = True
        resp.can_edit = True
        return resp


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
        caller_identity = _caller_identity(current_user)
        q = await _load_visible_query(
            db, model_id=model_id, query_id=query_id, owner=caller_identity
        )
        await _require_owner_or_modeler(db, current_user, project_id, model_id, q)
        updates = body.model_dump(exclude_unset=True)
        if (
            "is_shared" in updates
            and updates["is_shared"] is not None
            and updates["is_shared"] != q.is_shared
            and q.created_by != caller_identity
        ):
            raise HTTPException(
                status_code=403,
                detail="Only the query owner can change sharing",
            )
        for key, val in updates.items():
            if key == "is_shared" and val is None:
                continue
            setattr(q, key, val)
        await db.commit()
        await db.refresh(q)
        resp = SavedQueryResponse.model_validate(q)
        resp.is_owner = q.created_by == caller_identity
        # The mutation above already passed the owner-or-modeler gate, so the
        # caller can always edit the query they just successfully updated.
        resp.can_edit = True
        return resp


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
        q = await _load_visible_query(
            db,
            model_id=model_id,
            query_id=query_id,
            owner=_caller_identity(current_user),
        )
        await _require_owner_or_modeler(db, current_user, project_id, model_id, q)
        await db.delete(q)
        await db.commit()
