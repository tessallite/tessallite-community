"""User entity preferences: favourites and recently-used tracking."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from shared.db.models import KPI, NamedSet, UserEntityPreference
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    UserPreferencesResponse,
    UserPreferenceToggle,
)
from src.auth.middleware import CurrentUser, forbid_embed_user, get_current_user
from src.auth.rbac import require_role
from src.api._scope import ensure_model_in_project

RECENTLY_USED_CAP = 10

_ENTITY_MODELS = {
    "kpi": KPI,
    "named_set": NamedSet,
}


async def _validate_entity_exists(db, model_id: UUID, entity_type: str, entity_id: UUID) -> None:
    """Bug-5318: verify the referenced entity exists and belongs to the model."""
    orm_cls = _ENTITY_MODELS.get(entity_type)
    if orm_cls is None:
        raise HTTPException(status_code=422, detail=f"Unknown entity type: {entity_type}")
    entity = await db.get(orm_cls, entity_id)
    if entity is None or entity.model_id != model_id:
        raise HTTPException(status_code=404, detail=f"{entity_type} not found in this model")


router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/preferences",
    tags=["preferences"],
)


@router.get("", response_model=UserPreferencesResponse)
async def get_preferences(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> UserPreferencesResponse:
    favourites: dict[str, list[UUID]] = {"kpi": [], "named_set": []}
    recently_used: dict[str, list[UUID]] = {"kpi": [], "named_set": []}

    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        rows = await db.execute(
            select(UserEntityPreference)
            .where(
                UserEntityPreference.user_id == current_user.user_id,
                UserEntityPreference.model_id == model_id,
            )
            .order_by(UserEntityPreference.created_at.desc())
        )
        for row in rows.scalars().all():
            if row.entity_type not in ("kpi", "named_set"):
                continue
            if row.preference_type == "favourite":
                favourites[row.entity_type].append(row.entity_id)
            elif row.preference_type == "recently_used":
                recently_used[row.entity_type].append(row.entity_id)

    return UserPreferencesResponse(
        favourites=favourites,
        recently_used=recently_used,
    )


@router.put("/favourite")
async def toggle_favourite(
    project_id: UUID,
    model_id: UUID,
    body: UserPreferenceToggle,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> dict:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await _validate_entity_exists(db, model_id, body.entity_type, body.entity_id)
        existing = await db.execute(
            select(UserEntityPreference).where(
                UserEntityPreference.user_id == current_user.user_id,
                UserEntityPreference.model_id == model_id,
                UserEntityPreference.entity_type == body.entity_type,
                UserEntityPreference.entity_id == body.entity_id,
                UserEntityPreference.preference_type == "favourite",
            )
        )
        row = existing.scalar_one_or_none()
        if row:
            await db.delete(row)
            await db.commit()
            return {"favourited": False}
        else:
            pref = UserEntityPreference(
                user_id=current_user.user_id,
                model_id=model_id,
                entity_type=body.entity_type,
                entity_id=body.entity_id,
                preference_type="favourite",
            )
            db.add(pref)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                return {"favourited": True}
            return {"favourited": True}
    return {"favourited": False}


@router.post("/recently-used")
async def record_recently_used(
    project_id: UUID,
    model_id: UUID,
    body: UserPreferenceToggle,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> dict:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await _validate_entity_exists(db, model_id, body.entity_type, body.entity_id)
        existing = await db.execute(
            select(UserEntityPreference).where(
                UserEntityPreference.user_id == current_user.user_id,
                UserEntityPreference.model_id == model_id,
                UserEntityPreference.entity_type == body.entity_type,
                UserEntityPreference.entity_id == body.entity_id,
                UserEntityPreference.preference_type == "recently_used",
            )
        )
        row = existing.scalar_one_or_none()
        if row:
            await db.delete(row)
            await db.flush()

        pref = UserEntityPreference(
            user_id=current_user.user_id,
            model_id=model_id,
            entity_type=body.entity_type,
            entity_id=body.entity_id,
            preference_type="recently_used",
        )
        db.add(pref)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            return {"recorded": True}

        all_recent = await db.execute(
            select(UserEntityPreference)
            .where(
                UserEntityPreference.user_id == current_user.user_id,
                UserEntityPreference.model_id == model_id,
                UserEntityPreference.entity_type == body.entity_type,
                UserEntityPreference.preference_type == "recently_used",
            )
            .order_by(UserEntityPreference.created_at.desc())
        )
        rows = all_recent.scalars().all()
        if len(rows) > RECENTLY_USED_CAP:
            stale_ids = [r.id for r in rows[RECENTLY_USED_CAP:]]
            await db.execute(
                delete(UserEntityPreference).where(
                    UserEntityPreference.id.in_(stale_ids)
                )
            )

        await db.commit()
        return {"recorded": True}
    return {"recorded": False}
