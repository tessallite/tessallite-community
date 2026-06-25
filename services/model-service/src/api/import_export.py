"""Per-model export + cross-tenant import (Phase 6).

Routes:

  GET    /projects/{p}/models/{m}/export                    — download bundle
  POST   /projects/{p}/models/import                        — create from bundle

The export bundle is the live snapshot wrapped in a small envelope so the
import side can recognise the format and refuse strangers. Connections
never travel; the importer asks the caller for a connection_mapping
keyed by the *exporter's* connection ids.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.aggregate_rebuild_trigger import trigger_model_refresh
from shared.db.models import (
    Model,
    ProjectConnection,
    UserAccessBinding,
)
from shared.db.session import get_tenant_db
from shared.model_snapshot import (
    SNAPSHOT_SCHEMA_VERSION,
    SnapshotSchemaError,
    SnapshotVersionError,
    prepare_snapshot_for_import,
    rehydrate_into_live,
    snapshot_model,
)
from shared.model_snapshot.slug_utils import (
    insert_model_with_slug_retry,
    resolve_slug_collision,
    slugify,
)
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

router = APIRouter(tags=["import-export"])

EXPORT_FORMAT = "tessallite-model/v1"

# Strong refs to fire-and-forget import-rebuild trigger tasks so the event loop
# does not GC them mid-flight (F-013-13 pattern).
_import_rebuild_tasks: set[asyncio.Task] = set()


def _slugify(text: str) -> str:
    # F-020-19: bound to the 64-char column; suffix headroom is reserved by
    # the shared slug_utils collision helpers, not here.
    return slugify(text, fallback="imported-model")


# ---------------------------------------------------------------------------
# Authorization (matches versions.py — bootstrap-admin rule)
# ---------------------------------------------------------------------------

async def _ensure_model_access(
    project_id: UUID, model_id: UUID, current_user: CurrentUser, tenant_db: AsyncSession
) -> Model:
    model = await tenant_db.get(Model, model_id)
    if model is None or model.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model {model_id} not found in project {project_id}",
        )
    if current_user.role in ("system_admin", "tenant_admin"):
        return model
    user_identity = current_user.email or current_user.user_id
    any_binding = (
        await tenant_db.execute(
            select(UserAccessBinding).where(
                UserAccessBinding.project_id == project_id
            ).limit(1)
        )
    ).scalar_one_or_none()
    if any_binding is None:
        return model
    rows = await tenant_db.execute(
        select(UserAccessBinding).where(
            UserAccessBinding.user_identity == user_identity,
            (UserAccessBinding.project_id == project_id)
            | (UserAccessBinding.model_id == model_id),
        )
    )
    if rows.scalars().first() is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No access binding for this model",
        )
    return model


async def _ensure_project_access(
    project_id: UUID, current_user: CurrentUser, tenant_db: AsyncSession
) -> None:
    if current_user.role in ("system_admin", "tenant_admin"):
        return
    user_identity = current_user.email or current_user.user_id
    any_binding = (
        await tenant_db.execute(
            select(UserAccessBinding).where(
                UserAccessBinding.project_id == project_id
            ).limit(1)
        )
    ).scalar_one_or_none()
    if any_binding is None:
        return
    rows = await tenant_db.execute(
        select(UserAccessBinding).where(
            UserAccessBinding.user_identity == user_identity,
            UserAccessBinding.project_id == project_id,
        )
    )
    if rows.scalars().first() is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No access binding for this project",
        )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ExportBundle(BaseModel):
    schema_version: int
    export_format: str
    exported_at: datetime
    exported_from: dict[str, str]
    model_display_name: str
    model_slug: str
    snapshot: dict[str, Any]


class ConnectionStub(BaseModel):
    """A connection referenced by the export bundle's data sources/targets."""
    id: UUID
    role: str  # "source" | "target"
    display_name: Optional[str] = None
    connection_type: Optional[str] = None


class ExportPreviewResponse(BaseModel):
    bundle: ExportBundle
    connections_required: list[ConnectionStub]


class ImportRequest(BaseModel):
    bundle: ExportBundle
    target_project_id: UUID
    target_slug: Optional[str] = Field(default=None, max_length=64)
    target_display_name: Optional[str] = Field(default=None, max_length=255)
    connection_mapping: dict[str, str] = Field(default_factory=dict)
    deploy_immediately: bool = False


class ImportResponse(BaseModel):
    model_id: UUID
    slug: str
    display_name: str
    deployed_version_id: Optional[UUID] = None
    missing_connections: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

@router.get(
    "/projects/{project_id}/models/{model_id}/snapshot-export",
    response_model=ExportPreviewResponse,
    dependencies=[require_role("viewer")],
)
async def export_model(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ExportPreviewResponse:
    bundle: Optional[ExportBundle] = None
    connections: list[ConnectionStub] = []
    async for tenant_db in get_tenant_db(current_user.tenant_id):
        model = await _ensure_model_access(project_id, model_id, current_user, tenant_db)
        snap = await snapshot_model(model_id, tenant_db)
        snap["exported_deployed_version_id"] = (
            str(model.deployed_version_id) if model.deployed_version_id else None
        )
        # Resolve connection display info for every source/target so the
        # importer UI can show "this bundle wants a Postgres connection
        # called 'analytics'; pick a local one" without inventing names.
        conn_ids: dict[str, str] = {}  # id -> role
        for s in snap.get("data_sources", []) or []:
            cid = s.get("project_connection_id")
            if cid:
                conn_ids[cid] = "source"
        for t in snap.get("data_targets", []) or []:
            cid = t.get("project_connection_id")
            if cid:
                conn_ids[cid] = "target"
        if conn_ids:
            rows = await tenant_db.execute(
                select(ProjectConnection).where(
                    ProjectConnection.id.in_([UUID(c) for c in conn_ids.keys()])
                )
            )
            for c in rows.scalars().all():
                connections.append(
                    ConnectionStub(
                        id=c.id,
                        role=conn_ids[str(c.id)],
                        display_name=c.display_name,
                        connection_type=c.connection_type,
                    )
                )
        bundle = ExportBundle(
            schema_version=SNAPSHOT_SCHEMA_VERSION,
            export_format=EXPORT_FORMAT,
            exported_at=datetime.now(timezone.utc),
            exported_from={
                "tenant_id": str(current_user.tenant_id),
                "project_id": str(project_id),
                "model_id": str(model_id),
            },
            model_display_name=model.display_name,
            model_slug=model.slug,
            snapshot=snap,
        )
    assert bundle is not None
    return ExportPreviewResponse(bundle=bundle, connections_required=connections)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

@router.post(
    "/projects/{project_id}/models/snapshot-import",
    response_model=ImportResponse,
    dependencies=[require_role("modeler")],
)
async def import_model(
    project_id: UUID,
    body: ImportRequest = Body(...),
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ImportResponse:
    if body.bundle.export_format != EXPORT_FORMAT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported export_format {body.bundle.export_format!r}",
        )
    if body.target_project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="target_project_id must match the URL project id",
        )

    new_model_id = uuid.uuid4()
    out: Optional[ImportResponse] = None
    async for tenant_db in get_tenant_db(current_user.tenant_id):
        await _ensure_project_access(project_id, current_user, tenant_db)

        # Validate provided connection ids actually live in this tenant + project.
        if body.connection_mapping:
            target_ids = {UUID(v) for v in body.connection_mapping.values()}
            rows = await tenant_db.execute(
                select(ProjectConnection.id, ProjectConnection.project_id)
                .where(ProjectConnection.id.in_(target_ids))
            )
            owned = {row[0] for row in rows.all() if row[1] == project_id}
            unknown = target_ids - owned
            if unknown:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "connection_mapping points at connection ids that don't "
                        "belong to this project: "
                        + ", ".join(str(u) for u in unknown)
                    ),
                )

        # Pick slug + display name (caller overrides win).
        target_display = (body.target_display_name or body.bundle.model_display_name).strip()
        base_slug = _slugify(body.target_slug or body.bundle.model_slug or target_display)
        # Resolve slug collisions inside the target project by suffixing _2, _3, …
        # with suffix headroom (F-020-19). The actual insert below uses the
        # race-safe helper (F-020-22).
        existing_q = await tenant_db.execute(
            select(Model.slug).where(Model.project_id == project_id)
        )
        existing = {r[0] for r in existing_q.all()}
        target_slug = resolve_slug_collision(base_slug, existing)

        # Rewrite the snapshot for the new model id and remap connections.
        rewritten, missing = prepare_snapshot_for_import(
            body.bundle.snapshot,
            new_model_id=new_model_id,
            connection_mapping=body.connection_mapping,
        )
        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Some sources/targets in the bundle have no connection "
                    "mapping; supply connection_mapping entries for: "
                    + ", ".join(missing)
                ),
            )

        # Strip the old model row's id/project_id from the snapshot so the
        # rehydrator's scalar-update step doesn't try to overwrite project_id
        # with the source tenant's value (it's not in _MODEL_SCALAR_FIELDS,
        # but the explicit slug coming from the snapshot must match the new
        # row we just created).
        rewritten.setdefault("model", {})
        rewritten["model"]["display_name"] = target_display
        # last_deployed_at / deployed_version_id are intentionally not set —
        # the imported model starts undeployed unless caller asks otherwise.

        # Insert the destination Model row race-safely (F-020-22): on a
        # concurrent-import slug clash the helper advances the suffix and
        # retries inside a SAVEPOINT. The final slug is authoritative — stamp
        # it into the snapshot so the rehydrator's scalar update agrees.
        new_model, target_slug = await insert_model_with_slug_retry(
            tenant_db,
            project_id=project_id,
            base_slug=base_slug,
            existing_slugs=existing,
            display_name=target_display,
            new_model_id=new_model_id,
        )
        rewritten["model"]["slug"] = target_slug

        try:
            # F-013-05: import aggregates/pockets must NOT land active pointing
            # at the source model's physical tables. force_aggregate_pending /
            # force_pocket_stale mark them not-yet-materialised, and the
            # rehydrator rebinds their physical_table_name seed segment to this
            # new model's fresh seed (so a refresh builds a distinct table and
            # never clobbers the source's). Mirrors every sibling importer.
            await rehydrate_into_live(
                new_model_id,
                rewritten,
                tenant_db,
                drop_orphan_aggregates=False,
                force_aggregate_pending=True,
                force_pocket_stale=True,
                preserve_destination_seed=True,
                actor=current_user.email or current_user.user_id,
            )
        except (SnapshotSchemaError, SnapshotVersionError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            )

        deployed_id: Optional[UUID] = None
        if body.deploy_immediately:
            # Save a v1 snapshot of the imported live state and deploy it.
            from shared.db.models import ModelVersion
            snap = await snapshot_model(new_model_id, tenant_db)
            v = ModelVersion(
                model_id=new_model_id,
                version_number=1,
                snapshot_json=snap,
                summary=f"Imported from {body.bundle.exported_from.get('model_id')}",
                created_by=current_user.email or current_user.user_id,
            )
            tenant_db.add(v)
            await tenant_db.flush()
            new_model.deployed_version_id = v.id
            new_model.last_deployed_at = datetime.now(timezone.utc)
            deployed_id = v.id

        await tenant_db.commit()

        # Bug-5346: imported aggregates land `pending` (F-013-05). Kick the
        # scheduler to rebuild them now rather than waiting for the next refresh
        # sweep. Fire-and-forget — never fail or delay the import on this.
        _task = asyncio.create_task(
            trigger_model_refresh(current_user.tenant_id, new_model_id)
        )
        _import_rebuild_tasks.add(_task)
        _task.add_done_callback(_import_rebuild_tasks.discard)

        out = ImportResponse(
            model_id=new_model_id,
            slug=target_slug,
            display_name=target_display,
            deployed_version_id=deployed_id,
            missing_connections=[],
        )
    assert out is not None
    return out
