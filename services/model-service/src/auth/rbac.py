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

Access is binding-only (F-021-04 hard cutover, Wave C decision #9,
2026-08-19). There is NO zero-binding "first-arriver / bootstrap admin"
grant: a project with no bindings denies every ordinary user. Human
tenant_admin / canonical system_admin still bypass per-project bindings.
New projects create the creator's admin binding atomically at project
creation; a legacy binding-less project is repaired only by a human
tenant/system admin via the explicit access-repair operation.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional
from uuid import UUID

from fastapi import Depends, HTTPException, status

from shared.auth.identity import canonical_user_identity, user_identity_matches
from shared.auth.roles import PROJECT_ROLE_HIERARCHY, project_role_level
from shared.db.models import UserAccessBinding
from shared.db.session import get_tenant_db
from src.auth.middleware import (
    CurrentEmbedUser,
    CurrentServiceUser,
    CurrentUser,
    get_current_user,
    is_human_tenant_admin_or_system_admin,
)

logger = logging.getLogger(__name__)

# Ordered from highest to lowest privilege. Sourced from the shared role
# taxonomy so this module cannot drift from the SSO/JIT/frontend layers
# (F-021-12). Value is identical to the previous literal ["admin",
# "modeler", "viewer"].
ROLE_HIERARCHY: list[str] = list(PROJECT_ROLE_HIERARCHY)


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
    3. No binding => 403. There is NO zero-binding bootstrap-admin grant
       (F-021-04 hard cutover, decision #9). Human tenant/system admins bypass
       above; every other caller needs a persisted binding.

    Args:
        min_role:  Minimum role required ("admin" | "modeler" | "viewer").
    """
    from sqlalchemy import select

    async def _dependency(
        project_id: UUID,
        model_id: Optional[UUID] = None,
        current_user: CurrentUser = Depends(get_current_user),
    ) -> None:
        # Human tenant admins and canonical human system admins bypass all
        # per-project binding checks. Typed service principals must use
        # explicit service-scope dependencies and never inherit human RBAC.
        if is_human_tenant_admin_or_system_admin(current_user):
            return

        # Service principals must NEVER enter human RBAC paths. This guard keeps
        # a service token out of the human binding-lookup path entirely — it
        # must use explicit service-scope dependencies, never inherit human RBAC
        # (the documented scope-only contract, AUTH-RR-01).
        if isinstance(current_user, CurrentServiceUser):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Service tokens must use scope-based access",
            )

        # Embed tokens are self-contained scoped grants. Read-only routes
        # (viewer+) allow them through; mutation routes (modeler+, admin)
        # reject embed users — they have no binding and should not modify
        # models.
        #
        # F-021-02 / Bug-7992: the embed token's OWN project/model scope is
        # enforced HERE, at the single choke point every model-service route
        # passes through, using the ``project_id``/``model_id`` FastAPI already
        # resolved from the path. Previously this branch returned early and
        # relied on each route calling ``enforce_model_scope`` — but the large
        # majority of viewer-admitting metadata routes never did, so a token
        # scoped to project P1 (``model_ids`` null) could read another project's
        # models, measures, joins, personas, etc. Enforcing the scope on the
        # shared dependency closes the whole class of routes at once; the
        # per-route ``enforce_model_scope`` calls remain as defence in depth.
        if isinstance(current_user, CurrentEmbedUser):
            if _role_level(min_role) < _role_level("viewer"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Embed tokens cannot modify project resources",
                )
            if current_user.project_ids is not None:
                allowed_projects = {str(p).lower() for p in current_user.project_ids}
                if str(project_id).lower() not in allowed_projects:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Project not in embed token scope",
                    )
            if model_id is not None and current_user.model_ids is not None:
                allowed_models = {str(m).lower() for m in current_user.model_ids}
                if str(model_id).lower() not in allowed_models:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Model not in embed token scope",
                    )
            return

        async for db in get_tenant_db(current_user.tenant_id):
            effective_role: str | None = None

            # --- model-scoped binding (highest priority) ---
            if model_id is not None:
                model_result = await db.execute(
                    select(UserAccessBinding).where(
                        user_identity_matches(UserAccessBinding.user_identity, current_user.user_id),
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
                        user_identity_matches(UserAccessBinding.user_identity, current_user.user_id),
                        UserAccessBinding.project_id == project_id,
                        UserAccessBinding.model_id.is_(None),
                    )
                )
                proj_binding = result.scalar_one_or_none()
                if proj_binding is not None:
                    effective_role = proj_binding.role

            # --- no binding => deny (F-021-04 hard cutover, decision #9) ---
            # There is NO zero-binding bootstrap-admin grant. A project with no
            # binding for this caller denies, whether or not the project has
            # any bindings at all.
            if effective_role is None:
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
      human tenant_admin/canonical system_admin -> True; model-scoped binding
      -> project-scoped binding -> no binding => False. There is NO zero-binding
      bootstrap grant (F-021-04 hard cutover, decision #9).
    Embed users are never elevated past ``viewer`` here.
    """
    from sqlalchemy import select

    if is_human_tenant_admin_or_system_admin(current_user):
        return True
    # Service principals never have project RBAC bindings (AUTH-RR-01).
    if isinstance(current_user, CurrentServiceUser):
        return False
    if isinstance(current_user, CurrentEmbedUser):
        # Embed tokens are read-only; never elevated to modeler/admin.
        if _role_level(min_role) < _role_level("viewer"):
            return False
        # F-021-02 / Bug-7992: an embed token only "has" viewer on a project/
        # model that is within its own scope. Out-of-scope resources return
        # False (defence in depth alongside the require_role choke point).
        if current_user.project_ids is not None:
            allowed_projects = {str(p).lower() for p in current_user.project_ids}
            if str(project_id).lower() not in allowed_projects:
                return False
        if model_id is not None and current_user.model_ids is not None:
            allowed_models = {str(m).lower() for m in current_user.model_ids}
            if str(model_id).lower() not in allowed_models:
                return False
        return True

    effective_role: str | None = None
    if model_id is not None:
        model_result = await db.execute(
            select(UserAccessBinding).where(
                user_identity_matches(UserAccessBinding.user_identity, current_user.user_id),
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
                user_identity_matches(UserAccessBinding.user_identity, current_user.user_id),
                UserAccessBinding.project_id == project_id,
                UserAccessBinding.model_id.is_(None),
            )
        )
        proj_binding = proj_result.scalar_one_or_none()
        if proj_binding is not None:
            effective_role = proj_binding.role

    if effective_role is None:
        # No binding => not privileged. No zero-binding bootstrap grant
        # (F-021-04 hard cutover, decision #9).
        return False

    return _role_level(effective_role) <= _role_level(min_role)


async def resolve_listable_model_scope(
    db,
    current_user: CurrentUser,
    project_id: UUID,
) -> Optional[set[str]]:
    """Authorize the model-LIST endpoint and return the caller's visible scope.

    Codex-HIGH (Bug-8101 follow-up): the plain ``require_role("viewer")`` gate
    fails a MODEL-SCOPED viewer/model_viewer on the list route. A list request
    carries no ``model_id``, so ``require_role`` only consulted the project-wide
    binding, found none, and returned 403 — even though ``GET
    /projects/P/models/M`` and query execution honour the model-scoped grant.
    The list endpoint must instead authorize the caller AND filter to the models
    they may see.

    Returns:
      * ``None``  — the caller may see ALL models in the project (a project-wide
        binding of any role, or a human tenant/system admin).
      * ``set[str]`` — the caller may see ONLY these model ids (they hold only
        model-scoped bindings). An empty set means "authenticated but scoped to
        no model here" — an empty list, not a 403.

    Raises ``HTTPException(403)`` when the caller has no binding on the project.
    Mirrors ``require_role`` precedence; every project binding role
    (admin/modeler/viewer/model_viewer) satisfies read, so any binding grants
    visibility of its scope. There is NO zero-binding bootstrap grant
    (F-021-04 hard cutover, decision #9).
    """
    from sqlalchemy import select

    # Human tenant/system admins see every model in the project.
    if is_human_tenant_admin_or_system_admin(current_user):
        return None

    # Service principals must use scope-based access, never human RBAC.
    if isinstance(current_user, CurrentServiceUser):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Service tokens must use scope-based access",
        )

    # Embed tokens: project scope is enforced by the caller
    # (_enforce_embed_project_scope); narrow to the token's model scope here.
    if isinstance(current_user, CurrentEmbedUser):
        if current_user.model_ids is not None:
            return {str(m).lower() for m in current_user.model_ids}
        return None

    # Human user: load every binding they hold in this project.
    result = await db.execute(
        select(UserAccessBinding.model_id).where(
            user_identity_matches(UserAccessBinding.user_identity, current_user.user_id),
            UserAccessBinding.project_id == project_id,
        )
    )
    model_ids = [row[0] for row in result.all()]
    if model_ids:
        # A project-wide binding (NULL model_id) grants visibility of all models.
        if any(mid is None for mid in model_ids):
            return None
        # Otherwise only the explicitly granted models are visible.
        return {str(mid).lower() for mid in model_ids}

    # No binding for this user => deny. There is NO zero-binding bootstrap
    # grant (F-021-04 hard cutover, decision #9). A nonexistent project and an
    # inaccessible one return the SAME 403, so no project-existence oracle
    # exists here.
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Access denied: no binding for this project",
    )


async def filter_projects_by_user_access(
    db,
    project_ids: list,
    user_identity: str,
    current_user: CurrentUser | None = None,
) -> set:
    """Return the subset of ``project_ids`` the given user can see.

    Binding-only visibility (F-021-04 hard cutover, decision #9): a project is
    visible only to users who hold a binding on it. A project with zero
    bindings is visible to NO ordinary user — there is no zero-binding
    bootstrap grant. The binding's ROLE is deliberately not consulted here — any
    binding of any role grants visibility, because this is just a visibility
    filter for tenant discovery; individual operations still go through
    ``require_role`` to check the minimum privilege. (An unused ``role``
    parameter was removed in the F-021-04 round-2 cleanup — F7.)

    Human tenant admins and canonical human system admins bypass the binding
    check and see every project in the requested list. Service principals with
    admin role claims must still rely on persisted bindings.
    """
    from sqlalchemy import select

    user_identity = canonical_user_identity(user_identity)

    if not project_ids:
        return set()

    if current_user is not None and is_human_tenant_admin_or_system_admin(current_user):
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
            # No bindings at all → visible to NO ordinary user (no zero-binding
            # bootstrap grant, F-021-04 hard cutover, decision #9).
            continue
        if user_identity in {canonical_user_identity(identity) for identity in bindings}:
            visible.add(pid)
    return visible
