"""Saved pivot views — named pivot configurations per user."""
from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_, select

from shared.db.models import Dimension, Measure, SavedPivotView, Model
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    PivotConfigError,
    PivotConfigErrorCode,
    validate_pivot_config,
    validate_pivot_config_structure,
)
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import caller_has_role, require_role

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
    # share control so only owners publish/unpublish (F-029-22).
    # Derived per-request, not stored.
    is_owner: bool
    # True when the caller may edit/delete this view: the owner always may,
    # and a modeler+ may edit/delete a SHARED view they do not own (Bug-5839).
    # Personal views of other users are never returned, so can_edit here only
    # ever elevates for shared views. Derived per-request, not stored.
    can_edit: bool
    created_at: str
    updated_at: str


def _raise_pivot_config_error(err: PivotConfigError) -> None:
    """Surface a typed pivot-config validation failure as HTTP 422.

    Bug-8161/8182/7442: the 422 body's ``detail`` carries the machine
    ``error_code`` (one of ``PivotConfigErrorCode``) PLUS the human ``message``.
    The client keys its ERROR_CODE_MAP off ``detail.error_code`` and never parses
    the prose ``message`` — that is the producer/consumer contract this lane
    establishes. A config-VALIDATION failure is a 422 (unprocessable request
    body), distinct from the 404 raised when the model or the view row is
    genuinely not found.
    """
    raise HTTPException(
        status_code=422,
        detail={"error_code": err.error_code.value, "message": err.message},
    )


async def _validate_pivot_refs(
    db, model_id: UUID, measure_id: str, row_dim_ids: list[str], col_dim_ids: list[str]
) -> None:
    """Bug-5316: verify that the measure and dimension IDs belong to this model,
    rejecting dangling refs at save time rather than serving a broken view.

    FORMAT validation (the ``measure_id`` empty-vs-UUID contract of Bug-7442 and
    the dimension-id UUID shape) is performed FIRST by ``validate_pivot_config``
    at the route, so every id reaching this DB pass is already well-formed. This
    function therefore only confirms EXISTENCE, raising the typed 422 vocabulary
    (``UNKNOWN_MEASURE`` / ``UNKNOWN_DIMENSION``) on a dangling reference.

    Bug-6412: the pivot panel legitimately supports a synthetic first measure —
    the built-in Record Count, a per-user scratchpad measure, or no measure yet.
    The frontend sends an empty ``measure_id`` in those cases and the
    authoritative selection travels in ``config.measureSelections``. An empty
    pointer means "no primary model measure" and skips the measure existence
    check; the dangling-ref guard still applies to every dimension id.
    """
    # Existence check for the primary model-measure pointer (skipped when empty).
    if measure_id:
        m = await db.get(Measure, UUID(measure_id))
        if m is None or m.model_id != model_id:
            _raise_pivot_config_error(
                PivotConfigError(
                    error_code=PivotConfigErrorCode.UNKNOWN_MEASURE,
                    message="measure_id does not belong to this model",
                )
            )

    # Existence check for every row/col dimension id.
    all_dim_ids: list[UUID] = [UUID(raw_id) for raw_id in (*row_dim_ids, *col_dim_ids)]
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
            _raise_pivot_config_error(
                PivotConfigError(
                    error_code=PivotConfigErrorCode.UNKNOWN_DIMENSION,
                    message=f"Dimension IDs not found in this model: {', '.join(missing)}",
                )
            )


def _to_response(row: SavedPivotView, *, owner: str, can_edit: bool) -> PivotViewResponse:
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
        can_edit=can_edit,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


async def _authorize_view_mutation(
    db,
    current_user: CurrentUser,
    project_id: UUID,
    model_id: UUID,
    view: SavedPivotView,
) -> None:
    """Authorise an edit/delete on a pivot view (Bug-5839).

    Ownership model: a view's owner may always modify it. A modeler+ may also
    modify a SHARED view they do not own — shared views are a model-wide asset,
    so a modeler curating the model can correct or retire them. A personal
    (unshared) view of another user stays private: it is not even listed for
    other users, so we answer 404 to avoid leaking its existence. A shared view
    that the caller can see but lacks the modeler role for yields 403.
    """
    owner = current_user.email or current_user.user_id
    if view.created_by == owner:
        return
    if view.is_shared:
        if await caller_has_role(db, current_user, project_id, "modeler", model_id=model_id):
            return
        raise HTTPException(
            status_code=403,
            detail="Only the view owner or a modeler can modify a shared pivot view",
        )
    raise HTTPException(status_code=404, detail="Pivot view not found")


@router.get("", response_model=list[PivotViewResponse], dependencies=[require_role("viewer")])
async def list_pivot_views(
    project_id: UUID,
    model_id: UUID,
    # Bug-6423: fail closed to match the stricter sibling read endpoints
    # (saved_queries list/get both use forbid_embed_user). Embed tokens are
    # scoped BI-client grants and must not enumerate a user's saved pivot
    # views — reads across the two libraries now behave consistently.
    current_user: CurrentUser = Depends(forbid_embed_user),
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
        # Bug-5839: the modeler-role grant is model-wide, so resolve it once
        # per request rather than per row. A modeler may edit/delete shared
        # views they do not own.
        is_modeler_plus = await caller_has_role(
            db, current_user, project_id, "modeler", model_id=model_id
        )
        result: list[PivotViewResponse] = []
        for r in rows.scalars().all():
            is_owner = r.created_by == owner
            can_edit = is_owner or (r.is_shared and is_modeler_plus)
            result.append(_to_response(r, owner=owner, can_edit=can_edit))
        return result
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
        # Bug-8161/8182/7442: typed structural + format validation of the config
        # blob and reference-id shapes (no DB). A failure is a typed 422 the
        # client can map, not opaque prose.
        cfg_err = validate_pivot_config(
            body.config, body.measure_id, body.row_dim_ids, body.col_dim_ids
        )
        if cfg_err is not None:
            _raise_pivot_config_error(cfg_err)
        # Bug-5316: confirm the (now well-formed) referenced IDs exist in this model.
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
        # The creator always owns and can edit the view they just saved.
        return _to_response(view, owner=owner, can_edit=True)
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
        if not view or view.model_id != model_id:
            raise HTTPException(status_code=404, detail="Pivot view not found")
        # Bug-5839: owner, or a modeler+ on a shared view they do not own.
        await _authorize_view_mutation(db, current_user, project_id, model_id, view)
        # Bug-5839 (share boundary): publishing/unpublishing stays OWNER-ONLY.
        # A modeler admitted above to edit a shared view must not flip it to
        # personal (which would hide the owner's view from everyone else) or
        # publish it. Only the owner may change is_shared.
        is_owner = view.created_by == owner
        if (
            "is_shared" in supplied
            and supplied["is_shared"] is not None
            and supplied["is_shared"] != view.is_shared
            and not is_owner
        ):
            raise HTTPException(
                status_code=403,
                detail="Only the view owner can change sharing",
            )
        # Bug-5316: validate any updated IDs belong to this model.
        updated_measure = supplied.get("measure_id") if "measure_id" in supplied and supplied["measure_id"] is not None else None
        updated_rows = supplied.get("row_dim_ids") if "row_dim_ids" in supplied and supplied["row_dim_ids"] is not None else None
        updated_cols = supplied.get("col_dim_ids") if "col_dim_ids" in supplied and supplied["col_dim_ids"] is not None else None
        # Use the supplied value or fall back to the existing stored value.
        eff_measure = updated_measure if updated_measure is not None else view.measure_id
        eff_rows = updated_rows if updated_rows is not None else (json.loads(view.row_dim_ids) if view.row_dim_ids else [])
        eff_cols = updated_cols if updated_cols is not None else (json.loads(view.col_dim_ids) if view.col_dim_ids else [])
        eff_config = supplied["config"] if "config" in supplied else (json.loads(view.config_json) if view.config_json else None)
        # Bug-6412: validate whenever the measure pointer is supplied (even an
        # explicit "" that clears it), dims change, or a new config is supplied —
        # an empty measure_id skips only the measure existence check, never the
        # dimension checks.
        refs_touched = "measure_id" in supplied or updated_rows is not None or updated_cols is not None
        # Bug-8161 (review B2): publishing a view tenant-wide must revalidate the
        # EFFECTIVE (here: stored) config first. A personal view whose config was
        # persisted BEFORE this typed contract could otherwise be shared and crash
        # every other user's loader. A false->true is_shared transition therefore
        # forces structural validation even when no config/refs are supplied;
        # raising here (before the is_shared write below) leaves the flag unflipped.
        is_publishing = (
            "is_shared" in supplied
            and supplied["is_shared"] is True
            and view.is_shared is False
        )
        if refs_touched or "config" in supplied:
            # Bug-8161/8182/7442: typed structural + format validation first (no DB).
            cfg_err = validate_pivot_config(eff_config, eff_measure, eff_rows, eff_cols)
            if cfg_err is not None:
                _raise_pivot_config_error(cfg_err)
        elif is_publishing:
            # Pure publish (no config/refs supplied): revalidate only the config
            # STRUCTURE — the loader-crash vector a shared view must not carry.
            # A legacy measure_id/dimension pointer is a referential matter, not a
            # share-safety one, so it is NOT re-rejected here (review B2).
            cfg_err = validate_pivot_config_structure(eff_config)
            if cfg_err is not None:
                _raise_pivot_config_error(cfg_err)
        if refs_touched:
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
        # The mutation above already passed the owner-or-modeler gate, so the
        # caller can edit the view they just successfully updated.
        return _to_response(view, owner=owner, can_edit=True)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.delete("/{view_id}", status_code=204, dependencies=[require_role("viewer")])
async def delete_pivot_view(
    project_id: UUID,
    model_id: UUID,
    view_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        model = await db.get(Model, model_id)
        if not model or model.project_id != project_id:
            raise HTTPException(status_code=404, detail="Model not found")
        view = await db.get(SavedPivotView, view_id)
        if not view or view.model_id != model_id:
            raise HTTPException(status_code=404, detail="Pivot view not found")
        # Bug-5839: owner, or a modeler+ on a shared view they do not own.
        await _authorize_view_mutation(db, current_user, project_id, model_id, view)
        await db.delete(view)
        await db.commit()
        return
    raise HTTPException(status_code=500, detail="DB session exhausted")
