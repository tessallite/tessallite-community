"""
Model CRUD routes.

Role requirements:
  GET (list / get)  → viewer+
  POST              → modeler+
  PATCH             → modeler+
  DELETE            → admin
"""
from __future__ import annotations

import secrets
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select

from shared.audit.logger import audit
from shared.webhooks.dispatcher import emit_webhook_logged as emit_webhook
from shared.db.models import (
    AggregateDefinition,
    DataSource,
    DataTarget,
    Model,
    ModelVersion,
    ProjectConnection,
)
from src.api._cascade_delete import delete_model_cascade
from src.api._model_lock import acquire_model_definition_lock
from src.api._scope import ensure_ref_in_model
from shared.db.session import get_tenant_db
from shared.physical_cleanup import attempt_scheduled_physical_cleanup
from shared.schemas.pydantic_models import ModelCreate, ModelResponse, ModelUpdate
from src.auth.middleware import CurrentEmbedUser, CurrentUser, enforce_model_scope, get_current_user
from src.auth.rbac import caller_has_role, require_role, resolve_listable_model_scope
from src.api.personas import seed_technical_persona
from src.licensing_guard import enforce_create_cap

router = APIRouter(prefix="/projects/{project_id}/models", tags=["models"])


def _enforce_embed_project_scope(current_user: CurrentUser, project_id: UUID) -> None:
    """Block embed users whose project_ids claim excludes this project."""
    if isinstance(current_user, CurrentEmbedUser):
        if current_user.project_ids is not None:
            if str(project_id).lower() not in [p.lower() for p in current_user.project_ids]:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Embed token does not grant access to this project",
                )


async def _build_trust_meta(db, model: Model) -> dict:
    """Phase 5 of the semantic-layer plan: assemble the freshness / source /
    owner trust signals the gateway uses to render Excel column tooltips
    and synthetic info measures.

    `last_refreshed_at` is the most recent aggregate refresh timestamp on
    the model, falling back to the model's own updated_at if there are no
    aggregates yet. `source_system` is the connection type of the model's
    first DataSource. `owner` is empty until a future iteration adds an
    owner column to the Model entity.
    """
    last_refreshed = None
    agg_result = await db.execute(
        select(AggregateDefinition.last_refreshed_at)
        .where(AggregateDefinition.model_id == model.id)
        .where(AggregateDefinition.last_refreshed_at.is_not(None))
        .order_by(AggregateDefinition.last_refreshed_at.desc())
        .limit(1)
    )
    last_refreshed_row = agg_result.scalar_one_or_none()
    if last_refreshed_row is not None:
        last_refreshed = last_refreshed_row.isoformat()
    else:
        last_refreshed = model.updated_at.isoformat() if model.updated_at else None

    source_system: str | None = None
    src_result = await db.execute(
        select(DataSource).where(DataSource.model_id == model.id).limit(1)
    )
    src = src_result.scalar_one_or_none()
    if src is not None:
        conn = await db.get(ProjectConnection, src.project_connection_id)
        source_system = conn.connection_type if conn else src.source_type

    return {
        "last_refreshed_at": last_refreshed,
        "source_system": source_system,
        "owner": "",
    }


def _attach_trust(meta: dict, response: ModelResponse) -> ModelResponse:
    response.trust_meta = meta
    return response


async def _resolve_version_numbers(
    db, model: Model
) -> tuple[int | None, int | None]:
    """Return (last_saved_version_number, deployed_version_number).

    Both are None when the model has no saved versions. The deployed number
    is None when the model is undeployed even if saves exist. The frontend
    toolbar chip ("Saved v5", "Deployed v3") renders directly from these.
    """
    last_saved_row = await db.execute(
        select(ModelVersion.version_number)
        .where(ModelVersion.model_id == model.id)
        .order_by(ModelVersion.version_number.desc())
        .limit(1)
    )
    last_saved = last_saved_row.scalar_one_or_none()

    deployed_number: int | None = None
    if model.deployed_version_id is not None:
        deployed_row = await db.execute(
            select(ModelVersion.version_number).where(
                ModelVersion.id == model.deployed_version_id
            )
        )
        deployed_number = deployed_row.scalar_one_or_none()

    return last_saved, deployed_number


async def _decorate_response(db, model: Model) -> ModelResponse:
    response = ModelResponse.model_validate(model)
    last_saved, deployed_number = await _resolve_version_numbers(db, model)
    response.last_saved_version_number = last_saved
    response.deployed_version_number = deployed_number
    return _attach_trust(await _build_trust_meta(db, model), response)


@router.post(
    "",
    response_model=ModelResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_model(
    project_id: UUID,
    body: ModelCreate,
    current_user: CurrentUser = Depends(get_current_user),
) -> ModelResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        async def _count_models() -> int:
            # Total models across the whole own tenant (all projects), not per project.
            r = await db.execute(select(func.count()).select_from(Model))
            return int(r.scalar() or 0)

        # Bug-6567: pass db so the count-then-create is serialised with an
        # advisory lock, preventing two concurrent model creates at cap-1.
        await enforce_create_cap("model", _count_models, db=db)

        existing = await db.execute(
            select(Model).where(Model.project_id == project_id, Model.slug == body.slug)
        )
        existing_model = existing.scalar_one_or_none()
        if existing_model:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Model with slug '{body.slug}' already exists",
            )
        # seed is a 12-char random string used for deterministic aggregate naming
        seed = secrets.token_hex(6)  # 6 bytes = 12 hex chars
        payload = body.model_dump(exclude_none=True)
        payload["display_name"] = payload.get("display_name") or body.slug
        payload["status"] = "active"
        model = Model(project_id=project_id, seed=seed, **payload)
        db.add(model)
        await db.flush()
        # Bug-6138: seed the canonical Technical persona so the hidden-columns
        # technical catalogue is live from creation, not only for models that
        # predate migration 0042. Same transaction as the model create.
        await seed_technical_persona(db, model.id)
        await audit(
            db, action="model.create", severity="info",
            actor_email=current_user.email,
            target_type="model", target_id=model.id,
            target_name=model.display_name,
        )
        await db.commit()
        await db.refresh(model)
        return await _decorate_response(db, model)


async def _decorate_models_batch(db, models: list[Model]) -> list[ModelResponse]:
    """Decorate many models with O(1) grouped queries instead of O(5/model).

    F-013-12: ``_decorate_response`` runs 2 version queries + 2 trust-meta
    queries per model. On a project with N models, ``GET /models`` issued 4N+
    round-trips, and the gateway fans this out per project on XMLA catalog
    discovery. This batches the version-number lookup, the latest-refresh
    lookup, and the source/connection lookup into one query each across all
    models in the project, then assembles per-model responses in memory.
    """
    if not models:
        return []
    model_ids = [m.id for m in models]

    # 1. Last-saved version number per model (max version_number).
    last_saved_rows = await db.execute(
        select(
            ModelVersion.model_id,
            func.max(ModelVersion.version_number),
        )
        .where(ModelVersion.model_id.in_(model_ids))
        .group_by(ModelVersion.model_id)
    )
    last_saved_by_model: dict[UUID, int] = {
        mid: n for mid, n in last_saved_rows.all()
    }

    # 2. Deployed version number per model (only for models with a pointer).
    deployed_pointer_ids = [
        m.deployed_version_id for m in models if m.deployed_version_id is not None
    ]
    deployed_number_by_version: dict[UUID, int] = {}
    if deployed_pointer_ids:
        dep_rows = await db.execute(
            select(ModelVersion.id, ModelVersion.version_number).where(
                ModelVersion.id.in_(deployed_pointer_ids)
            )
        )
        deployed_number_by_version = {vid: n for vid, n in dep_rows.all()}

    # 3. Latest aggregate refresh timestamp per model.
    refresh_rows = await db.execute(
        select(
            AggregateDefinition.model_id,
            func.max(AggregateDefinition.last_refreshed_at),
        )
        .where(
            AggregateDefinition.model_id.in_(model_ids),
            AggregateDefinition.last_refreshed_at.is_not(None),
        )
        .group_by(AggregateDefinition.model_id)
    )
    last_refresh_by_model: dict[UUID, Any] = {
        mid: ts for mid, ts in refresh_rows.all()
    }

    # 4. First DataSource per model + its connection type. Fetch all sources
    #    for the project's models, then resolve connection types in one pass.
    src_rows = await db.execute(
        select(DataSource).where(DataSource.model_id.in_(model_ids))
    )
    first_source_by_model: dict[UUID, DataSource] = {}
    for src in src_rows.scalars().all():
        # ORM has no created_at ordering guarantee here, but trust-meta only
        # needs *a* source; keep the first seen per model (matches the prior
        # single-row `.limit(1)` behaviour, which was itself unordered).
        first_source_by_model.setdefault(src.model_id, src)
    conn_ids = {
        s.project_connection_id for s in first_source_by_model.values()
    }
    conn_type_by_id: dict[UUID, str] = {}
    if conn_ids:
        conn_rows = await db.execute(
            select(ProjectConnection.id, ProjectConnection.connection_type).where(
                ProjectConnection.id.in_(conn_ids)
            )
        )
        conn_type_by_id = {cid: ctype for cid, ctype in conn_rows.all()}

    out: list[ModelResponse] = []
    for m in models:
        response = ModelResponse.model_validate(m)
        response.last_saved_version_number = last_saved_by_model.get(m.id)
        response.deployed_version_number = (
            deployed_number_by_version.get(m.deployed_version_id)
            if m.deployed_version_id is not None
            else None
        )
        ts = last_refresh_by_model.get(m.id)
        if ts is not None:
            last_refreshed = ts.isoformat()
        else:
            last_refreshed = m.updated_at.isoformat() if m.updated_at else None
        src = first_source_by_model.get(m.id)
        source_system: str | None = None
        if src is not None:
            source_system = conn_type_by_id.get(
                src.project_connection_id, src.source_type
            )
        meta = {
            "last_refreshed_at": last_refreshed,
            "source_system": source_system,
            "owner": "",
        }
        out.append(_attach_trust(meta, response))
    return out


@router.get("", response_model=list[ModelResponse])
async def list_models(
    project_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[ModelResponse]:
    # Codex-HIGH (Bug-8101 follow-up): authorize AND filter per model so a
    # MODEL-SCOPED viewer/model_viewer can browse (and only) their granted
    # model(s). The old require_role("viewer") route dependency 403'd them
    # because a list request carries no model_id, so only the project-wide
    # binding was consulted. resolve_listable_model_scope raises 403 when the
    # caller has no project access and otherwise returns None (all models) or
    # the exact set of visible model ids.
    _enforce_embed_project_scope(current_user, project_id)
    async for db in get_tenant_db(current_user.tenant_id):
        visible_scope = await resolve_listable_model_scope(
            db, current_user, project_id
        )
        result = await db.execute(
            select(Model).where(Model.project_id == project_id).order_by(Model.slug)
        )
        models = result.scalars().all()
        visible = [
            m for m in models
            if visible_scope is None or str(m.id).lower() in visible_scope
        ]
        return await _decorate_models_batch(db, visible)


@router.get("/{model_id}", response_model=ModelResponse)
async def get_model(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> ModelResponse:
    _enforce_embed_project_scope(current_user, project_id)
    enforce_model_scope(current_user, str(model_id), project_id=str(project_id))
    async for db in get_tenant_db(current_user.tenant_id):
        m = await db.get(Model, model_id)
        if m is None or m.project_id != project_id:
            raise HTTPException(status_code=404, detail="Model not found")
        response = await _decorate_response(db, m)
        # Bug-8101 / F-104-01 (spec D2): tell the Model Builder whether this
        # caller may author the model, so a read-only consumer (model_viewer /
        # viewer) opens the model read-only instead of the full authoring
        # canvas. Uses the same binding precedence as require_role; the backend
        # gate stays authoritative for every mutation.
        response.caller_can_author = await caller_has_role(
            db, current_user, project_id, "modeler", model_id=model_id
        )
        # G-013-02: same binding precedence the revert route enforces
        # (require_role("admin")), so the Versions dialog can show Revert to a
        # project-scoped admin, not only a tenant/system admin.
        response.caller_can_admin = await caller_has_role(
            db, current_user, project_id, "admin", model_id=model_id
        )
        return response


@router.patch(
    "/{model_id}",
    response_model=ModelResponse,
    dependencies=[require_role("modeler")],
)
async def update_model(
    project_id: UUID,
    model_id: UUID,
    body: ModelUpdate,
    current_user: CurrentUser = Depends(get_current_user),
) -> ModelResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        # Bug-8437: a revert UPDATEs this row's scalars in place from the
        # snapshot (rehydrator step 5). Without the per-model definition lock a
        # rename / display-name / canvas-layout edit made during a revert is
        # silently discarded, or overwrites the just-restored value — no error,
        # no audit trail. Detection cannot close a lost update; mutual exclusion
        # can, which is why ``models`` stays OUT of the runtime write guard's
        # table set (guarding it would only re-introduce the ``bump_data_epoch``
        # flood that made the guard self-mute) and the race is closed HERE.
        #
        # READ-UNDER-LOCK: the ownership check IS the entity fetch, so the lock
        # is acquired first and the row is read under it (shared/db/model_lock.py
        # ordering contract).
        await acquire_model_definition_lock(db, model_id)
        m = await db.get(Model, model_id)
        if m is None or m.project_id != project_id:
            raise HTTPException(status_code=404, detail="Model not found")
        updates = body.model_dump(exclude_unset=True)

        # ``ModelUpdate.target_id`` is a body foreign key to ``data_targets``,
        # applied by the blanket ``setattr`` loop below. RBAC proves only that
        # the caller may act in the PATH project; nothing proved the submitted
        # target belonged to it, and ``data_targets.id`` is tenant-schema-wide,
        # so a project-B target satisfies the foreign key and persists.
        #
        # This is the model-level twin of the aggregate-level hole Bug-8026
        # closed in ``aggregates.py::create_aggregate``, and it is the more
        # dangerous of the two: ``models.target_id`` is the model's DEFAULT
        # materialisation destination, so it is inherited by aggregates and
        # pockets created afterwards rather than scoped to one definition. A
        # DataTarget carries ``project_connection_id`` — live, Fernet-encrypted
        # source credentials (Bug-5325) — so a foreign value points this model's
        # CREATE TABLE AS and every scheduled refresh at another project's
        # source connection.
        #
        # Keyed on PRESENCE, not truthiness: an explicit ``null`` means "clear
        # the default target" and stays legal, exactly as ``targets.py``'s
        # delete path clears it. The check runs under the definition lock and
        # BEFORE the setattr loop, because the helper's SELECT autoflushes and a
        # guard placed afterwards would already have sent the unvalidated value
        # to the database.
        if "target_id" in updates:
            await ensure_ref_in_model(
                db,
                DataTarget,
                ref_id=updates["target_id"],
                model_id=model_id,
                project_id=project_id,
                field_name="target_id",
                noun="a data target",
            )

        old_slug = m.slug
        new_slug = updates.get("slug")
        if new_slug and new_slug != old_slug:
            existing = await db.execute(
                select(Model).where(
                    Model.project_id == project_id,
                    Model.slug == new_slug,
                    Model.id != model_id,
                )
            )
            existing_model = existing.scalar_one_or_none()
            if existing_model:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Model with slug '{new_slug}' already exists",
                )
            if "display_name" not in updates and (not m.display_name or m.display_name == old_slug):
                updates["display_name"] = new_slug
        if "display_name" in updates and (updates["display_name"] is None or not str(updates["display_name"]).strip()):
            updates["display_name"] = new_slug or m.slug
        for key, val in updates.items():
            setattr(m, key, val)
        await audit(
            db, action="model.update", severity="info",
            actor_email=current_user.email,
            target_type="model", target_id=m.id,
            target_name=m.display_name,
            detail={"fields": list(updates.keys())},
        )
        await db.commit()

        if "canvas_layout" in updates:
            try:
                import asyncio
                from shared.git.model_repo import commit_layout as git_commit_layout
                await asyncio.to_thread(
                    git_commit_layout,
                    tenant_slug=current_user.tenant_id,
                    model_slug=m.slug,
                    layout_json=updates["canvas_layout"],
                    summary=None,
                    author_email=current_user.email or current_user.user_id,
                )
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    "Git layout commit failed for model %s (non-fatal)",
                    model_id, exc_info=True,
                )

        await db.refresh(m)
        return await _decorate_response(db, m)


@router.delete(
    "/{model_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    # Model deletion is a model-level setup action: a project modeller owns the
    # full model lifecycle (create/edit/deploy/delete). Tenant/project admins
    # inherit it. Project rename/disable/delete stay above modeller — see the
    # Explorer RBAC matrix (docs/architecture/architecture_explorer-rbac-matrix.md).
    dependencies=[require_role("modeler")],
)
async def delete_model(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        m = await db.get(Model, model_id)
        if m is None or m.project_id != project_id:
            raise HTTPException(status_code=404, detail="Model not found")
        model_name = m.display_name
        errors = await delete_model_cascade(db, model_id)
        if errors:
            await db.rollback()
            raise HTTPException(
                status_code=500,
                detail=f"Model deletion failed at: {'; '.join(errors)}",
            )
        await audit(
            db, action="model.delete", severity="critical",
            actor_email=current_user.email,
            target_type="model", target_id=model_id,
            target_name=model_name,
        )
        await db.commit()
        await attempt_scheduled_physical_cleanup(db)
        await emit_webhook(current_user.tenant_id, "model.deleted", {
            "model_id": str(model_id),
            "model_name": model_name,
            "actor": current_user.email,
        })
