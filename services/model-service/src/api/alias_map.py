"""Per-model phrase -> canonical attribute alias map.

A single jsonb row per model. Used by the conversational agent to disambiguate
business terminology (e.g., "revenue" -> "fact_orders.gross_amount") when
parsing user questions.

Endpoints:
  GET    /projects/{p}/models/{m}/alias-map           — fetch (empty if unset)
  PUT    /projects/{p}/models/{m}/alias-map           — replace whole map
  POST   /projects/{p}/models/{m}/alias-map/import    — bulk JSON import
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from shared.db.models import ModelAliasMap
from shared.db.session import get_tenant_db
from src.api._scope import ensure_model_in_project
from src.api._model_lock import acquire_model_definition_lock
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/alias-map",
    tags=["alias-map"],
)


class AliasMapResponse(BaseModel):
    model_id: UUID
    alias_map: dict[str, str] = Field(default_factory=dict)


class AliasMapReplace(BaseModel):
    alias_map: dict[str, str] = Field(default_factory=dict)


class AliasMapImport(BaseModel):
    """Bulk import payload.

    `mode='replace'` overwrites existing entries; `mode='merge'` keeps existing
    keys not present in the payload. Default is merge.
    """
    alias_map: dict[str, str] = Field(default_factory=dict)
    mode: str = Field(default="merge", pattern="^(replace|merge)$")


def _validate_pairs(pairs: dict[str, Any]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for k, v in pairs.items():
        if not isinstance(k, str) or not k.strip():
            raise HTTPException(
                status_code=400,
                detail="Alias keys must be non-empty strings.",
            )
        if not isinstance(v, str) or not v.strip():
            raise HTTPException(
                status_code=400,
                detail=f"Alias value for '{k}' must be a non-empty string.",
            )
        cleaned[k.strip()] = v.strip()
    return cleaned


@router.get("", response_model=AliasMapResponse, dependencies=[require_role("viewer")])
async def get_alias_map(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> AliasMapResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        record = await db.get(ModelAliasMap, model_id)
        if record is None:
            return AliasMapResponse(model_id=model_id, alias_map={})
        return AliasMapResponse(
            model_id=model_id, alias_map=record.alias_map or {}
        )
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.put(
    "", response_model=AliasMapResponse, dependencies=[require_role("modeler")]
)
async def replace_alias_map(
    project_id: UUID,
    model_id: UUID,
    body: AliasMapReplace,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> AliasMapResponse:
    cleaned = _validate_pairs(body.alias_map)
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        # Bug-7982 finding 7 then 3: auth before lock; ModelAliasMap is
        # snapshot-owned (truncate-reinserted on revert).
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        record = await db.get(ModelAliasMap, model_id)
        if record is None:
            record = ModelAliasMap(model_id=model_id, alias_map=cleaned)
            db.add(record)
        else:
            record.alias_map = cleaned
        await db.commit()
        return AliasMapResponse(model_id=model_id, alias_map=cleaned)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.post(
    "/import",
    response_model=AliasMapResponse,
    dependencies=[require_role("modeler")],
)
async def import_alias_map(
    project_id: UUID,
    model_id: UUID,
    body: AliasMapImport,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> AliasMapResponse:
    """Bulk import. `mode=merge` keeps existing keys; `mode=replace` overwrites."""
    incoming = _validate_pairs(body.alias_map)
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        # Bug-7982 finding 7 then 3: auth before lock; ModelAliasMap is
        # snapshot-owned (truncate-reinserted on revert).
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        record = await db.get(ModelAliasMap, model_id)
        if record is None:
            record = ModelAliasMap(model_id=model_id, alias_map=incoming)
            db.add(record)
            final = incoming
        else:
            if body.mode == "replace":
                final = incoming
            else:
                final = {**(record.alias_map or {}), **incoming}
            record.alias_map = final
        await db.commit()
        return AliasMapResponse(model_id=model_id, alias_map=final)
    raise HTTPException(status_code=500, detail="DB session exhausted")
