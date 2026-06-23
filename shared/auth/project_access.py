"""Shared project/model RBAC checks for non-model-service boundaries."""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select

from shared.auth.middleware import CurrentEmbedUser, CurrentUser
from shared.auth.roles import project_role_level
from shared.db.models import Model, UserAccessBinding


def _as_uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _has_admin_bypass(current_user: CurrentUser) -> bool:
    return current_user.role in ("tenant_admin", "system_admin")


async def ensure_project_model_access(
    db,
    current_user: CurrentUser,
    *,
    project_id: UUID | str,
    model_id: UUID | str | None = None,
    min_role: str = "viewer",
) -> None:
    """Enforce model-service-compatible project/model access bindings.

    ``require_role`` lives inside model-service, but optimizer and
    query-router also expose model data. This helper keeps those service
    boundaries on the same persisted ``UserAccessBinding`` contract without
    importing model-service-local modules.
    """
    project_uuid = _as_uuid(project_id)
    model_uuid = _as_uuid(model_id) if model_id is not None else None

    if _has_admin_bypass(current_user):
        return

    if isinstance(current_user, CurrentEmbedUser):
        if project_role_level(min_role) < project_role_level("viewer"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Embed tokens cannot modify project resources",
            )
        if current_user.project_ids is not None:
            allowed_projects = {str(pid).lower() for pid in current_user.project_ids}
            if str(project_uuid).lower() not in allowed_projects:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Project not in embed token scope",
                )
        if model_uuid is not None and current_user.model_ids is not None:
            allowed = {str(mid).lower() for mid in current_user.model_ids}
            if str(model_uuid).lower() not in allowed:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Model not in embed token scope",
                )
        return

    effective_role: Optional[str] = None

    if model_uuid is not None:
        model_binding = (
            await db.execute(
                select(UserAccessBinding).where(
                    UserAccessBinding.user_identity == current_user.user_id,
                    UserAccessBinding.project_id == project_uuid,
                    UserAccessBinding.model_id == model_uuid,
                )
            )
        ).scalar_one_or_none()
        if model_binding is not None:
            effective_role = model_binding.role

    if effective_role is None:
        project_binding = (
            await db.execute(
                select(UserAccessBinding).where(
                    UserAccessBinding.user_identity == current_user.user_id,
                    UserAccessBinding.project_id == project_uuid,
                    UserAccessBinding.model_id.is_(None),
                )
            )
        ).scalar_one_or_none()
        if project_binding is not None:
            effective_role = project_binding.role

    if effective_role is None:
        any_binding = (
            await db.execute(
                select(UserAccessBinding.id)
                .where(UserAccessBinding.project_id == project_uuid)
                .limit(1)
            )
        ).first()
        if any_binding is None:
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: no binding for this project",
        )

    if project_role_level(effective_role) > project_role_level(min_role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Access denied: requires '{min_role}', "
                f"caller has '{effective_role}'"
            ),
        )


async def load_authorized_model(
    db,
    current_user: CurrentUser,
    *,
    model_id: UUID | str,
    project_id: UUID | str | None = None,
    min_role: str = "viewer",
) -> Model:
    model_uuid = _as_uuid(model_id)
    model = await db.get(Model, model_uuid)
    if model is None or (
        project_id is not None and model.project_id != _as_uuid(project_id)
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Model not found",
        )
    await ensure_project_model_access(
        db,
        current_user,
        project_id=model.project_id,
        model_id=model.id,
        min_role=min_role,
    )
    return model
