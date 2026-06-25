"""
Role-based access control for Tessallite model-service.

Role hierarchy (highest to lowest privilege):
    admin   → full CRUD; manage access bindings, connections
    modeler → CRUD on models, dimensions, measures, joins, aggregates
    viewer  → read-only on all resources + query execution

Usage in route handlers:

    from src.auth.rbac import require_role

    @router.delete(
        "/{project_id}",
        dependencies=[require_role("admin")],
    )
    async def delete_project(
        project_id: UUID,
        current_user: CurrentUser = Depends(get_current_user),
    ): ...

The dependency reads ``project_id`` from the path automatically — every
route that uses it must declare ``project_id`` as a path parameter.
The old ``project_id_param`` kwarg was removed in CR-010.

require_role returns a FastAPI Depends-compatible async callable.
Each call fetches the caller's binding from the tenant DB (no caching so
revocations take effect immediately on the next request).

Bootstrap rule: if the project has zero bindings, the first authenticated
user is treated as admin. This allows the tenant creator to set up access
without a chicken-and-egg problem.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional
from uuid import UUID

from fastapi import Depends, HTTPException, status

from shared.auth.roles import PROJECT_ROLE_HIERARCHY, project_role_level
from shared.db.models import UserAccessBinding
from shared.db.session import get_tenant_db
from src.auth.middleware import CurrentEmbedUser, CurrentUser, get_current_user

logger = logging.getLogger(__name__)

# Ordered from highest to lowest privilege. Sourced from the shared role
# taxonomy so this module cannot drift from the SSO/JIT/frontend layers
# (F-021-12). Value is identical to the previous literal ["admin",
# "modeler", "viewer"].
ROLE_HIERARCHY: list[str] = list(PROJECT_ROLE_HIERARCHY)

# ---------------------------------------------------------------------------
# Bootstrap-admin audit (F-021-02, accepted risk per decision D2)
# ---------------------------------------------------------------------------
# The bootstrap rule fires on every request to a binding-less project, so a
# raw per-fire audit write would flood the log. Deduplicate per
# (tenant, user, project) within a TTL window per process — the security
# signal is "this user exercised implicit admin on this project", not the
# request count.
_BOOTSTRAP_AUDIT_TTL_SECONDS = 3600
_BOOTSTRAP_AUDIT_MAX_KEYS = 10_000
_bootstrap_audit_seen: dict[tuple[str, str, str], float] = {}


async def _audit_bootstrap_admin_grant(db, current_user: CurrentUser, project_id: UUID) -> None:
    """Write an audit event recording that the bootstrap-admin rule granted
    project access to a caller without any binding. Best-effort: failures are
    logged and never block the request."""
    key = (str(current_user.tenant_id), str(current_user.user_id), str(project_id))
    now = time.monotonic()
    last = _bootstrap_audit_seen.get(key)
    if last is not None and now - last < _BOOTSTRAP_AUDIT_TTL_SECONDS:
        return
    if len(_bootstrap_audit_seen) >= _BOOTSTRAP_AUDIT_MAX_KEYS:
        _bootstrap_audit_seen.clear()  # crude reset; only affects dedup
    _bootstrap_audit_seen[key] = now
    try:
        from shared.audit.logger import audit

        await audit(
            db,
            action="rbac.bootstrap_admin_grant",
            severity="warn",
            actor_email=getattr(current_user, "email", None),
            target_type="project",
            target_id=project_id,
            detail={
                "rule": "bootstrap_admin",
                "user_identity": str(current_user.user_id),
                "reason": (
                    "Project has no access bindings — caller granted implicit "
                    "admin (accepted-risk decision D2; see security help)."
                ),
            },
        )
        await db.commit()
    except Exception:
        logger.warning(
            "Failed to write bootstrap-admin audit event (project=%s)",
            project_id, exc_info=True,
        )


def _role_level(role: str) -> int:
    """Lower index = more privileged. Returns len(ROLE_HIERARCHY) if unknown.

    Delegates to the shared ``project_role_level`` so the privilege ordering
    is defined once (F-021-12). Behaviour for the three RBAC tiers
    (admin/modeler/viewer) and for unknown roles is identical to the previous
    ``ROLE_HIERARCHY.index`` implementation; legacy aliases (member/analyst/
    model_technical) additionally resolve to viewer level rather than full
    deny — these never reach this function via real bindings or min_role
    literals, so enforcement is unchanged.
    """
    return project_role_level(role)


def require_role(min_role: str) -> Callable:
    """
    Return a FastAPI dependency that enforces *min_role* or higher.

    The dependency reads ``project_id`` (and optionally ``model_id``) from the
    path automatically via FastAPI's parameter resolution.

    Precedence:
    1. model-scoped binding (user_identity, project_id, model_id) — if model_id
       is in the path and a binding exists for it, use that role.
    2. project-scoped binding (user_identity, project_id, NULL model_id).
    3. Bootstrap-admin rule: if the project has zero bindings at all, treat
       caller as admin (first user becomes implicit admin).

    Args:
        min_role:  Minimum role required ("admin" | "modeler" | "viewer").
    """
    from sqlalchemy import select

    async def _dependency(
        project_id: UUID,
        model_id: Optional[UUID] = None,
        current_user: CurrentUser = Depends(get_current_user),
    ) -> None:
        # tenant_admin and system_admin bypass all per-project binding checks.
        if current_user.role in ("tenant_admin", "system_admin"):
            return

        # Embed tokens are self-contained scoped grants. Read-only routes
        # (viewer+) allow them through; model_ids scope is enforced by
        # enforce_model_scope() at the route level. Mutation routes
        # (modeler+, admin) reject embed users — they have no binding
        # and should not modify models.
        if isinstance(current_user, CurrentEmbedUser):
            if _role_level(min_role) >= _role_level("viewer"):
                return
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Embed tokens cannot modify project resources",
            )

        async for db in get_tenant_db(current_user.tenant_id):
            effective_role: str | None = None

            # --- model-scoped binding (highest priority) ---
            if model_id is not None:
                model_result = await db.execute(
                    select(UserAccessBinding).where(
                        UserAccessBinding.user_identity == current_user.user_id,
                        UserAccessBinding.project_id == project_id,
                        UserAccessBinding.model_id == model_id,
                    )
                )
                model_binding = model_result.scalar_one_or_none()
                if model_binding is not None:
                    effective_role = model_binding.role

            # --- project-scoped binding (fallback) ---
            if effective_role is None:
                result = await db.execute(
                    select(UserAccessBinding).where(
                        UserAccessBinding.user_identity == current_user.user_id,
                        UserAccessBinding.project_id == project_id,
                        UserAccessBinding.model_id.is_(None),
                    )
                )
                proj_binding = result.scalar_one_or_none()
                if proj_binding is not None:
                    effective_role = proj_binding.role

            # --- bootstrap rule ---
            if effective_role is None:
                # Existence probe only: a project may carry many bindings.
                # Using limit(1)+first() (not scalar_one_or_none) keeps the
                # deny path returning a clean 403 instead of raising
                # MultipleResultsFound -> HTTP 500 (F-H27R1-01).
                existence_result = await db.execute(
                    select(UserAccessBinding.id)
                    .where(UserAccessBinding.project_id == project_id)
                    .limit(1)
                )
                any_binding = existence_result.first()
                if any_binding is None:
                    # No bindings yet — implicit admin for first user.
                    # F-021-02 (accepted risk, D2): record that the open
                    # bootstrap rule fired so admins can see who used it.
                    await _audit_bootstrap_admin_grant(db, current_user, project_id)
                    return

                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: no binding for this project",
                )

            caller_level = _role_level(effective_role)
            required_level = _role_level(min_role)
            if caller_level > required_level:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied: requires '{min_role}', caller has '{effective_role}'",
                )

    return Depends(_dependency)


async def caller_has_role(
    db,
    current_user: CurrentUser,
    project_id: UUID,
    min_role: str,
    model_id: Optional[UUID] = None,
) -> bool:
    """Return whether ``current_user`` holds ``min_role`` or higher on the
    project/model, using the same precedence as :func:`require_role`.

    This is the in-handler companion to ``require_role``: a route that already
    admits ``viewer`` (via the route dependency) can call this to decide
    whether the caller is *also* a ``modeler``/``admin`` and therefore allowed
    to act on resources they do not own (e.g. owner-or-modeler edit/delete of a
    shared saved query, F-029-03). It never raises on a denied role — it
    returns ``False`` — so the caller composes it with an ownership check.

    Precedence (mirrors ``require_role._dependency``):
      tenant_admin/system_admin -> True; model-scoped binding -> project-scoped
      binding -> bootstrap rule (no bindings at all => implicit admin).
    Embed users are never elevated past ``viewer`` here.
    """
    from sqlalchemy import select

    if current_user.role in ("tenant_admin", "system_admin"):
        return True
    if isinstance(current_user, CurrentEmbedUser):
        # Embed tokens are read-only; never elevated to modeler/admin.
        return _role_level(min_role) >= _role_level("viewer")

    effective_role: str | None = None
    if model_id is not None:
        model_result = await db.execute(
            select(UserAccessBinding).where(
                UserAccessBinding.user_identity == current_user.user_id,
                UserAccessBinding.project_id == project_id,
                UserAccessBinding.model_id == model_id,
            )
        )
        model_binding = model_result.scalar_one_or_none()
        if model_binding is not None:
            effective_role = model_binding.role

    if effective_role is None:
        proj_result = await db.execute(
            select(UserAccessBinding).where(
                UserAccessBinding.user_identity == current_user.user_id,
                UserAccessBinding.project_id == project_id,
                UserAccessBinding.model_id.is_(None),
            )
        )
        proj_binding = proj_result.scalar_one_or_none()
        if proj_binding is not None:
            effective_role = proj_binding.role

    if effective_role is None:
        # Bootstrap rule: a project with zero bindings treats every
        # authenticated caller as admin (existence probe, no MultipleResults).
        existence_result = await db.execute(
            select(UserAccessBinding.id)
            .where(UserAccessBinding.project_id == project_id)
            .limit(1)
        )
        if existence_result.first() is None:
            return True
        return False

    return _role_level(effective_role) <= _role_level(min_role)


async def filter_projects_by_user_access(
    db,
    project_ids: list,
    user_identity: str,
    role: str | None = None,
) -> set:
    """Return the subset of ``project_ids`` the given user can see.

    Matches the bootstrap-admin rule in ``require_role``: if a
    project has zero bindings at all, it is visible to every
    authenticated user (the first user becomes implicit admin).
    If a project has at least one binding, only users with a binding
    on that project are granted visibility. Role is not enforced
    here — the list is just a visibility filter for tenant
    discovery; individual operations still go through
    ``require_role`` to check the minimum privilege.

    Tenant-wide roles (``tenant_admin`` / ``system_admin``) bypass the
    binding check and see every project in the requested list.
    """
    from sqlalchemy import select

    if not project_ids:
        return set()

    if role in ("tenant_admin", "system_admin"):
        return set(project_ids)

    # Load every binding for any project in the requested list in a
    # single query so we don't round-trip per project.
    result = await db.execute(
        select(UserAccessBinding.project_id, UserAccessBinding.user_identity)
        .where(UserAccessBinding.project_id.in_(project_ids))
    )
    bindings_by_project: dict = {}
    for pid, uid in result.all():
        bindings_by_project.setdefault(pid, set()).add(uid)

    visible: set = set()
    for pid in project_ids:
        bindings = bindings_by_project.get(pid)
        if not bindings:
            # Bootstrap: no bindings at all → visible to everyone.
            visible.add(pid)
            continue
        if user_identity in bindings:
            visible.add(pid)
    return visible
