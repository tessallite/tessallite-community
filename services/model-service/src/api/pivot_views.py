"""Saved pivot views — named pivot configurations per user."""
from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_, select

from shared.db.models import Dimension, Measure, SavedPivotView, Model
from shared.db.session import get_tenant_db
from src.auth.middleware import CurrentUser, forbid_embed_user, get_current_user
from src.auth.rbac import require_role

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/pivot-views",
    tags=["pivot-views"],
)


class PivotViewCreate(BaseModel):
    name: str
    measure_id: str
    row_dim_ids: list[str]
    col_dim_ids: list[str]
    config: dict | None = None
    is_shared: bool = False


class PivotViewUpdate(BaseModel):
    name: str | None = None
    measure_id: str | None = None
    row_dim_ids: list[str] | None = None
    col_dim_ids: list[str] | None = None
    config: dict | None = None
    is_shared: bool | None = None


class PivotViewResponse(BaseModel):
    id: UUID
    model_id: UUID
    name: str
    measure_id: str
    row_dim_ids: list[str]
    col_dim_ids: list[str]
    config: dict | None
    created_by: str
    is_shared: bool
    # True when the requesting user owns this view. Drives the UI's
    # delete/share controls so shared views from other users are read-only
    # (F-029-22). Derived per-request, not stored.
    is_owner: bool
    created_at: str
    updated_at: str


async def _validate_pivot_refs(
    db, model_id: UUID, measure_id: str, row_dim_ids: list[str], col_dim_ids: list[str]
) -> None:
    """Bug-5316: verify that the measure and dimension IDs belong to
    this model. Rejects dangling refs at save time rather than serving a
    broken view silently."""
    # Validate measure_id
    try:
        m_uuid = UUID(measure_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"measure_id {measure_id!r} is not a valid UUID")
    m = await db.get(Measure, m_uuid)
    if m is None or m.model_id != model_id:
        raise HTTPException(status_code=400, detail="measure_id does not belong to this model")

    # Validate row and col dimension IDs
    all_dim_ids: list[UUID] = []
    for raw_id in (*row_dim_ids, *col_dim_ids):
        try:
            all_dim_ids.append(UUID(raw_id))
        except (ValueError, AttributeError):
            raise HTTPException(status_code=400, detail=f"Dimension ID {raw_id!r} is not a valid UUID")
    if all_dim_ids:
        result = await db.execute(
            select(Dimension.id).where(
                Dimension.id.in_(all_dim_ids),
                Dimension.model_id == model_id,
            )
        )
        found = set(result.scalars().all())
        missing = [str(d) for d in all_dim_ids if d not in found]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Dimension IDs not found in this model: {', '.join(missing)}",
            )


def _to_response(row: SavedPivotView, *, owner: str) -> PivotViewResponse:
    return PivotViewResponse(
        id=row.id,
        model_id=row.model_id,
        name=row.name,
        measure_id=row.measure_id,
        row_dim_ids=json.loads(row.row_dim_ids) if row.row_dim_ids else [],
        col_dim_ids=json.loads(row.col_dim_ids) if row.col_dim_ids else [],
        config=json.loads(row.config_json) if row.config_json else None,
        created_by=row.created_by,
        is_shared=row.is_shared,
        is_owner=row.created_by == owner,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


@router.get("", response_model=list[PivotViewResponse], dependencies=[require_role("viewer")])
async def list_pivot_views(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[PivotViewResponse]:
    owner = current_user.email or current_user.user_id
    async for db in get_tenant_db(current_user.tenant_id):
        model = await db.get(Model, model_id)
        if not model or model.project_id != project_id:
            raise HTTPException(status_code=404, detail="Model not found")
        # Personal views (own) plus any view shared with the tenant (F-029-22).
        rows = await db.execute(
            select(SavedPivotView)
            .where(
                SavedPivotView.model_id == model_id,
                or_(
                    SavedPivotView.created_by == owner,
                    SavedPivotView.is_shared.is_(True),
                ),
            )
            .order_by(SavedPivotView.name)
        )
        return [_to_response(r, owner=owner) for r in rows.scalars().all()]
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.post("", response_model=PivotViewResponse, status_code=201, dependencies=[require_role("viewer")])
async def create_pivot_view(
    project_id: UUID,
    model_id: UUID,
    body: PivotViewCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> PivotViewResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        model = await db.get(Model, model_id)
        if not model or model.project_id != project_id:
            raise HTTPException(status_code=404, detail="Model not found")
        # Bug-5316: validate that referenced IDs belong to this model.
        await _validate_pivot_refs(db, model_id, body.measure_id, body.row_dim_ids, body.col_dim_ids)
        owner = current_user.email or current_user.user_id
        view = SavedPivotView(
            model_id=model_id,
            name=body.name,
            measure_id=body.measure_id,
            row_dim_ids=json.dumps(body.row_dim_ids),
            col_dim_ids=json.dumps(body.col_dim_ids),
            config_json=json.dumps(body.config) if body.config else None,
            created_by=owner,
            is_shared=body.is_shared,
        )
        db.add(view)
        await db.commit()
        await db.refresh(view)
        return _to_response(view, owner=owner)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.patch("/{view_id}", response_model=PivotViewResponse, dependencies=[require_role("viewer")])
async def update_pivot_view(
    project_id: UUID,
    model_id: UUID,
    view_id: UUID,
    body: PivotViewUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> PivotViewResponse:
    owner = current_user.email or current_user.user_id
    # Only the fields the caller actually supplied are applied; an explicit
    # ``config: null`` therefore clears the stored configuration rather than
    # being silently skipped (F-029-11).
    supplied = body.model_dump(exclude_unset=True)
    async for db in get_tenant_db(current_user.tenant_id):
        model = await db.get(Model, model_id)
        if not model or model.project_id != project_id:
            raise HTTPException(status_code=404, detail="Model not found")
        view = await db.get(SavedPivotView, view_id)
        if not view or view.model_id != model_id or view.created_by != owner:
            raise HTTPException(status_code=404, detail="Pivot view not found")
        # Bug-5316: validate any updated IDs belong to this model.
        updated_measure = supplied.get("measure_id") if "measure_id" in supplied and supplied["measure_id"] is not None else None
        updated_rows = supplied.get("row_dim_ids") if "row_dim_ids" in supplied and supplied["row_dim_ids"] is not None else None
        updated_cols = supplied.get("col_dim_ids") if "col_dim_ids" in supplied and supplied["col_dim_ids"] is not None else None
        # Use the supplied value or fall back to the existing stored value.
        eff_measure = updated_measure if updated_measure is not None else view.measure_id
        eff_rows = updated_rows if updated_rows is not None else (json.loads(view.row_dim_ids) if view.row_dim_ids else [])
        eff_cols = updated_cols if updated_cols is not None else (json.loads(view.col_dim_ids) if view.col_dim_ids else [])
        if updated_measure or updated_rows is not None or updated_cols is not None:
            await _validate_pivot_refs(db, model_id, eff_measure, eff_rows, eff_cols)

        if "name" in supplied and supplied["name"] is not None:
            view.name = supplied["name"]
        if "measure_id" in supplied and supplied["measure_id"] is not None:
            view.measure_id = supplied["measure_id"]
        if "row_dim_ids" in supplied and supplied["row_dim_ids"] is not None:
            view.row_dim_ids = json.dumps(supplied["row_dim_ids"])
        if "col_dim_ids" in supplied and supplied["col_dim_ids"] is not None:
            view.col_dim_ids = json.dumps(supplied["col_dim_ids"])
        if "config" in supplied:
            view.config_json = (
                json.dumps(supplied["config"]) if supplied["config"] is not None else None
            )
        if "is_shared" in supplied and supplied["is_shared"] is not None:
            view.is_shared = supplied["is_shared"]
        await db.commit()
        await db.refresh(view)
        return _to_response(view, owner=owner)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.delete("/{view_id}", status_code=204, dependencies=[require_role("viewer")])
async def delete_pivot_view(
    project_id: UUID,
    model_id: UUID,
    view_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    owner = current_user.email or current_user.user_id
    async for db in get_tenant_db(current_user.tenant_id):
        model = await db.get(Model, model_id)
        if not model or model.project_id != project_id:
            raise HTTPException(status_code=404, detail="Model not found")
        view = await db.get(SavedPivotView, view_id)
        if not view or view.model_id != model_id or view.created_by != owner:
            raise HTTPException(status_code=404, detail="Pivot view not found")
        await db.delete(view)
        await db.commit()
        return
    raise HTTPException(status_code=500, detail="DB session exhausted")
