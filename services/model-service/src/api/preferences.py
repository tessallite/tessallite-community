"""User entity preferences: favourites and recently-used tracking."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from shared.db.models import KPI, Model, NamedSet, UserEntityPreference
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    FavouriteModelsResponse,
    UserPreferencesResponse,
    UserPreferenceToggle,
)
from src.auth.middleware import CurrentUser, forbid_embed_user, get_current_user
from src.auth.rbac import require_role
from src.api._scope import ensure_model_in_project, model_not_found

RECENTLY_USED_CAP = 10

# Entity types that carry a preference and prove membership through their own
# ``model_id`` column. ``"model"`` is deliberately NOT here — see
# ``_validate_entity_exists``.
_ENTITY_MODELS = {
    "kpi": KPI,
    "named_set": NamedSet,
}

# The complete vocabulary the read path will surface. Keep in step with
# ``UserPreferenceToggle.entity_type``; a row written under a type missing from
# here is stored and never read back.
_PREFERENCE_ENTITY_TYPES = ("kpi", "named_set", "model")


async def _validate_entity_exists(db, model_id: UUID, entity_type: str, entity_id: UUID) -> None:
    """Bug-5318: verify the referenced entity exists and belongs to the model.

    Bug-8899: ``"model"`` is its own branch and cannot go through the generic
    one. Every other entity here is OWNED by a model and says so with a
    ``model_id`` column; a model IS the scope, and the ``Model`` ORM class has no
    ``model_id`` attribute at all — the identity column is ``id``. Evaluating
    ``entity.model_id`` on one raises AttributeError, which FastAPI turns into a
    500 on every model toggle. That is exactly what registering ``"model"`` in
    ``_ENTITY_MODELS`` and changing nothing else produces.

    Membership for a model is therefore identity: the favourited model must BE
    the model named in the path, which ``ensure_model_in_project`` has already
    proven belongs to the path project. Anything else — a sibling model in the
    same project, a model in another project, an id that names nothing — is one
    indistinguishable 404, so this route cannot be used to probe which model ids
    exist elsewhere in the tenant.
    """
    if entity_type == "model":
        if entity_id != model_id:
            raise model_not_found()
        return
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

project_router = APIRouter(
    prefix="/projects/{project_id}/preferences",
    tags=["preferences"],
)


@project_router.get("/favourite-models", response_model=FavouriteModelsResponse)
async def list_favourite_models(
    project_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> FavouriteModelsResponse:
    """The calling user's favourited models within ONE project.

    The Explorer ranks a whole project's model list at once; asking the
    per-model preferences route once per card would be an N+1 that grows with
    the project. Same rows, one query.

    The join to ``models`` is the scope: it is what keeps another project's
    favourites out of this answer, and it also drops rows whose model has since
    been deleted rather than returning ids the caller cannot resolve.
    """
    model_ids: list[UUID] = []
    async for db in get_tenant_db(current_user.tenant_id):
        rows = await db.execute(
            select(UserEntityPreference.entity_id)
            .join(Model, Model.id == UserEntityPreference.model_id)
            .where(
                UserEntityPreference.user_id == current_user.user_id,
                UserEntityPreference.entity_type == "model",
                UserEntityPreference.preference_type == "favourite",
                Model.project_id == project_id,
            )
            .order_by(UserEntityPreference.created_at.desc())
        )
        model_ids = list(rows.scalars().all())
    return FavouriteModelsResponse(model_ids=model_ids)


@router.get("", response_model=UserPreferencesResponse)
async def get_preferences(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> UserPreferencesResponse:
    favourites: dict[str, list[UUID]] = {k: [] for k in _PREFERENCE_ENTITY_TYPES}
    recently_used: dict[str, list[UUID]] = {k: [] for k in _PREFERENCE_ENTITY_TYPES}

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
            if row.entity_type not in _PREFERENCE_ENTITY_TYPES:
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
