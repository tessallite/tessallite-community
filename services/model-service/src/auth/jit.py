"""JIT user adoption and IdP group-to-role mapping.

Shared by the credential-based login endpoint and redirect-based SSO
callbacks (SAML ACS, OIDC callback).
"""
from __future__ import annotations

import logging
import secrets

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.backend import UserIdentity
from shared.auth.roles import PROJECT_ROLE_HIERARCHY, normalize_jit_default_role
from shared.db.models import (
    IdpGroupRoleMapping,
    LocalUser,
    ProjectSetting,
    UserAccessBinding,
)
from src.auth.local_backend import hash_password

logger = logging.getLogger(__name__)

# Group-mapping roles that materialise as a project ``UserAccessBinding``.
# ``model_technical`` is an audience role carried on ``LocalUser.role`` (the
# persona resolver consumes it); it is not a project RBAC tier, so it never
# becomes a binding.
_BINDING_ROLES = frozenset({"admin", "modeler", "viewer"})


async def resolve_jit_default_role(db: AsyncSession) -> str:
    result = await db.execute(
        select(ProjectSetting.value_json).where(
            ProjectSetting.key == "jit_default_role",
        )
    )
    row = result.scalar_one_or_none()
    if row and isinstance(row, dict):
        return normalize_jit_default_role(row.get("value"))
    return normalize_jit_default_role(None)


# Derived from the centralised hierarchy so the group-binding "best role"
# ranking cannot drift from the RBAC tier ordering (F-021-12). Highest tier
# gets the highest rank; unknown roles fall to 0 via ``.get(role, 0)``.
_ROLE_RANK = {
    role: len(PROJECT_ROLE_HIERARCHY) - idx
    for idx, role in enumerate(PROJECT_ROLE_HIERARCHY)
}


async def resolve_group_role(
    db: AsyncSession, groups: list[str], project_id: str | None = None,
) -> str | None:
    """Best role for the given IdP groups, scoped to ``project_id`` if given.

    With ``project_id`` set: considers project-scoped mappings for that project
    plus tenant-wide mappings. Without it: tenant-wide mappings only. Returns
    the highest-ranked matching role, or None.
    """
    if not groups:
        return None

    result = await db.execute(
        select(IdpGroupRoleMapping).where(
            IdpGroupRoleMapping.idp_group_name.in_(groups)
        )
    )
    mappings = result.scalars().all()
    if not mappings:
        return None

    best_role: str | None = None
    best_rank = -1
    for m in mappings:
        applies = (
            m.project_id is None
            or (project_id is not None and str(m.project_id) == str(project_id))
        )
        if not applies:
            continue
        rank = _ROLE_RANK.get(m.role, 0)
        if rank > best_rank:
            best_role = m.role
            best_rank = rank
    return best_role


async def _sync_group_bindings(
    db: AsyncSession, user_identity: str, groups: list[str],
) -> None:
    """F-021-03: materialise project ``UserAccessBinding`` rows from the IdP
    group-role mappings that match the user's groups.

    Without this, a project-scoped ``IdpGroupRoleMapping`` was stored and
    managed via the API but never consulted — an admin who mapped
    ``analysts -> modeler`` on a project granted those SSO users no access.
    Each matching project-scoped mapping now becomes (or updates) the user's
    binding on that project, with the highest mapped role winning per project
    (matching ``resolve_group_role``'s rank order). Tenant-wide mappings
    (``project_id IS NULL``) set the cosmetic ``LocalUser.role`` and, for the
    ``admin`` role, elevate to ``tenant_admin`` (see ``jit_adopt_user``); they
    do not fan out a binding to every project.

    Bindings are only ever created or have their role updated here — existing
    bindings on projects the user is not mapped to are left untouched, so a
    manual grant is never silently revoked by an SSO login.
    """
    if not groups:
        return

    result = await db.execute(
        select(IdpGroupRoleMapping).where(
            IdpGroupRoleMapping.idp_group_name.in_(groups),
            IdpGroupRoleMapping.project_id.is_not(None),
        )
    )
    mappings = result.scalars().all()
    if not mappings:
        return

    # Highest-ranked binding role per project.
    best_per_project: dict[str, str] = {}
    for m in mappings:
        if m.role not in _BINDING_ROLES:
            continue
        pid = str(m.project_id)
        current = best_per_project.get(pid)
        if current is None or _ROLE_RANK.get(m.role, 0) > _ROLE_RANK.get(current, 0):
            best_per_project[pid] = m.role

    for pid, role in best_per_project.items():
        existing = await db.execute(
            select(UserAccessBinding).where(
                UserAccessBinding.user_identity == user_identity,
                UserAccessBinding.project_id == pid,
                UserAccessBinding.model_id.is_(None),
            )
        )
        binding = existing.scalar_one_or_none()
        if binding is None:
            db.add(UserAccessBinding(
                user_identity=user_identity,
                role=role,
                project_id=pid,
                model_id=None,
            ))
        elif binding.role != role:
            binding.role = role


def _tenant_role_for_group_role(group_role: str | None) -> str | None:
    """Map a tenant-wide group-mapping role to a ``LocalUser.role``.

    A tenant-wide ``admin`` mapping is the only group role the platform's
    tenant taxonomy can honour directly: it elevates to ``tenant_admin`` (which
    ``require_role`` recognises on every project). ``modeler``/``viewer`` are
    project-scoped tiers with no tenant-wide representation, so they are carried
    on the binding path (``_sync_group_bindings``) rather than here.
    ``model_technical`` is an audience role and is passed through verbatim.
    """
    if group_role == "admin":
        return "tenant_admin"
    if group_role == "model_technical":
        return "model_technical"
    return None


async def jit_adopt_user(
    db: AsyncSession,
    identity: UserIdentity,
    tenant_id: str,
) -> tuple[LocalUser, str]:
    """Ensure the identity has a local_users record. Returns (user, role).

    On every SSO login (new or returning user) the IdP group-role mappings are
    re-applied (F-021-03): project-scoped mappings materialise/refresh
    ``UserAccessBinding`` rows, and a tenant-wide ``admin`` mapping elevates the
    user to ``tenant_admin``. SSO email matching is case-insensitive
    (F-021-09) — IdPs vary case and a returning user must match their existing
    record rather than spawn a duplicate.
    """
    email = identity.email.strip().lower()
    result = await db.execute(
        select(LocalUser).where(func.lower(LocalUser.email) == email)
    )
    local_user = result.scalar_one_or_none()

    # Tenant-wide group role (project_id IS NULL) for cosmetic LocalUser.role.
    tenant_group_role = await resolve_group_role(db, identity.groups)
    mapped_tenant_role = _tenant_role_for_group_role(tenant_group_role)

    if local_user is None:
        # New user role precedence: a tenant-wide ``admin`` mapping elevates to
        # tenant_admin; otherwise the matched group role is kept as the cosmetic
        # LocalUser.role (the JIT default applies when no group matched). The
        # access-granting half is the binding sync below — project-scoped
        # mappings now materialise real UserAccessBinding rows (F-021-03).
        jit_role = (
            mapped_tenant_role
            or tenant_group_role
            or await resolve_jit_default_role(db)
        )
        local_user = LocalUser(
            username=email.split("@")[0],
            email=email,
            hashed_password=hash_password(secrets.token_urlsafe(64)),
            is_active=True,
            role=jit_role,
            auth_source=identity.source_backend,
            has_completed_onboarding=False,
        )
        db.add(local_user)
        await db.flush()
        logger.info(
            "JIT adopted user %s via %s (tenant=%s, role=%s)",
            email, identity.source_backend, tenant_id, jit_role,
        )
    elif mapped_tenant_role and local_user.role != mapped_tenant_role:
        # A returning user whose tenant-wide mapping now grants a stronger
        # tenant role is upgraded; never silently downgraded below the mapping.
        if (
            mapped_tenant_role == "tenant_admin"
            or local_user.role not in ("tenant_admin", "system_admin")
        ):
            local_user.role = mapped_tenant_role

    # Materialise project bindings from project-scoped mappings (F-021-03).
    await _sync_group_bindings(db, email, identity.groups)

    await db.commit()
    await db.refresh(local_user)
    return local_user, local_user.role
