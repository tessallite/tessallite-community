"""Per-user scratchpad measures — ephemeral calculated expressions."""
from __future__ import annotations

from uuid import UUID

import sqlglot
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from shared.db.models import ScratchpadMeasure
from shared.db.session import get_tenant_db
from src.auth.middleware import CurrentUser, forbid_embed_user, get_current_user
from src.auth.rbac import require_role
from src.api._scope import ensure_model_in_project

# The supported scratchpad data types. The UI offers exactly these; an unknown
# value would render a raw i18n key (``scratchpad.dataType.<value>``) in the
# panel, so the set is validated server-side as well (F-029-14).
SCRATCHPAD_DATA_TYPES = {"numeric", "string", "boolean", "date", "timestamp", "integer"}


def _validate_expression(expr: str) -> None:
    """Validate that expression is a parseable, single SQL expression.

    sqlglot raises ``ParseError`` on malformed SQL, but a syntactically valid
    *multi-statement* input (e.g. ``1; SELECT 2``) parses into several
    statements and is rejected by the ``len(parsed) != 1`` guard as a
    ``ValueError``. Both must surface to the user as a 400, not an unhandled
    500 (F-029-08), so both exception types are mapped here.
    """
    try:
        parsed = sqlglot.parse(f"SELECT {expr} AS _v")
    except sqlglot.errors.ParseError as e:
        raise HTTPException(status_code=400, detail=f"Invalid expression: {e}")
    if not parsed or len(parsed) != 1:
        raise HTTPException(
            status_code=400,
            detail="Expression must be a single SQL expression, not multiple statements",
        )

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/scratchpad-measures",
    tags=["scratchpad-measures"],
)


def _validate_data_type(v: str) -> str:
    if v not in SCRATCHPAD_DATA_TYPES:
        raise ValueError(
            f"data_type must be one of {sorted(SCRATCHPAD_DATA_TYPES)}"
        )
    return v


class ScratchpadCreate(BaseModel):
    name: str
    display_name: str | None = None
    expression: str
    data_type: str = "numeric"
    format: str | None = None

    @field_validator("data_type")
    @classmethod
    def _check_data_type(cls, v: str) -> str:
        return _validate_data_type(v)


class ScratchpadUpdate(BaseModel):
    name: str | None = None
    display_name: str | None = None
    expression: str | None = None
    data_type: str | None = None
    format: str | None = None

    @field_validator("data_type")
    @classmethod
    def _check_data_type(cls, v: str | None) -> str | None:
        if v is not None:
            return _validate_data_type(v)
        return v


class ScratchpadResponse(BaseModel):
    id: UUID
    model_id: UUID
    name: str
    display_name: str | None
    expression: str
    data_type: str
    format: str | None
    created_by: str
    created_at: str
    updated_at: str


def _to_response(row: ScratchpadMeasure) -> ScratchpadResponse:
    return ScratchpadResponse(
        id=row.id,
        model_id=row.model_id,
        name=row.name,
        display_name=row.display_name,
        expression=row.expression,
        data_type=row.data_type,
        format=row.format,
        created_by=row.created_by,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


@router.get("", response_model=list[ScratchpadResponse], dependencies=[require_role("viewer")])
async def list_scratchpad_measures(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[ScratchpadResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        user_id = current_user.email or current_user.user_id
        rows = (await db.execute(
            select(ScratchpadMeasure)
            .where(
                ScratchpadMeasure.model_id == model_id,
                ScratchpadMeasure.created_by == user_id,
            )
            .order_by(ScratchpadMeasure.name)
        )).scalars().all()
        return [_to_response(r) for r in rows]
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.post("", response_model=ScratchpadResponse, status_code=201, dependencies=[require_role("viewer")])
async def create_scratchpad_measure(
    project_id: UUID,
    model_id: UUID,
    body: ScratchpadCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ScratchpadResponse:
    _validate_expression(body.expression)
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        user_id = current_user.email or current_user.user_id
        # Pre-check the (model_id, created_by, name) uniqueness constraint and
        # return a clean 409 instead of letting the DB IntegrityError escape as
        # a 500 (F-029-08), mirroring parameters.py.
        existing = await db.execute(
            select(ScratchpadMeasure).where(
                ScratchpadMeasure.model_id == model_id,
                ScratchpadMeasure.created_by == user_id,
                ScratchpadMeasure.name == body.name,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=409,
                detail=f"Scratchpad measure '{body.name}' already exists on this model",
            )
        row = ScratchpadMeasure(
            model_id=model_id,
            name=body.name,
            display_name=body.display_name,
            expression=body.expression,
            data_type=body.data_type,
            format=body.format,
            created_by=user_id,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return _to_response(row)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.patch("/{measure_id}", response_model=ScratchpadResponse, dependencies=[require_role("viewer")])
async def update_scratchpad_measure(
    project_id: UUID,
    model_id: UUID,
    measure_id: UUID,
    body: ScratchpadUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ScratchpadResponse:
    # Distinguish "field absent" from "field set to null": only fields the
    # caller actually supplied are applied, so an explicit null clears an
    # optional column (display_name / format) rather than being silently
    # skipped (F-029-11).
    supplied = body.model_dump(exclude_unset=True)
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        row = await db.get(ScratchpadMeasure, measure_id)
        if not row or row.model_id != model_id:
            raise HTTPException(status_code=404, detail="Scratchpad measure not found")
        user_id = current_user.email or current_user.user_id
        if row.created_by != user_id:
            raise HTTPException(status_code=403, detail="Not your scratchpad measure")
        if "name" in supplied and supplied["name"] is not None:
            row.name = supplied["name"]
        if "display_name" in supplied:
            row.display_name = supplied["display_name"]
        if "expression" in supplied and supplied["expression"] is not None:
            _validate_expression(supplied["expression"])
            row.expression = supplied["expression"]
        if "data_type" in supplied and supplied["data_type"] is not None:
            row.data_type = supplied["data_type"]
        if "format" in supplied:
            row.format = supplied["format"]
        await db.commit()
        await db.refresh(row)
        return _to_response(row)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.delete("/{measure_id}", status_code=204, dependencies=[require_role("viewer")])
async def delete_scratchpad_measure(
    project_id: UUID,
    model_id: UUID,
    measure_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        row = await db.get(ScratchpadMeasure, measure_id)
        if not row or row.model_id != model_id:
            raise HTTPException(status_code=404, detail="Scratchpad measure not found")
        user_id = current_user.email or current_user.user_id
        if row.created_by != user_id:
            raise HTTPException(status_code=403, detail="Not your scratchpad measure")
        await db.delete(row)
        await db.commit()
        return
    raise HTTPException(status_code=500, detail="DB session exhausted")
