"""Project-level configuration API.

  GET  /projects/{project_id}/settings              — registry + current values
  PUT  /projects/{project_id}/settings/{key}        — write a single override

Project-level settings are pure overrides of tenant keys. Their default
in the registry is ``None``, meaning "fall through to the tenant level".
The GET endpoint reports BOTH the project-level value (which may be
None / unset) AND the resolved effective value (after fallback) so the
UI can show inherited placeholders.

Access matrix (per C-9):
  - system_admin: any project in any tenant
  - tenant_admin: any project in their tenant
  - modeler:      projects they have a binding for
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from shared.audit.logger import audit
from shared.webhooks.dispatcher import emit_webhook_logged as emit_webhook
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.identity import user_identity_matches
from shared.config.registry import get_def, surfaced_for_level
from shared.config.resolver import _read_project, get_setting, set_setting
from shared.db.models import Project, UserAccessBinding
from shared.db.session import get_system_db, get_tenant_db
from src.auth.middleware import (
    CurrentUser,
    forbid_embed_user,
    is_human_tenant_admin_or_system_admin,
)
from src.auth.rbac import require_role

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/projects/{project_id}/settings", tags=["project-settings"])


class ProjectSettingItem(BaseModel):
    key: str
    section: str
    type: str
    description: str
    own_value: Any        # the project-level row, None if not set
    effective_value: Any  # what get_setting returns after fallback
    label: Optional[str] = None
    ui_help: Optional[str] = None
    ui_group: Optional[str] = None
    ui_control: Optional[str] = None
    ui_choices: Optional[list] = None
    unit: Optional[str] = None


class ProjectSettingsListResponse(BaseModel):
    project_id: str
    items: list[ProjectSettingItem]


class ProjectSettingWrite(BaseModel):
    value: Any  # pass null to clear (i.e. fall back to tenant)


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

async def _ensure_project_access(
    project_id: UUID, current_user: CurrentUser, tenant_db: AsyncSession
) -> Project:
    """Verify the project exists and the caller can manage its settings."""
    project = await tenant_db.get(Project, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project {project_id} not found",
        )

    if is_human_tenant_admin_or_system_admin(current_user):
        return project

    user_identity = current_user.email or current_user.user_id
    # Binding-only (F-021-04 hard cutover, decision #9): no zero-binding
    # bootstrap-admin grant — a project with no binding for this caller denies.
    rows = await tenant_db.execute(
        select(UserAccessBinding).where(
            UserAccessBinding.project_id == project_id,
            user_identity_matches(UserAccessBinding.user_identity, user_identity),
        )
    )
    binding = rows.scalar_one_or_none()
    if binding is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No access binding for this project",
        )
    return project


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get(
    "",
    response_model=ProjectSettingsListResponse,
    dependencies=[require_role("viewer")],
)
async def list_project_settings(
    project_id: UUID,
    sys_db: AsyncSession = Depends(get_system_db),
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ProjectSettingsListResponse:
    items: list[ProjectSettingItem] = []
    async for tenant_db in get_tenant_db(current_user.tenant_id):
        await _ensure_project_access(project_id, current_user, tenant_db)
        for d in surfaced_for_level("project"):
            present, raw = await _read_project(tenant_db, project_id, d.key)
            own_value = raw if present else None
            try:
                effective = await get_setting(
                    d.key,
                    system_session=sys_db,
                    tenant_session=tenant_db,
                    project_id=project_id,
                )
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("project settings list: %s: %s", d.key, exc)
                effective = None
            items.append(
                ProjectSettingItem(
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
    return ProjectSettingsListResponse(
        project_id=str(project_id), items=items
    )


@router.put("/{key}", dependencies=[require_role("modeler")])
async def write_project_setting(
    project_id: UUID,
    key: str,
    body: ProjectSettingWrite = Body(...),
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> dict:
    try:
        get_def(key, "project")
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown project setting: {key!r}",
        )
    async for tenant_db in get_tenant_db(current_user.tenant_id):
        await _ensure_project_access(project_id, current_user, tenant_db)
        try:
            await set_setting(
                key, body.value,
                actor=current_user.email or current_user.user_id,
                tenant_session=tenant_db,
                project_id=project_id,
            )
            await audit(
                tenant_db, action="settings.update", severity="warn",
                actor_email=current_user.email,
                target_type="project_setting",
                target_name=key,
                detail={"scope": "project", "project_id": str(project_id)},
            )
            await tenant_db.commit()
            await emit_webhook(current_user.tenant_id, "settings.changed", {
                "scope": "project",
                "project_id": str(project_id),
                "key": key,
                "actor": current_user.email,
            })
        except (ValueError, KeyError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            )
    return {"status": "ok", "project_id": str(project_id), "key": key}
