"""Collibra integration API — config CRUD, validate, preview, sync.

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
from shared.db.models import Model, Project, CollibraConnection, CollibraSyncRun, CollibraObjectMapping
from shared.db.session import get_tenant_db

from shared.schemas.pydantic_models import (
    CollibraConnectionCreate,
    CollibraConnectionResponse,
    CollibraConnectionUpdate,
    CollibraExportPreviewRequest,
    CollibraExportPreviewResponse,
    CollibraObjectMappingResponse,
    CollibraSyncRequest,
    CollibraSyncResponse,
    CollibraSyncRunResponse,
    CollibraValidateRequest,
    CollibraValidateResponse,
)
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role
from src.collibra_client import CollibraClient
from src.collibra_mapper import map_graph_to_collibra
from src.collibra_sync import run_collibra_sync
from src.governance_exporter import build_governance_graph
from src.governance_helpers import (
    decrypt_credentials,
    encrypt_credentials,
    get_model,
    not_found,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/collibra",
    tags=["collibra"],
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


def _validate_collibra_preview_scope(body: CollibraExportPreviewRequest) -> None:
    if not body.include_business_assets and not body.include_technical_assets:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "empty_collibra_preview_scope",
                "message": "At least one of include_business_assets or include_technical_assets must be true.",
            },
        )


def _connection_to_response(c: CollibraConnection) -> CollibraConnectionResponse:
    return CollibraConnectionResponse(
        id=c.id,
        project_id=c.project_id,
        model_id=c.model_id,
        display_name=c.display_name,
        base_url=c.base_url,
        auth_type=c.auth_type,
        community_id=c.community_id,
        domain_id=c.domain_id,
        sync_scope=c.sync_scope,
        sync_mode=c.sync_mode,
        is_active=c.is_active,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )

# ---------------------------------------------------------------------------
# Config CRUD
# ---------------------------------------------------------------------------

@router.get(
    "/config",
    response_model=list[CollibraConnectionResponse],
    dependencies=[require_role("viewer")],
)
async def list_collibra_configs(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[CollibraConnectionResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await get_model(db, project_id, model_id)
        result = await db.execute(
            select(CollibraConnection)
            .where(CollibraConnection.model_id == model_id)
            .order_by(CollibraConnection.created_at)
        )
        return [_connection_to_response(c) for c in result.scalars().all()]

@router.get(
    "/config/{connection_id}",
    response_model=CollibraConnectionResponse,
    dependencies=[require_role("viewer")],
)
async def get_collibra_config(
    project_id: UUID,
    model_id: UUID,
    connection_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> CollibraConnectionResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await get_model(db, project_id, model_id)
        conn = await db.get(CollibraConnection, connection_id)
        if conn is None or conn.model_id != model_id:
            raise not_found("Collibra connection not found")
        return _connection_to_response(conn)

@router.post(
    "/config",
    response_model=CollibraConnectionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("admin")],
)
async def create_collibra_config(
    project_id: UUID,
    model_id: UUID,
    body: CollibraConnectionCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> CollibraConnectionResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await get_model(db, project_id, model_id)
        encrypted = encrypt_credentials({"token": body.token})

        conn = CollibraConnection(
            project_id=project_id,
            model_id=model_id,
            display_name=body.display_name,
            base_url=body.base_url,
            auth_type=body.auth_type,
            encrypted_credentials=encrypted,
            community_id=body.community_id,
            domain_id=body.domain_id,
            asset_type_mapping=body.asset_type_mapping,
            relation_type_mapping=body.relation_type_mapping,
            responsibility_mapping=body.responsibility_mapping,
            sync_scope=body.sync_scope,
            sync_mode=body.sync_mode,
        )
        db.add(conn)
        await db.flush()
        await audit(
            db,
            action="collibra.config.create",
            severity="info",
            actor_email=current_user.email,
            target_type="collibra_connection",
            target_id=conn.id,
            target_name=conn.display_name,
            detail={
                "base_url": conn.base_url,
                "sync_scope": conn.sync_scope,
                "sync_mode": conn.sync_mode,
            },
        )
        await db.commit()
        await db.refresh(conn)
        return _connection_to_response(conn)

@router.put(
    "/config/{connection_id}",
    response_model=CollibraConnectionResponse,
    dependencies=[require_role("admin")],
)
async def update_collibra_config(
    project_id: UUID,
    model_id: UUID,
    connection_id: UUID,
    body: CollibraConnectionUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> CollibraConnectionResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await get_model(db, project_id, model_id)
        conn = await db.get(CollibraConnection, connection_id)
        if conn is None or conn.model_id != model_id:
            raise not_found("Collibra connection not found")

        if body.display_name is not None:
            conn.display_name = body.display_name
        if body.base_url is not None:
            conn.base_url = body.base_url
        if body.auth_type is not None:
            conn.auth_type = body.auth_type
        if body.token is not None:
            conn.encrypted_credentials = encrypt_credentials({"token": body.token})
        if body.community_id is not None:
            conn.community_id = body.community_id
        if body.domain_id is not None:
            conn.domain_id = body.domain_id
        if body.asset_type_mapping is not None:
            conn.asset_type_mapping = body.asset_type_mapping
        if body.relation_type_mapping is not None:
            conn.relation_type_mapping = body.relation_type_mapping
        if body.responsibility_mapping is not None:
            conn.responsibility_mapping = body.responsibility_mapping
        if body.sync_scope is not None:
            conn.sync_scope = body.sync_scope
        if body.sync_mode is not None:
            conn.sync_mode = body.sync_mode
        if body.is_active is not None:
            conn.is_active = body.is_active

        changed_fields = sorted(body.model_fields_set - {"token"})
        await audit(
            db,
            action="collibra.config.update",
            severity="info",
            actor_email=current_user.email,
            target_type="collibra_connection",
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
async def delete_collibra_config(
    project_id: UUID,
    model_id: UUID,
    connection_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        await get_model(db, project_id, model_id)
        conn = await db.get(CollibraConnection, connection_id)
        if conn is None or conn.model_id != model_id:
            raise not_found("Collibra connection not found")
        conn_name = conn.display_name
        await audit(
            db,
            action="collibra.config.delete",
            severity="critical",
            actor_email=current_user.email,
            target_type="collibra_connection",
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
    response_model=CollibraValidateResponse,
    dependencies=[require_role("modeler")],
)
async def validate_collibra_connection(
    project_id: UUID,
    model_id: UUID,
    body: CollibraValidateRequest,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> CollibraValidateResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await get_model(db, project_id, model_id)
        conn = await db.get(CollibraConnection, body.connection_id)
        if conn is None or conn.model_id != model_id:
            raise not_found("Collibra connection not found")

        creds = decrypt_credentials(conn.encrypted_credentials)
        token = creds.get("token", "")

        client = CollibraClient(base_url=conn.base_url, token=token)
        status_result = await client.validate_connection()

        return CollibraValidateResponse(
            ok=status_result.ok,
            simulated=status_result.simulated,
            base_url=status_result.base_url,
            community_found=status_result.community_found,
            domain_found=status_result.domain_found,
            missing_asset_types=status_result.missing_asset_types,
            missing_relation_types=status_result.missing_relation_types,
            warnings=status_result.warnings,
        )

# ---------------------------------------------------------------------------
# Asset types (read-only reference for frontend mapping config)
# ---------------------------------------------------------------------------

@router.get(
    "/asset-types",
    dependencies=[require_role("viewer")],
)
async def get_collibra_asset_types(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> dict:
    """Return the default Collibra asset type and relation type mappings."""
    async for db in get_tenant_db(current_user.tenant_id):
        await get_model(db, project_id, model_id)
    from src.collibra_mapper import COLLIBRA_ASSET_TYPE_MAP, COLLIBRA_RELATION_TYPE_MAP
    return {
        "asset_types": COLLIBRA_ASSET_TYPE_MAP,
        "relation_types": COLLIBRA_RELATION_TYPE_MAP,
    }

# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

@router.post(
    "/sync",
    response_model=CollibraSyncResponse,
    dependencies=[require_role("modeler")],
)
async def collibra_sync(
    project_id: UUID,
    model_id: UUID,
    body: CollibraSyncRequest,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> CollibraSyncResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        model = await get_model(db, project_id, model_id)
        _require_exportable_snapshot(model, body.export_draft)

        conn = await db.get(CollibraConnection, body.connection_id)
        if conn is None or conn.model_id != model_id:
            raise not_found("Collibra connection not found")
        await audit(
            db,
            action="collibra.sync.trigger",
            severity="info",
            actor_email=current_user.email,
            target_type="collibra_connection",
            target_id=conn.id,
            target_name=conn.display_name,
            detail={
                "dry_run": body.dry_run,
                "deprecate_missing": body.deprecate_missing,
                "export_draft": body.export_draft,
            },
        )

        project = await db.execute(
            select(Project).where(Project.id == project_id)
        )
        proj = project.scalar_one_or_none()

        try:
            run = await run_collibra_sync(
                db,
                connection_id=conn.id,
                project_id=project_id,
                model_id=model_id,
                project_slug=proj.slug if proj else "",
                model_slug=model.slug,
                dry_run=body.dry_run,
                include_technical=body.include_technical_assets,
                include_aggregates=body.include_aggregates,
                include_downstream_assets=body.include_downstream_assets,
                include_glossary=body.include_glossary,
                include_security_tags=body.include_data_tags,
                include_responsibilities=body.include_responsibilities,
                deprecate_missing=body.deprecate_missing,
                export_draft=getattr(body, "export_draft", False),
            )

            return CollibraSyncResponse(
                run_id=run.id,
                status=run.status,
                assets_total=run.assets_total,
                relations_total=run.relations_total,
                attributes_total=run.attributes_total,
                responsibilities_total=run.responsibilities_total,
                assets_created=run.assets_created,
                assets_updated=run.assets_updated,
                relations_created=run.relations_created,
                relations_updated=run.relations_updated,
                warnings=[],
                error_message=run.error_message,
            )
        except Exception as exc:
            logger.exception("Collibra sync failed")
            return CollibraSyncResponse(
                run_id=_uuid.UUID("00000000-0000-0000-0000-000000000000"),
                status="failed",
                error_message=str(exc),
            )

# ---------------------------------------------------------------------------
# Export preview
# ---------------------------------------------------------------------------

@router.post(
    "/export-preview",
    response_model=CollibraExportPreviewResponse,
    dependencies=[require_role("viewer")],
)
async def collibra_export_preview(
    project_id: UUID,
    model_id: UUID,
    body: CollibraExportPreviewRequest | None = None,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> CollibraExportPreviewResponse:
    if body is None:
        body = CollibraExportPreviewRequest()
    _validate_collibra_preview_scope(body)
    async for db in get_tenant_db(current_user.tenant_id):
        model = await get_model(db, project_id, model_id)
        _require_exportable_snapshot(model, body.export_draft)

        conn = None
        if body.connection_id is not None:
            conn = await db.get(CollibraConnection, body.connection_id)
            if conn is None or conn.model_id != model_id:
                raise not_found("Collibra connection not found")

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
            include_business_assets=body.include_business_assets,
            include_technical=body.include_technical_assets,
            include_hidden_objects=body.include_hidden_objects,
            include_aggregates=body.include_aggregates,
            include_downstream_assets=body.include_downstream_assets,
            include_glossary=body.include_glossary,
            include_security_tags=body.include_data_tags,
            export_draft=body.export_draft,
        )

        payload = map_graph_to_collibra(
            graph,
            domain_id=(conn.domain_id or "") if conn else "",
            asset_type_mapping=(conn.asset_type_mapping or {}) if conn else {},
            relation_type_mapping=(conn.relation_type_mapping or {}) if conn else {},
            responsibility_mapping=(conn.responsibility_mapping or {}) if conn else {},
        )
        by_type: dict[str, int] = {}
        for asset in payload.assets:
            by_type[asset.asset_type] = by_type.get(asset.asset_type, 0) + 1

        return CollibraExportPreviewResponse(
            assets_total=len(payload.assets),
            relations_total=len(payload.relations),
            attributes_total=sum(len(asset.attributes) for asset in payload.assets),
            responsibilities_total=(
                len(payload.responsibilities) if body.include_responsibilities else 0
            ),
            by_asset_type=by_type,
            warnings=[],
        )

# ---------------------------------------------------------------------------
# Sync runs (read-only)
# ---------------------------------------------------------------------------

@router.get(
    "/runs",
    response_model=list[CollibraSyncRunResponse],
    dependencies=[require_role("viewer")],
)
async def list_collibra_runs(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[CollibraSyncRunResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await get_model(db, project_id, model_id)
        result = await db.execute(
            select(CollibraSyncRun)
            .where(CollibraSyncRun.model_id == model_id)
            .order_by(CollibraSyncRun.started_at.desc())
            .limit(50)
        )
        return [CollibraSyncRunResponse.model_validate(r) for r in result.scalars().all()]

@router.get(
    "/runs/{run_id}",
    response_model=CollibraSyncRunResponse,
    dependencies=[require_role("viewer")],
)
async def get_collibra_run(
    project_id: UUID,
    model_id: UUID,
    run_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> CollibraSyncRunResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await get_model(db, project_id, model_id)
        run = await db.get(CollibraSyncRun, run_id)
        if run is None or run.model_id != model_id:
            raise not_found("Collibra sync run not found")
        return CollibraSyncRunResponse.model_validate(run)

# ---------------------------------------------------------------------------
# Object mappings (read-only)
# ---------------------------------------------------------------------------

@router.get(
    "/mappings",
    response_model=list[CollibraObjectMappingResponse],
    dependencies=[require_role("viewer")],
)
async def list_collibra_mappings(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[CollibraObjectMappingResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await get_model(db, project_id, model_id)
        result = await db.execute(
            select(CollibraObjectMapping)
            .join(CollibraConnection)
            .where(CollibraConnection.model_id == model_id)
            .order_by(CollibraObjectMapping.tessallite_object_type)
            .limit(500)
        )
        return [
            CollibraObjectMappingResponse.model_validate(m)
            for m in result.scalars().all()
        ]
