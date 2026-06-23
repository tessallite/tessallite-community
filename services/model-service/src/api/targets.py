"""
DataTarget CRUD routes (aggregate output destinations).
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from shared.db.models import DataTarget, Model, ProjectConnection
from shared.db.session import get_tenant_db
from shared.schemas.connection_type import (
    ALLOWED_CONNECTION_TYPES,
    normalize_connection_type,
)
from shared.schemas.pydantic_models import DataTargetCreate, DataTargetResponse, DataTargetUpdate
from src.api._scope import ensure_model_in_project
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

# Connector types Tessallite can materialise an aggregate INTO. A subset of
# ALLOWED_CONNECTION_TYPES — every canonical connector is a valid aggregate
# target today (the optimizer/scheduler dispatch on PG-family, bigquery,
# hadoop_spark), so the supported-target matrix equals the canonical set.
SUPPORTED_TARGET_TYPES = frozenset(ALLOWED_CONNECTION_TYPES)


async def _validate_target_connection(
    db, project_id, model_id, project_connection_id
) -> None:
    """F-009-18: fail early when a DataTarget references an invalid connection.

    The optimizer/scheduler otherwise surface an unsupported or cross-project
    target only later, as a confusing source-side SQL syntax error. Validate
    at write time that the connection exists, belongs to the same project as
    the model, and is a supported target connector.
    """
    model = await ensure_model_in_project(
        db, project_id=project_id, model_id=model_id
    )
    conn = await db.get(ProjectConnection, project_connection_id)
    if conn is None:
        raise HTTPException(
            status_code=422,
            detail="project_connection_id does not reference an existing connection",
        )
    if conn.project_id != model.project_id:
        raise HTTPException(
            status_code=422,
            detail="Target connection belongs to a different project",
        )
    canonical = normalize_connection_type(conn.connection_type)
    if canonical not in SUPPORTED_TARGET_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Connection type {conn.connection_type!r} is not a supported "
                f"aggregate target"
            ),
        )

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/targets", tags=["targets"]
)


@router.post(
    "",
    response_model=DataTargetResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_target(
    project_id: UUID,
    model_id: UUID,
    body: DataTargetCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> DataTargetResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _validate_target_connection(
            db, project_id, model_id, body.project_connection_id
        )
        target = DataTarget(model_id=model_id, **body.model_dump())
        db.add(target)
        await db.flush()
        model = await db.get(Model, model_id)
        if model is not None and model.target_id is None:
            model.target_id = target.id
        await db.commit()
        await db.refresh(target)
        return DataTargetResponse.model_validate(target)


@router.get("", response_model=list[DataTargetResponse], dependencies=[require_role("viewer")])
async def list_targets(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[DataTargetResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        result = await db.execute(
            select(DataTarget).where(DataTarget.model_id == model_id)
        )
        return [DataTargetResponse.model_validate(t) for t in result.scalars().all()]


@router.get("/{target_id}", response_model=DataTargetResponse, dependencies=[require_role("viewer")])
async def get_target(
    project_id: UUID,
    model_id: UUID,
    target_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> DataTargetResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        t = await db.get(DataTarget, target_id)
        if t is None or t.model_id != model_id:
            raise HTTPException(status_code=404, detail="DataTarget not found")
        return DataTargetResponse.model_validate(t)


@router.patch(
    "/{target_id}",
    response_model=DataTargetResponse,
    dependencies=[require_role("modeler")],
)
async def update_target(
    project_id: UUID,
    model_id: UUID,
    target_id: UUID,
    body: DataTargetUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> DataTargetResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        t = await db.get(DataTarget, target_id)
        if t is None or t.model_id != model_id:
            raise HTTPException(status_code=404, detail="DataTarget not found")
        updates = body.model_dump(exclude_unset=True)
        # F-009-18: re-validate when the referenced connection is changed.
        new_conn_id = updates.get("project_connection_id")
        if new_conn_id is not None and new_conn_id != t.project_connection_id:
            await _validate_target_connection(db, project_id, model_id, new_conn_id)
        for k, v in updates.items():
            setattr(t, k, v)
        await db.commit()
        await db.refresh(t)
        return DataTargetResponse.model_validate(t)


@router.delete(
    "/{target_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def delete_target(
    project_id: UUID,
    model_id: UUID,
    target_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        t = await db.get(DataTarget, target_id)
        if t is None or t.model_id != model_id:
            raise HTTPException(status_code=404, detail="DataTarget not found")
        model = await db.get(Model, model_id)
        await db.delete(t)
        await db.flush()
        if model is not None and model.target_id == target_id:
            remaining = await db.execute(
                select(DataTarget.id).where(DataTarget.model_id == model_id).limit(1)
            )
            model.target_id = remaining.scalar_one_or_none()
        await db.commit()
