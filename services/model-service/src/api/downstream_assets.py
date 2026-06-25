"""CRUD endpoints for downstream asset tagging (Impact Analysis)."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select

from shared.db.models import (
    DownstreamAsset,
    GatewayQueryReference,
    Model,
    ModelColumn,
)
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    DownstreamAssetCreate,
    DownstreamAssetResponse,
    DownstreamAssetSummaryResponse,
    DownstreamAssetUpdate,
    GatewayQueryReferenceResponse,
)
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/downstream-assets",
    tags=["impact-analysis"],
)


def _not_found(msg: str = "Not found") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)


async def _get_model(db, project_id: UUID, model_id: UUID) -> Model:
    model = await db.get(Model, model_id)
    if model is None or model.project_id != project_id:
        raise _not_found("Model not found")
    return model


def _to_response(asset: DownstreamAsset) -> DownstreamAssetResponse:
    data = DownstreamAssetResponse.model_validate(asset)
    data.column_ids = [c.id for c in asset.columns]
    return data


@router.get(
    "",
    response_model=list[DownstreamAssetResponse],
    dependencies=[require_role("viewer")],
)
async def list_downstream_assets(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[DownstreamAssetResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        result = await db.execute(
            select(DownstreamAsset)
            .where(DownstreamAsset.model_id == model_id)
            .order_by(DownstreamAsset.created_at)
        )
        assets = result.scalars().all()
        return [_to_response(a) for a in assets]


@router.get(
    "/summary",
    response_model=DownstreamAssetSummaryResponse,
    dependencies=[require_role("viewer")],
)
async def downstream_asset_summary(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> DownstreamAssetSummaryResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        result = await db.execute(
            select(
                DownstreamAsset.asset_type,
                func.count(DownstreamAsset.id),
            )
            .where(DownstreamAsset.model_id == model_id)
            .group_by(DownstreamAsset.asset_type)
        )
        rows = result.all()
        by_type = {r[0]: r[1] for r in rows}
        return DownstreamAssetSummaryResponse(
            total=sum(by_type.values()),
            by_type=by_type,
        )


@router.post(
    "",
    response_model=DownstreamAssetResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_downstream_asset(
    project_id: UUID,
    model_id: UUID,
    body: DownstreamAssetCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> DownstreamAssetResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)

        asset = DownstreamAsset(
            model_id=model_id,
            asset_type=body.asset_type,
            asset_name=body.asset_name,
            asset_url=body.asset_url,
            owner=body.owner,
            notes=body.notes,
        )

        if body.column_ids:
            cols = (
                await db.execute(
                    select(ModelColumn).where(ModelColumn.id.in_(body.column_ids))
                )
            ).scalars().all()
            asset.columns = list(cols)

        db.add(asset)
        await db.commit()
        await db.refresh(asset)
        return _to_response(asset)


@router.put(
    "/{asset_id}",
    response_model=DownstreamAssetResponse,
    dependencies=[require_role("modeler")],
)
async def update_downstream_asset(
    project_id: UUID,
    model_id: UUID,
    asset_id: UUID,
    body: DownstreamAssetUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> DownstreamAssetResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        asset = await db.get(DownstreamAsset, asset_id)
        if asset is None or asset.model_id != model_id:
            raise _not_found("Downstream asset not found")

        for field in ("asset_type", "asset_name", "asset_url", "owner", "notes"):
            val = getattr(body, field, None)
            if val is not None:
                setattr(asset, field, val)

        if body.column_ids is not None:
            cols = (
                await db.execute(
                    select(ModelColumn).where(ModelColumn.id.in_(body.column_ids))
                )
            ).scalars().all()
            asset.columns = list(cols)

        await db.commit()
        await db.refresh(asset)
        return _to_response(asset)


@router.delete(
    "/{asset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def delete_downstream_asset(
    project_id: UUID,
    model_id: UUID,
    asset_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        asset = await db.get(DownstreamAsset, asset_id)
        if asset is None or asset.model_id != model_id:
            raise _not_found("Downstream asset not found")
        await db.delete(asset)
        await db.commit()


# ---------------------------------------------------------------------------
# Query references (read-only — populated by impact scan)
# ---------------------------------------------------------------------------

query_ref_router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/impact/query-references",
    tags=["impact-analysis"],
)


@query_ref_router.get(
    "",
    response_model=list[GatewayQueryReferenceResponse],
    dependencies=[require_role("viewer")],
)
async def list_query_references(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[GatewayQueryReferenceResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        result = await db.execute(
            select(GatewayQueryReference)
            .where(GatewayQueryReference.model_id == model_id)
            .order_by(GatewayQueryReference.hit_count.desc())
        )
        return [
            GatewayQueryReferenceResponse.model_validate(r)
            for r in result.scalars().all()
        ]
