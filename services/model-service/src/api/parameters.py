"""Model parameter CRUD routes.

Named parameters on models resolved at query time.
Sources: model default, JDBC session variable, persona default filter.
Types: string, number, multi_value, date_range, boolean.

Role requirements:
  GET     -> viewer+
  POST    -> modeler+
  PUT     -> modeler+
  DELETE  -> modeler+
"""
from __future__ import annotations

import re
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import select

from shared.db.models import ModelParameter
from shared.db.session import get_tenant_db
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role
from src.api._scope import ensure_model_in_project

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/parameters",
    tags=["parameters"],
)

VALID_TYPES = {"string", "number", "multi_value", "date_range", "boolean"}

# A declared parameter name must be ``@`` + a leading letter/underscore +
# word characters so the query-router lexer recognises ``@name`` as a single
# placeholder span and binds it unambiguously (Bug-1105 / F-029-05).
_PARAM_NAME_RE = re.compile(r"^@[A-Za-z_]\w*$")


class ParameterCreate(BaseModel):
    name: str
    display_name: str | None = None
    param_type: str
    default_value: dict | str | int | float | bool | list | None = None
    allowed_values: list | None = None
    description: str | None = None

    @field_validator("param_type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in VALID_TYPES:
            raise ValueError(f"param_type must be one of {sorted(VALID_TYPES)}")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        # ``@`` then a leading letter/underscore then word characters, so the
        # query-router's sqlglot lexer always recognises ``@name`` as a single
        # placeholder span and binds it unambiguously (Bug-1105 / F-029-05).
        if not _PARAM_NAME_RE.match(v):
            raise ValueError(
                "Parameter name must be '@' followed by a letter or underscore "
                "and then letters, digits, or underscores (e.g. '@region')"
            )
        return v

    @model_validator(mode="after")
    def validate_allowed_values(self) -> "ParameterCreate":
        _check_allowed_values(self.param_type, self.allowed_values)
        return self


def _check_allowed_values(param_type: str | None, allowed_values: list | None) -> None:
    """Reject an ``allowed_values`` whitelist that cannot govern this type.

    Complements resolver-side enforcement (F-029-02): a modeler who declares an
    allowed list gets a clean 422 at definition time rather than a list that
    can never match. ``date_range`` resolves to a {from,to} object, so a
    discrete whitelist is meaningless; for numeric parameters every entry must
    be coercible to a number, otherwise it could never match a coerced value.
    """
    if not allowed_values:
        return
    if param_type == "date_range":
        raise ValueError("allowed_values is not supported for date_range parameters")
    if param_type == "number":
        for entry in allowed_values:
            if isinstance(entry, bool) or not isinstance(entry, (int, float, str)):
                raise ValueError(f"allowed_values entry {entry!r} is not numeric")
            if isinstance(entry, str):
                try:
                    float(entry)
                except ValueError:
                    raise ValueError(f"allowed_values entry {entry!r} is not numeric")


def _coerce_default_value(
    param_type: str,
    default_value: dict | str | int | float | bool | list | None,
) -> dict | str | int | float | bool | list | None:
    """Validate and coerce *default_value* against the declared *param_type*.

    Bug-5314: previously the create and update endpoints persisted the raw
    default_value without checking type compatibility, so a ``number``
    parameter could hold ``"hello"`` as its default and fail at query time.
    """
    if default_value is None:
        return None
    if param_type == "number":
        if isinstance(default_value, bool):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="default_value must be numeric for a 'number' parameter",
            )
        if isinstance(default_value, (int, float)):
            return default_value
        if isinstance(default_value, str):
            try:
                return float(default_value) if "." in default_value else int(default_value)
            except (TypeError, ValueError):
                pass
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="default_value must be numeric for a 'number' parameter",
        )
    if param_type == "boolean":
        if isinstance(default_value, bool):
            return default_value
        if isinstance(default_value, str):
            if default_value.lower() in ("true", "1", "yes"):
                return True
            if default_value.lower() in ("false", "0", "no"):
                return False
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="default_value must be a boolean for a 'boolean' parameter",
        )
    if param_type == "string":
        if not isinstance(default_value, str):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="default_value must be a string for a 'string' parameter",
            )
        return default_value
    if param_type == "multi_value":
        if isinstance(default_value, list):
            return default_value
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="default_value must be a list for a 'multi_value' parameter",
        )
    if param_type == "date_range":
        if isinstance(default_value, dict):
            return default_value
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="default_value must be a {from, to} object for a 'date_range' parameter",
        )
    return default_value


class ParameterUpdate(BaseModel):
    display_name: str | None = None
    param_type: str | None = None
    default_value: dict | str | int | float | bool | list | None = None
    allowed_values: list | None = None
    description: str | None = None

    @field_validator("param_type")
    @classmethod
    def validate_type(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_TYPES:
            raise ValueError(f"param_type must be one of {sorted(VALID_TYPES)}")
        return v

    @model_validator(mode="after")
    def validate_allowed_values(self) -> "ParameterUpdate":
        # Only validate against an explicitly supplied param_type on update;
        # when type is absent the resolver still enforces membership at query
        # time against the stored type.
        if self.param_type is not None:
            _check_allowed_values(self.param_type, self.allowed_values)
        return self


class ParameterResponse(BaseModel):
    id: str
    model_id: str
    name: str
    display_name: str | None = None
    param_type: str
    default_value: dict | str | int | float | bool | list | None = None
    allowed_values: list | None = None
    description: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    model_config = {"from_attributes": True}


def _to_response(p: ModelParameter) -> ParameterResponse:
    return ParameterResponse(
        id=str(p.id),
        model_id=str(p.model_id),
        name=p.name,
        display_name=p.display_name,
        param_type=p.param_type,
        default_value=p.default_value,
        allowed_values=p.allowed_values,
        description=p.description,
        created_at=p.created_at.isoformat() if p.created_at else None,
        updated_at=p.updated_at.isoformat() if p.updated_at else None,
    )


@router.get("", response_model=list[ParameterResponse])
async def list_parameters(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[ParameterResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        result = await db.execute(
            select(ModelParameter)
            .where(ModelParameter.model_id == model_id)
            .order_by(ModelParameter.name)
        )
        return [_to_response(p) for p in result.scalars().all()]


@router.post(
    "",
    response_model=ParameterResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_parameter(
    project_id: UUID,
    model_id: UUID,
    body: ParameterCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ParameterResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        existing = await db.execute(
            select(ModelParameter).where(
                ModelParameter.model_id == model_id,
                ModelParameter.name == body.name,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Parameter '{body.name}' already exists on this model",
            )
        coerced_default = _coerce_default_value(body.param_type, body.default_value)
        param = ModelParameter(
            model_id=model_id,
            name=body.name,
            display_name=body.display_name,
            param_type=body.param_type,
            default_value=coerced_default,
            allowed_values=body.allowed_values,
            description=body.description,
        )
        db.add(param)
        await db.commit()
        await db.refresh(param)
        return _to_response(param)


@router.put(
    "/{param_id}",
    response_model=ParameterResponse,
    dependencies=[require_role("modeler")],
)
async def update_parameter(
    project_id: UUID,
    model_id: UUID,
    param_id: UUID,
    body: ParameterUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ParameterResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        param = await db.get(ModelParameter, param_id)
        if param is None or param.model_id != model_id:
            raise HTTPException(status_code=404, detail="Parameter not found")
        updates = body.model_dump(exclude_unset=True)
        # Bug-5314: coerce default_value against the effective param_type
        if "default_value" in updates:
            effective_type = updates.get("param_type", param.param_type)
            updates["default_value"] = _coerce_default_value(
                effective_type, updates["default_value"],
            )
        for key, value in updates.items():
            setattr(param, key, value)
        await db.commit()
        await db.refresh(param)
        return _to_response(param)


@router.delete(
    "/{param_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def delete_parameter(
    project_id: UUID,
    model_id: UUID,
    param_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        param = await db.get(ModelParameter, param_id)
        if param is None or param.model_id != model_id:
            raise HTTPException(status_code=404, detail="Parameter not found")
        await db.delete(param)
        await db.commit()
