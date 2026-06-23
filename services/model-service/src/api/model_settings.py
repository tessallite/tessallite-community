"""Model-level configuration API.

  GET  /projects/{project_id}/models/{model_id}/settings              — registry + values
  PUT  /projects/{project_id}/models/{model_id}/settings/{key}        — write override

Model-level keys cover the per-model knobs: aggregate cron, AI scheduler
config, optimizer aggregate cap. Overrides fall through to project,
tenant, system, and finally the registry default.

Access matrix (per C-9):
  - system_admin:  any model
  - tenant_admin:  any model in their tenant
  - modeler:       models they have a binding for (project- or model-level)
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.webhooks.dispatcher import emit_webhook
from shared.config.registry import get_def, surfaced_for_level
from shared.config.resolver import _read_model, get_setting, set_setting
from shared.db.models import Model, UserAccessBinding
from shared.db.session import get_system_db, get_tenant_db
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/settings",
    tags=["model-settings"],
)


class ModelSettingItem(BaseModel):
    key: str
    section: str
    type: str
    description: str
    own_value: Any
    effective_value: Any
    label: Optional[str] = None
    ui_help: Optional[str] = None
    ui_group: Optional[str] = None
    ui_control: Optional[str] = None
    ui_choices: Optional[list] = None
    unit: Optional[str] = None


class ModelSettingsListResponse(BaseModel):
    model_id: str
    items: list[ModelSettingItem]


class ModelSettingWrite(BaseModel):
    value: Any  # null = clear override (inherit from project/tenant/system)


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

async def _ensure_model_access(
    project_id: UUID, model_id: UUID, current_user: CurrentUser, tenant_db: AsyncSession
) -> Model:
    """Verify the model exists in the project and the caller can manage it."""
    model = await tenant_db.get(Model, model_id)
    if model is None or model.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model {model_id} not found in project {project_id}",
        )

    if current_user.role in ("system_admin", "tenant_admin"):
        return model

    user_identity = current_user.email or current_user.user_id
    # Bootstrap-admin rule: if the project has no bindings at all, the
    # first authenticated user is treated as implicit admin.
    any_binding = (
        await tenant_db.execute(
            select(UserAccessBinding).where(
                UserAccessBinding.project_id == project_id
            ).limit(1)
        )
    ).scalar_one_or_none()
    if any_binding is None:
        return model

    rows = await tenant_db.execute(
        select(UserAccessBinding).where(
            UserAccessBinding.user_identity == user_identity,
            (UserAccessBinding.project_id == project_id)
            | (UserAccessBinding.model_id == model_id),
        )
    )
    binding = rows.scalars().first()
    if binding is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No access binding for this model",
        )
    return model


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("", response_model=ModelSettingsListResponse)
async def list_model_settings(
    project_id: UUID,
    model_id: UUID,
    sys_db: AsyncSession = Depends(get_system_db),
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ModelSettingsListResponse:
    items: list[ModelSettingItem] = []
    async for tenant_db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_access(project_id, model_id, current_user, tenant_db)
        for d in surfaced_for_level("model"):
            present, raw = await _read_model(tenant_db, model_id, d.key)
            own_value = raw if present else None
            try:
                effective = await get_setting(
                    d.key,
                    system_session=sys_db,
                    tenant_session=tenant_db,
                    project_id=project_id,
                    model_id=model_id,
                )
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("model settings list: %s: %s", d.key, exc)
                effective = None
            items.append(
                ModelSettingItem(
                    key=d.key,
                    section=d.section,
                    type=d.type,
                    description=d.description,
                    own_value=own_value,
                    effective_value=effective,
                    label=d.label,
                    ui_help=d.ui_help,
                    ui_group=d.ui_group,
                    ui_control=d.ui_control,
                    ui_choices=d.ui_choices,
                    unit=d.unit,
                )
            )
    items.sort(key=lambda it: (it.ui_group or it.section, it.label or it.key))
    return ModelSettingsListResponse(model_id=str(model_id), items=items)


@router.put("/{key}", dependencies=[require_role("modeler")])
async def write_model_setting(
    project_id: UUID,
    model_id: UUID,
    key: str,
    body: ModelSettingWrite = Body(...),
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> dict:
    try:
        get_def(key, "model")
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown model setting: {key!r}",
        )
    async for tenant_db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_access(project_id, model_id, current_user, tenant_db)
        try:
            await set_setting(
                key, body.value,
                actor=current_user.email or current_user.user_id,
                tenant_session=tenant_db,
                model_id=model_id,
            )
            await emit_webhook(current_user.tenant_id, "settings.changed", {
                "scope": "model",
                "model_id": str(model_id),
                "project_id": str(project_id),
                "key": key,
                "actor": current_user.email,
            })
        except (ValueError, KeyError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            )
    return {"status": "ok", "model_id": str(model_id), "key": key}
