"""Solidatus integration API — config CRUD, validate, preview, sync.

Phase 1: config CRUD, validate (fake client).
Phase 2: export-preview from GovernanceGraph.
Phase 4: mapper + dry-run sync (this file).

Tokens are Fernet-encrypted before storage and never returned in responses.
"""
from __future__ import annotations

import logging
import uuid as _uuid
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from shared.audit.logger import audit
from shared.db.models import Model, Project, SolidatusConnection, SolidatusSyncRun, SolidatusObjectMapping
from shared.db.session import get_tenant_db

from shared.schemas.pydantic_models import (
    SolidatusConnectionCreate,
    SolidatusConnectionResponse,
    SolidatusConnectionUpdate,
    SolidatusExportPreviewRequest,
    SolidatusExportPreviewResponse,
    SolidatusObjectMappingResponse,
    SolidatusSyncRequest,
    SolidatusSyncResponse,
    SolidatusSyncRunResponse,
    SolidatusValidateRequest,
    SolidatusValidateResponse,
)
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role
from src.governance_exporter import build_governance_graph
from src.governance_helpers import (
    decrypt_credentials,
    encrypt_credentials,
    get_model,
    not_found,
)
from src.solidatus_client import SolidatusClient
from src.solidatus_sync import run_solidatus_sync

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/solidatus",
    tags=["solidatus"],
)


def _require_exportable_snapshot(model: Model, export_draft: bool) -> None:
    if not export_draft and model.deployed_version_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "model_not_deployed",
                "message": "Model has no deployed version. Enable export_draft to export draft state.",
            },
        )


def _solidatus_effective_dry_run(body: SolidatusSyncRequest) -> bool:
    if body.mode == "dry_run":
        return True
    if body.mode != "push":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "invalid_solidatus_sync_mode",
                "message": "Solidatus sync mode must be one of: dry_run, push.",
                "mode": body.mode,
            },
        )
    if body.dry_run:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "inconsistent_solidatus_sync_mode",
                "message": "dry_run=true is inconsistent with mode='push'. Use mode='dry_run'.",
            },
        )
    return False

def _connection_to_response(c: SolidatusConnection) -> SolidatusConnectionResponse:
    return SolidatusConnectionResponse(
        id=c.id,
        project_id=c.project_id,
        model_id=c.model_id,
        display_name=c.display_name,
        base_url=c.base_url,
        auth_type=c.auth_type,
        workspace_id=c.workspace_id,
        model_ref=c.model_ref,
        sync_scope=c.sync_scope,
        is_active=c.is_active,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )

# ---------------------------------------------------------------------------
# Config CRUD
# ---------------------------------------------------------------------------

@router.get(
    "/config",
    response_model=list[SolidatusConnectionResponse],
    dependencies=[require_role("viewer")],
)
async def list_solidatus_configs(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[SolidatusConnectionResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await get_model(db, project_id, model_id)
        result = await db.execute(
            select(SolidatusConnection)
            .where(SolidatusConnection.model_id == model_id)
            .order_by(SolidatusConnection.created_at)
        )
        return [_connection_to_response(c) for c in result.scalars().all()]

@router.get(
    "/config/{connection_id}",
    response_model=SolidatusConnectionResponse,
    dependencies=[require_role("viewer")],
)
async def get_solidatus_config(
    project_id: UUID,
    model_id: UUID,
    connection_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> SolidatusConnectionResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await get_model(db, project_id, model_id)
        conn = await db.get(SolidatusConnection, connection_id)
        if conn is None or conn.model_id != model_id:
            raise not_found("Solidatus connection not found")
        return _connection_to_response(conn)

@router.post(
    "/config",
    response_model=SolidatusConnectionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("admin")],
)
async def create_solidatus_config(
    project_id: UUID,
    model_id: UUID,
    body: SolidatusConnectionCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> SolidatusConnectionResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await get_model(db, project_id, model_id)
        encrypted = encrypt_credentials({"token": body.token})

        conn = SolidatusConnection(
            project_id=project_id,
            model_id=model_id,
            display_name=body.display_name,
            base_url=body.base_url,
            auth_type=body.auth_type,
            encrypted_credentials=encrypted,
            workspace_id=body.workspace_id,
            model_ref=body.model_ref,
            sync_scope=body.sync_scope,
        )
        db.add(conn)
        await db.flush()
        await audit(
            db,
            action="solidatus.config.create",
            severity="info",
            actor_email=current_user.email,
            target_type="solidatus_connection",
            target_id=conn.id,
            target_name=conn.display_name,
            detail={
                "base_url": conn.base_url,
                "sync_scope": conn.sync_scope,
                "workspace_id": conn.workspace_id,
                "model_ref": conn.model_ref,
            },
        )
        await db.commit()
        await db.refresh(conn)
        return _connection_to_response(conn)

@router.put(
    "/config/{connection_id}",
    response_model=SolidatusConnectionResponse,
    dependencies=[require_role("admin")],
)
async def update_solidatus_config(
    project_id: UUID,
    model_id: UUID,
    connection_id: UUID,
    body: SolidatusConnectionUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> SolidatusConnectionResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await get_model(db, project_id, model_id)
        conn = await db.get(SolidatusConnection, connection_id)
        if conn is None or conn.model_id != model_id:
            raise not_found("Solidatus connection not found")

        if body.display_name is not None:
            conn.display_name = body.display_name
        if body.base_url is not None:
            conn.base_url = body.base_url
        if body.auth_type is not None:
            conn.auth_type = body.auth_type
        if body.token is not None:
            conn.encrypted_credentials = encrypt_credentials({"token": body.token})
        if body.workspace_id is not None:
            conn.workspace_id = body.workspace_id
        if body.model_ref is not None:
            conn.model_ref = body.model_ref
        if body.sync_scope is not None:
            conn.sync_scope = body.sync_scope
        if body.is_active is not None:
            conn.is_active = body.is_active

        changed_fields = sorted(body.model_fields_set - {"token"})
        await audit(
            db,
            action="solidatus.config.update",
            severity="info",
            actor_email=current_user.email,
            target_type="solidatus_connection",
            target_id=conn.id,
            target_name=conn.display_name,
            detail={
                "fields": changed_fields,
                "credentials_updated": body.token is not None,
            },
        )
        await db.commit()
        await db.refresh(conn)
        return _connection_to_response(conn)

@router.delete(
    "/config/{connection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("admin")],
)
async def delete_solidatus_config(
    project_id: UUID,
    model_id: UUID,
    connection_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        await get_model(db, project_id, model_id)
        conn = await db.get(SolidatusConnection, connection_id)
        if conn is None or conn.model_id != model_id:
            raise not_found("Solidatus connection not found")
        conn_name = conn.display_name
        await audit(
            db,
            action="solidatus.config.delete",
            severity="critical",
            actor_email=current_user.email,
            target_type="solidatus_connection",
            target_id=connection_id,
            target_name=conn_name,
        )
        await db.delete(conn)
        await db.commit()

# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------

@router.post(
    "/validate",
    response_model=SolidatusValidateResponse,
    dependencies=[require_role("modeler")],
)
async def validate_solidatus_connection(
    project_id: UUID,
    model_id: UUID,
    body: SolidatusValidateRequest,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> SolidatusValidateResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await get_model(db, project_id, model_id)
        conn = await db.get(SolidatusConnection, body.connection_id)
        if conn is None or conn.model_id != model_id:
            raise not_found("Solidatus connection not found")

        creds = decrypt_credentials(conn.encrypted_credentials)
        token = creds.get("token", "")

        client = SolidatusClient(base_url=conn.base_url, token=token)
        status_result = await client.validate_connection()

        return SolidatusValidateResponse(
            ok=status_result.ok,
            simulated=status_result.simulated,
            base_url=status_result.base_url,
            workspace_found=status_result.workspace_found,
            model_ref_found=status_result.model_ref_found,
            warnings=status_result.warnings,
        )

# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

@router.post(
    "/sync",
    response_model=SolidatusSyncResponse,
    dependencies=[require_role("modeler")],
)
async def solidatus_sync(
    project_id: UUID,
    model_id: UUID,
    body: SolidatusSyncRequest,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> SolidatusSyncResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        model = await get_model(db, project_id, model_id)
        _require_exportable_snapshot(model, body.export_draft)
        effective_dry_run = _solidatus_effective_dry_run(body)

        conn = await db.get(SolidatusConnection, body.connection_id)
        if conn is None or conn.model_id != model_id:
            raise not_found("Solidatus connection not found")
        await audit(
            db,
            action="solidatus.sync.trigger",
            severity="info",
            actor_email=current_user.email,
            target_type="solidatus_connection",
            target_id=conn.id,
            target_name=conn.display_name,
            detail={
                "mode": body.mode,
                "dry_run": effective_dry_run,
                "deprecate_missing": body.deprecate_missing,
                "export_draft": body.export_draft,
            },
        )

        project = await db.execute(
            select(Project).where(Project.id == project_id)
        )
        proj = project.scalar_one_or_none()

        try:
            run = await run_solidatus_sync(
                db,
                connection_id=conn.id,
                project_id=project_id,
                model_id=model_id,
                project_slug=proj.slug if proj else "",
                model_slug=model.slug,
                dry_run=effective_dry_run,
                include_technical=body.include_technical,
                include_aggregates=body.include_aggregates,
                include_downstream_assets=body.include_downstream_assets,
                include_glossary=body.include_glossary,
                include_security_tags=body.include_security_tags,
                export_draft=body.export_draft,
                deprecate_missing=body.deprecate_missing,
            )

            return SolidatusSyncResponse(
                run_id=run.id,
                status=run.status,
                nodes_total=run.nodes_total,
                edges_total=run.edges_total,
                nodes_created=run.nodes_created,
                nodes_updated=run.nodes_updated,
                edges_created=run.edges_created,
                edges_updated=run.edges_updated,
                error_message=run.error_message,
            )
        except Exception as exc:
            logger.exception("Solidatus sync failed")
            return SolidatusSyncResponse(
                run_id=_uuid.UUID("00000000-0000-0000-0000-000000000000"),
                status="failed",
                error_message=str(exc),
            )

# ---------------------------------------------------------------------------
# Export preview
# ---------------------------------------------------------------------------

@router.post(
    "/export-preview",
    response_model=SolidatusExportPreviewResponse,
    dependencies=[require_role("viewer")],
)
async def solidatus_export_preview(
    project_id: UUID,
    model_id: UUID,
    body: SolidatusExportPreviewRequest | None = None,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> SolidatusExportPreviewResponse:
    if body is None:
        body = SolidatusExportPreviewRequest()
    async for db in get_tenant_db(current_user.tenant_id):
        model = await get_model(db, project_id, model_id)
        _require_exportable_snapshot(model, body.export_draft)

        if body.connection_id is not None:
            conn = await db.get(SolidatusConnection, body.connection_id)
            if conn is None or conn.model_id != model_id:
                raise not_found("Solidatus connection not found")

        project = await db.execute(
            select(Project).where(Project.id == project_id)
        )
        proj = project.scalar_one_or_none()

        graph = await build_governance_graph(
            db,
            project_id=project_id,
            model_id=model_id,
            project_slug=proj.slug if proj else "",
            model_slug=model.slug,
            include_technical=body.include_technical,
            include_aggregates=body.include_aggregates,
            include_downstream_assets=body.include_downstream_assets,
            include_glossary=body.include_glossary,
            include_security_tags=body.include_security_tags,
            export_draft=body.export_draft,
        )

        by_type: dict[str, int] = {}
        for node in graph.nodes:
            by_type[node.object_type] = by_type.get(node.object_type, 0) + 1

        return SolidatusExportPreviewResponse(
            nodes_total=len(graph.nodes),
            edges_total=len(graph.edges),
            by_type=by_type,
            warnings=[],
        )

# ---------------------------------------------------------------------------
# Sync runs (read-only)
# ---------------------------------------------------------------------------

@router.get(
    "/runs",
    response_model=list[SolidatusSyncRunResponse],
    dependencies=[require_role("viewer")],
)
async def list_solidatus_runs(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[SolidatusSyncRunResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await get_model(db, project_id, model_id)
        result = await db.execute(
            select(SolidatusSyncRun)
            .where(SolidatusSyncRun.model_id == model_id)
            .order_by(SolidatusSyncRun.started_at.desc())
            .limit(50)
        )
        return [SolidatusSyncRunResponse.model_validate(r) for r in result.scalars().all()]

@router.get(
    "/runs/{run_id}",
    response_model=SolidatusSyncRunResponse,
    dependencies=[require_role("viewer")],
)
async def get_solidatus_run(
    project_id: UUID,
    model_id: UUID,
    run_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> SolidatusSyncRunResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await get_model(db, project_id, model_id)
        run = await db.get(SolidatusSyncRun, run_id)
        if run is None or run.model_id != model_id:
            raise not_found("Solidatus sync run not found")
        return SolidatusSyncRunResponse.model_validate(run)

# ---------------------------------------------------------------------------
# Object mappings (read-only)
# ---------------------------------------------------------------------------

@router.get(
    "/mappings",
    response_model=list[SolidatusObjectMappingResponse],
    dependencies=[require_role("viewer")],
)
async def list_solidatus_mappings(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[SolidatusObjectMappingResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await get_model(db, project_id, model_id)
        result = await db.execute(
            select(SolidatusObjectMapping)
            .join(SolidatusConnection)
            .where(SolidatusConnection.model_id == model_id)
            .order_by(SolidatusObjectMapping.tessallite_object_type)
            .limit(500)
        )
        return [
            SolidatusObjectMappingResponse.model_validate(m)
            for m in result.scalars().all()
        ]
