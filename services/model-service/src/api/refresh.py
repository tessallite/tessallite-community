"""
Aggregate refresh policy + run history routes.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from shared.db.models import (
    AggregateDefinition,
    AggregateRefreshPolicy,
    AggregateRefreshRun,
    Model,
)
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    RefreshPolicyCreate,
    RefreshPolicyResponse,
    RefreshRunResponse,
)
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/aggregates/{agg_id}/refresh",
    tags=["refresh"],
)

# Model-level refresh runs (all aggregates combined)
model_refresh_router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/refresh",
    tags=["refresh"],
)


async def _get_scoped_model(db, project_id: UUID, model_id: UUID) -> Model:
    """Prove the model belongs to the path project. Mirrors the same helper in
    ``pockets.py`` and ``row_security.py``."""
    model = await db.get(Model, model_id)
    if model is None or model.project_id != project_id:
        raise HTTPException(status_code=404, detail="Model not found")
    return model


async def _get_scoped_aggregate(
    db, project_id: UUID, model_id: UUID, agg_id: UUID
) -> AggregateDefinition:
    """Prove the full project -> model -> aggregate chain before any read or write.

    Bug-8786: these handlers took ``project_id`` and ``model_id`` as path
    parameters and never used them, querying by ``agg_id`` alone. RBAC gates the
    caller's ROLE (Bug-7891) but not resource ownership, so a same-tenant caller
    authorised for one project could read or mutate another project's refresh
    policy and run history purely by substituting IDs.

    404 rather than 403 on a mismatch: confirming the resource exists elsewhere
    in the tenant would itself leak cross-project information.
    """
    await _get_scoped_model(db, project_id, model_id)
    agg = await db.get(AggregateDefinition, agg_id)
    if agg is None or agg.model_id != model_id:
        raise HTTPException(status_code=404, detail="Aggregate not found")
    return agg


@router.post(
    "/policy",
    response_model=RefreshPolicyResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def upsert_refresh_policy(
    project_id: UUID,
    model_id: UUID,
    agg_id: UUID,
    body: RefreshPolicyCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> RefreshPolicyResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_aggregate(db, project_id, model_id, agg_id)
        result = await db.execute(
            select(AggregateRefreshPolicy).where(
                AggregateRefreshPolicy.aggregate_definition_id == agg_id
            )
        )
        policy = result.scalar_one_or_none()
        if policy is None:
            policy = AggregateRefreshPolicy(
                aggregate_definition_id=agg_id, **body.model_dump()
            )
            db.add(policy)
        else:
            for k, v in body.model_dump().items():
                setattr(policy, k, v)
        await db.commit()
        await db.refresh(policy)
        return RefreshPolicyResponse.model_validate(policy)


@router.get(
    "/policy",
    response_model=RefreshPolicyResponse,
    dependencies=[require_role("viewer")],
)
async def get_refresh_policy(
    project_id: UUID,
    model_id: UUID,
    agg_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> RefreshPolicyResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_aggregate(db, project_id, model_id, agg_id)
        result = await db.execute(
            select(AggregateRefreshPolicy).where(
                AggregateRefreshPolicy.aggregate_definition_id == agg_id
            )
        )
        policy = result.scalar_one_or_none()
        if policy is None:
            raise HTTPException(status_code=404, detail="Refresh policy not configured")
        return RefreshPolicyResponse.model_validate(policy)


def _serialize_run(
    run: AggregateRefreshRun,
    physical_table_name: str | None,
) -> RefreshRunResponse:
    duration_ms: int | None = None
    if run.completed_at is not None and run.started_at is not None:
        duration_ms = int((run.completed_at - run.started_at).total_seconds() * 1000)
    return RefreshRunResponse(
        id=run.id,
        aggregate_definition_id=run.aggregate_definition_id,
        aggregate_table=physical_table_name,
        refresh_mode=run.refresh_mode,
        status=run.status,
        started_at=run.started_at,
        completed_at=run.completed_at,
        duration_ms=duration_ms,
        rows_written=run.rows_written,
        bytes_processed=run.bytes_processed,
        error_message=run.error_message,
        triggered_by=run.triggered_by,
    )


@router.get(
    "/runs",
    response_model=list[RefreshRunResponse],
    dependencies=[require_role("viewer")],
)
async def list_refresh_runs(
    project_id: UUID,
    model_id: UUID,
    agg_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[RefreshRunResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        agg_def = await _get_scoped_aggregate(db, project_id, model_id, agg_id)
        table_name = agg_def.physical_table_name
        result = await db.execute(
            select(AggregateRefreshRun)
            .where(AggregateRefreshRun.aggregate_definition_id == agg_id)
            .order_by(AggregateRefreshRun.started_at.desc())
            .limit(100)
        )
        return [_serialize_run(r, table_name) for r in result.scalars().all()]


@model_refresh_router.get(
    "/runs",
    response_model=list[RefreshRunResponse],
    dependencies=[require_role("viewer")],
)
async def list_model_refresh_runs(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[RefreshRunResponse]:
    """Return all refresh runs across all aggregates for the model, newest first."""
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_model(db, project_id, model_id)
        aggs_result = await db.execute(
            select(AggregateDefinition.id, AggregateDefinition.physical_table_name).where(
                AggregateDefinition.model_id == model_id
            )
        )
        table_by_id: dict[UUID, str] = {row[0]: row[1] for row in aggs_result.all()}
        if not table_by_id:
            return []
        result = await db.execute(
            select(AggregateRefreshRun)
            .where(AggregateRefreshRun.aggregate_definition_id.in_(list(table_by_id.keys())))
            .order_by(AggregateRefreshRun.started_at.desc())
            .limit(200)
        )
        return [
            _serialize_run(r, table_by_id.get(r.aggregate_definition_id))
            for r in result.scalars().all()
        ]
