"""Shared project/model RBAC checks for non-model-service boundaries."""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select

from shared.auth.identity import user_identity_matches
from shared.auth.middleware import (
    CurrentEmbedUser,
    CurrentServiceUser,
    CurrentUser,
    is_human_tenant_admin_or_system_admin,
)
from shared.auth.roles import project_role_level
from shared.db.models import Model, UserAccessBinding


def _as_uuid(value: UUID | str) -> UUID:
    """Coerce ``value`` to a UUID, raising HTTP 400 on malformed input.

    Bug-6601: bare ``UUID(str(value))`` raised an uncaught ``ValueError``
    on non-UUID strings, surfacing as an unguarded 500. The query-router
    JSON surfaces are now individually guarded (Bug-6381), but any other
    caller reaching this shared helper with a bad id would still 500.
    """
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid UUID: {value!r}",
        )


def _has_admin_bypass(current_user: CurrentUser) -> bool:
    return is_human_tenant_admin_or_system_admin(current_user)


def _is_service_user(current_user: CurrentUser) -> bool:
    """Return True when *current_user* is a service principal.

    Bug-8613: A service principal must never enter human binding lookup or
    bootstrap-admin logic (AUTH-RR-01).  The previous behaviour was to
    unconditionally ADMIT every service principal on the assumption that
    ``require_capability_or_service_scope`` already validated its scope at
    the route level.  That assumption holds ONLY for routes that actually
    use ``require_capability_or_service_scope`` (or an equivalent typed-
    scope dependency).  Routes gated by bare ``require_capability(...)``
    never perform a scope check on service tokens — ``require_capability``
    narrows only ``CurrentEmbedUser`` — so the pair was wide open to any
    service token regardless of its scopes.

    The callers of this helper now decide what to do with the result by
    passing ``service_scope_verified``.
    """
    return isinstance(current_user, CurrentServiceUser)


async def ensure_project_model_access(
    db,
    current_user: CurrentUser,
    *,
    project_id: UUID | str,
    model_id: UUID | str | None = None,
    min_role: str = "viewer",
    service_scope_verified: bool = False,
) -> None:
    """Enforce model-service-compatible project/model access bindings.

    ``require_role`` lives inside model-service, but optimizer and
    query-router also expose model data. This helper keeps those service
    boundaries on the same persisted ``UserAccessBinding`` contract without
    importing model-service-local modules.

    Bug-8613: ``service_scope_verified`` defaults to ``False``. When False
    and the caller is a ``CurrentServiceUser``, the request is refused
    (HTTP 403). Callers on routes that have already verified the service
    token's scope via ``require_capability_or_service_scope`` (or an
    equivalent typed-scope dependency) must pass ``True`` to re-enable the
    previous pass-through behaviour. This closes the composition defect
    where ``require_capability(...)`` (which narrows only embed users) was
    paired with the old unconditional service-user bypass — any service
    token, regardless of its scopes, could access project resources.
    """
    project_uuid = _as_uuid(project_id)
    model_uuid = _as_uuid(model_id) if model_id is not None else None

    if _has_admin_bypass(current_user):
        return

    if _is_service_user(current_user):
        if service_scope_verified:
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Service token scope not verified for project access",
        )

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
                    user_identity_matches(UserAccessBinding.user_identity, current_user.user_id),
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
                    user_identity_matches(UserAccessBinding.user_identity, current_user.user_id),
                    UserAccessBinding.project_id == project_uuid,
                    UserAccessBinding.model_id.is_(None),
                )
            )
        ).scalar_one_or_none()
        if project_binding is not None:
            effective_role = project_binding.role

    if effective_role is None:
        # No binding => deny. There is NO zero-binding bootstrap-admin grant
        # (F-021-04 hard cutover, Wave C decision #9, 2026-08-19). A project
        # with no binding for this caller denies, whether or not the project
        # has any bindings at all.
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
    service_scope_verified: bool = False,
) -> Model:
    """Load a model and verify the caller's access.

    Bug-8613: ``service_scope_verified`` is forwarded to
    ``ensure_project_model_access``. Routes that have already verified
    the service token's scope (via ``require_capability_or_service_scope``
    or equivalent) should pass ``True``; all others rely on the default
    ``False`` which refuses unverified service principals.
    """
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
        service_scope_verified=service_scope_verified,
    )
    return model
