"""
Aggregate refresh policy + run history routes.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from shared.db.models import AggregateDefinition, AggregateRefreshPolicy, AggregateRefreshRun
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    RefreshPolicyCreate,
    RefreshPolicyResponse,
    RefreshRunResponse,
)
from src.auth.middleware import CurrentUser, forbid_embed_user

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/aggregates/{agg_id}/refresh",
    tags=["refresh"],
)

# Model-level refresh runs (all aggregates combined)
model_refresh_router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/refresh",
    tags=["refresh"],
)


@router.post(
    "/policy",
    response_model=RefreshPolicyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upsert_refresh_policy(
    project_id: UUID,
    model_id: UUID,
    agg_id: UUID,
    body: RefreshPolicyCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> RefreshPolicyResponse:
    async for db in get_tenant_db(current_user.tenant_id):
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


@router.get("/policy", response_model=RefreshPolicyResponse)
async def get_refresh_policy(
    project_id: UUID,
    model_id: UUID,
    agg_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> RefreshPolicyResponse:
    async for db in get_tenant_db(current_user.tenant_id):
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


@router.get("/runs", response_model=list[RefreshRunResponse])
async def list_refresh_runs(
    project_id: UUID,
    model_id: UUID,
    agg_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[RefreshRunResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        agg_def = await db.get(AggregateDefinition, agg_id)
        table_name = agg_def.physical_table_name if agg_def is not None else None
        result = await db.execute(
            select(AggregateRefreshRun)
            .where(AggregateRefreshRun.aggregate_definition_id == agg_id)
            .order_by(AggregateRefreshRun.started_at.desc())
            .limit(100)
        )
        return [_serialize_run(r, table_name) for r in result.scalars().all()]


@model_refresh_router.get("/runs", response_model=list[RefreshRunResponse])
async def list_model_refresh_runs(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[RefreshRunResponse]:
    """Return all refresh runs across all aggregates for the model, newest first."""
    async for db in get_tenant_db(current_user.tenant_id):
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
