"""
Access-binding management routes.

Only project admins (or human tenant/system admins) may manage bindings. There
is NO zero-binding bootstrap grant (F-021-04, decision #9): on a binding-less
legacy project only a human tenant/system admin can seed the first admin binding
via the explicit repair route.

Routes:
  POST   /api/v1/projects/{project_id}/access           — grant role to user
  POST   /api/v1/projects/{project_id}/access/repair     — seed initial admin binding
  GET    /api/v1/projects/{project_id}/access           — list bindings
  DELETE /api/v1/projects/{project_id}/access/{id}      — revoke binding
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from shared.audit.logger import audit_required
from shared.auth.identity import canonical_user_identity, user_identity_matches
from shared.auth.roles import (
    is_model_viewer_role,
    is_modeller_role,
    scope_covers,
)
from shared.db.models import Project, UserAccessBinding
from shared.db.session import get_tenant_db
from shared.webhooks.dispatcher import emit_webhook_logged as emit_webhook
from shared.schemas.pydantic_models import (
    AccessSupersedePreflight,
    AccessSupersedePreflightResponse,
    UserAccessBindingCreate,
    UserAccessBindingResponse,
)
from src.auth.middleware import CurrentUser, forbid_embed_user, require_tenant_admin
from src.auth.rbac import require_role

router = APIRouter(
    prefix="/projects/{project_id}/access",
    tags=["access"],
)


async def _load_user_project_bindings(
    db, *, project_id: UUID, user_identity: str
) -> list[UserAccessBinding]:
    """All of one user's bindings within a project (any model scope)."""
    result = await db.execute(
        select(UserAccessBinding).where(
            user_identity_matches(UserAccessBinding.user_identity, user_identity),
            UserAccessBinding.project_id == project_id,
        )
    )
    return list(result.scalars().all())


async def _lock_user_project_grants(db, *, project_id: UUID, user_identity: str) -> None:
    """Serialize concurrent grants for the same ``(user, project)`` scope so the
    Modeller/Model-viewer supersession check-then-write cannot interleave
    (Codex-MEDIUM).

    Without this, two concurrent grants (e.g. a project-wide Modeller and a
    model-scoped Model-viewer for the same user) both read the pre-change
    binding set, each see no conflict, and both insert — leaving the user with
    both roles, where the model-scoped Model-viewer then takes RBAC precedence
    on that model and silently downgrades the intended Modeller.

    A transaction-scoped Postgres advisory lock (``pg_advisory_xact_lock``)
    keyed on a stable hash of the ``(project, user)`` scope forces the second
    grant to wait until the first commits; the supersession check that follows
    then runs against the committed state. The lock releases automatically on
    commit/rollback. Callers MUST hold it for the whole read-modify-write.
    """
    key = f"grant:{project_id}:{canonical_user_identity(user_identity)}"
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))").bindparams(k=key)
    )


async def _lock_project_grants(db, *, project_id: UUID) -> None:
    """Serialize binding mutations that reason about the WHOLE project's binding
    set — specifically the legacy binding-less repair op (F4).

    ``_lock_user_project_grants`` above is keyed on ``(project, user)``, correct
    for grant_access's per-user supersession check. The repair op is different:
    it decides on the presence of ANY binding on the project, so two concurrent
    repairs targeting the SAME project but DIFFERENT users would take different
    per-user locks, both observe zero bindings, and both insert — violating the
    "seed the INITIAL admin binding" invariant (two admin bindings, no
    fail-closed 409). Keying the advisory lock on the project ALONE forces those
    repairs to serialise: the first commits its binding, the second then sees it
    and returns 409. Same mechanism as ``_lock_user_project_grants``
    (``pg_advisory_xact_lock`` on a hashed key), project-scoped; released
    automatically on commit/rollback. The caller MUST hold it across the whole
    existence-check-then-insert.
    """
    key = f"grant:{project_id}"
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))").bindparams(k=key)
    )


def _superseded_model_viewer_bindings(
    bindings: list[UserAccessBinding],
    *,
    grant_role: str,
    grant_model_id,
) -> list[UserAccessBinding]:
    """Model-viewer bindings the incoming ``modeler`` grant would supersede
    (Bug-8101).

    The Modeller/Model-viewer mutual-exclusivity invariant (spec D2): a Modeller
    binding wins over a Model-viewer binding whose scope it COVERS. This returns
    the existing model_viewer bindings that must be removed so only Modeller
    remains, evaluated by scope CONTAINMENT (not mere overlap):

    - Granting ``modeler`` at scope S: every existing ``model_viewer`` binding
      whose scope S covers is superseded. A project-wide modeler covers all
      model_viewer bindings in the project; a model-scoped modeler covers only
      the same-model model_viewer. A project-wide model_viewer is therefore NOT
      superseded by a model-scoped modeler — the viewer retains read access on
      the other models where the user is not a Modeller (spec:44-46).
    - Granting ``model_viewer``: no existing binding is deleted here; redundancy
      under a covering modeler is handled by ``_grant_is_redundant_under_modeller``
      (the grant is simply not persisted). Other model_viewer rows are untouched.
    """
    if is_modeller_role(grant_role):
        return [
            b
            for b in bindings
            if is_model_viewer_role(b.role)
            and scope_covers(grant_model_id, b.model_id)
        ]
    return []


def _grant_is_redundant_under_modeller(
    bindings: list[UserAccessBinding],
    *,
    grant_role: str,
    grant_model_id,
) -> bool:
    """True when an incoming ``model_viewer`` grant is fully covered by an
    existing ``modeler`` binding and must therefore NOT be persisted (only
    Modeller remains). Modeller strictly supersedes Model-viewer.

    Coverage is asymmetric: an existing modeler binding covers the incoming
    model_viewer only when the modeler scope is a superset of the grant scope
    (a project-wide modeler covers a model-scoped grant; a model-scoped modeler
    does NOT cover a project-wide grant)."""
    if not is_model_viewer_role(grant_role):
        return False
    return any(
        is_modeller_role(b.role) and scope_covers(b.model_id, grant_model_id)
        for b in bindings
    )


@router.post(
    "",
    response_model=UserAccessBindingResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("admin")],
)
async def grant_access(
    project_id: UUID,
    body: UserAccessBindingCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
    supersede: bool = False,
) -> UserAccessBindingResponse:
    """Grant a role to a user for this project (or a specific model in it).

    F-021-04/F-021-11: the upsert key is the binding's true identity
    ``(user_identity, project_id, model_id)`` — role is a mutable attribute,
    not part of identity. A model-scoped grant therefore no longer overwrites
    a project-level row, and a user holding both a project-level and a
    model-level binding can no longer cause a ``MultipleResultsFound`` 500.
    NULL ``model_id`` (project-level) is matched distinctly from any concrete
    model id.

    Bug-8101 / spec D2 — Modeller supersedes Model-viewer (mutual exclusivity).
    A user must not effectively hold both ``modeler`` and ``model_viewer`` for
    an overlapping scope. This is the authoritative choke point for BOTH admin
    surfaces (tenant user-admin and system user-management) and BOTH assignment
    directions:

    - Granting ``modeler`` where an overlapping ``model_viewer`` binding exists,
      or granting ``model_viewer`` where an overlapping ``modeler`` exists,
      requires explicit operator confirmation (``supersede=true``). Without it
      the request is rejected 409 so the caller can surface the confirmation
      "Modeller supersedes Model viewer. The Model viewer role will be removed."
      and neither change is applied (cancel path).
    - On confirm, any overlapping ``model_viewer`` binding is removed and only
      Modeller remains. Granting ``model_viewer`` under an existing overlapping
      ``modeler`` never persists a redundant ``model_viewer`` row.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        user_identity = canonical_user_identity(body.user_identity)

        # --- Modeller/Model-viewer supersession (authoritative) ---
        # Serialize concurrent grants for this (user, project) BEFORE reading the
        # binding set, so the check-then-write below is race-free (Codex-MEDIUM).
        await _lock_user_project_grants(
            db, project_id=project_id, user_identity=user_identity
        )
        existing_bindings = await _load_user_project_bindings(
            db, project_id=project_id, user_identity=user_identity
        )
        superseded = _superseded_model_viewer_bindings(
            existing_bindings, grant_role=body.role, grant_model_id=body.model_id
        )
        redundant = _grant_is_redundant_under_modeller(
            existing_bindings, grant_role=body.role, grant_model_id=body.model_id
        )
        if (superseded or redundant) and not supersede:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="modeller_supersedes_model_viewer",
            )
        if superseded:
            # Do not remove the row we are about to upsert into (the concrete
            # target scope) if it is itself a model_viewer being replaced by a
            # modeler — the upsert below flips its role instead.
            for b in superseded:
                same_scope = (
                    (b.model_id is None and body.model_id is None)
                    or (
                        b.model_id is not None
                        and body.model_id is not None
                        and str(b.model_id) == str(body.model_id)
                    )
                )
                if same_scope and is_modeller_role(body.role):
                    continue
                await db.delete(b)

        if redundant:
            await audit_required(
                db, action="access.grant", severity="warn",
                actor_email=current_user.email,
                target_type="access_binding",
                detail={
                    "user_identity": user_identity,
                    "role": body.role,
                    "project_id": str(project_id),
                    "redundant": True,
                },
            )
            await db.commit()
            modeler_binding = next(
                b
                for b in existing_bindings
                if is_modeller_role(b.role)
                and scope_covers(b.model_id, body.model_id)
            )
            await db.refresh(modeler_binding)
            return UserAccessBindingResponse.model_validate(modeler_binding)

        model_filter = (
            UserAccessBinding.model_id == body.model_id
            if body.model_id is not None
            else UserAccessBinding.model_id.is_(None)
        )
        existing = await db.execute(
            select(UserAccessBinding).where(
                user_identity_matches(UserAccessBinding.user_identity, user_identity),
                UserAccessBinding.project_id == project_id,
                model_filter,
            )
        )
        binding = existing.scalar_one_or_none()
        if binding:
            binding.role = body.role
            # Bug-6303: an explicit operator grant is a MANUAL action and must
            # pin the binding as manual. If this scope was previously an
            # ``sso_group`` binding, leaving ``source`` untouched would let the
            # SSO group sync (``jit._sync_group_bindings``) later revoke the
            # admin's explicit grant on IdP de-provisioning — a lockout. Pinning
            # to ``manual`` makes the grant durable and un-revocable by SSO.
            binding.source = "manual"
        else:
            binding = UserAccessBinding(
                user_identity=user_identity,
                role=body.role,
                project_id=project_id,
                model_id=body.model_id,
                source="manual",
            )
            db.add(binding)
        await db.flush()
        await audit_required(
            db, action="access.grant", severity="warn",
            actor_email=current_user.email,
            target_type="access_binding",
            target_id=binding.id,
            detail={
                "user_identity": user_identity,
                "role": body.role,
                "project_id": str(project_id),
                "model_id": str(body.model_id) if body.model_id else None,
            },
        )
        await emit_webhook(current_user.tenant_id, "access.granted", {
            "user_identity": user_identity,
            "role": body.role,
            "project_id": str(project_id),
        })
        await db.commit()
        await db.refresh(binding)
        return UserAccessBindingResponse.model_validate(binding)


@router.post(
    "/preflight",
    response_model=AccessSupersedePreflightResponse,
    dependencies=[require_role("admin")],
)
async def preflight_access(
    project_id: UUID,
    body: AccessSupersedePreflight,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> AccessSupersedePreflightResponse:
    """Dry-run a grant to tell the admin UI whether the Modeller/Model-viewer
    supersession rule will fire (Bug-8101 / spec D2).

    The tenant user-admin and system user-management surfaces call this before
    ``POST /access`` so they can show the confirmation "Modeller supersedes
    Model viewer. The Model viewer role will be removed." and only send the
    grant with ``supersede=true`` when the admin confirms. Nothing is mutated
    here.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        user_identity = canonical_user_identity(body.user_identity)
        existing_bindings = await _load_user_project_bindings(
            db, project_id=project_id, user_identity=user_identity
        )
        superseded = _superseded_model_viewer_bindings(
            existing_bindings, grant_role=body.role, grant_model_id=body.model_id
        )
        redundant = _grant_is_redundant_under_modeller(
            existing_bindings, grant_role=body.role, grant_model_id=body.model_id
        )
        return AccessSupersedePreflightResponse(
            supersedes=bool(superseded or redundant),
            removed_model_viewer_count=len(superseded),
            grant_is_redundant=redundant,
        )


class ProjectAdminRepairRequest(BaseModel):
    """Body for the legacy binding-less project repair op (F-021-04)."""

    user_identity: str = Field(..., min_length=1)


@router.post(
    "/repair",
    response_model=UserAccessBindingResponse,
    status_code=status.HTTP_201_CREATED,
)
async def repair_project_admin_binding(
    project_id: UUID,
    body: ProjectAdminRepairRequest,
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> UserAccessBindingResponse:
    """Create the INITIAL project-admin binding for a legacy binding-less project.

    F-021-04 (Wave C decision #9) removed the zero-binding bootstrap-admin
    grant, so a project created/imported before this cutover with no bindings
    denies every ordinary user. This explicit repair operation — usable ONLY by
    a human tenant_admin / canonical system_admin (``require_tenant_admin``) —
    seeds the first project-admin binding so the project can be governed again.

    It is deliberately narrow and fails closed: it refuses (409) on any project
    that already has at least one binding, so it can never be used to escalate
    on a project that already has access control. Use ``POST /access`` for
    ordinary grants once at least one admin binding exists.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        project = await db.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        user_identity = canonical_user_identity(body.user_identity)
        # F4: serialise concurrent repairs on this project so two callers cannot
        # both observe zero bindings and both seed an admin binding. Acquired
        # BEFORE the existence probe so the check-then-insert is race-free; the
        # transaction-scoped lock releases on commit/rollback below.
        await _lock_project_grants(db, project_id=project_id)
        existing = (
            await db.execute(
                select(UserAccessBinding.id)
                .where(UserAccessBinding.project_id == project_id)
                .limit(1)
            )
        ).first()
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Project already has access bindings; use POST /access to "
                    "grant roles. Repair applies only to a binding-less project."
                ),
            )

        binding = UserAccessBinding(
            user_identity=user_identity,
            role="admin",
            project_id=project_id,
            model_id=None,
            source="manual",
        )
        db.add(binding)
        await db.flush()
        await audit_required(
            db, action="access.repair_admin_binding", severity="warn",
            actor_email=current_user.email,
            target_type="access_binding",
            target_id=binding.id,
            detail={
                "user_identity": user_identity,
                "role": "admin",
                "project_id": str(project_id),
                "reason": "F-021-04 legacy binding-less project repair",
            },
        )
        await emit_webhook(current_user.tenant_id, "access.granted", {
            "user_identity": user_identity,
            "role": "admin",
            "project_id": str(project_id),
        })
        await db.commit()
        await db.refresh(binding)
        return UserAccessBindingResponse.model_validate(binding)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.get("", response_model=list[UserAccessBindingResponse])
async def list_access(
    project_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> list[UserAccessBindingResponse]:
    """List all bindings for this project. Requires at least viewer access."""
    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(
            select(UserAccessBinding).where(
                UserAccessBinding.project_id == project_id
            ).order_by(UserAccessBinding.created_at)
        )
        return [
            UserAccessBindingResponse.model_validate(b)
            for b in result.scalars().all()
        ]


@router.delete(
    "/{binding_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("admin")],
)
async def revoke_access(
    project_id: UUID,
    binding_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    """Revoke an access binding."""
    async for db in get_tenant_db(current_user.tenant_id):
        binding = await db.get(UserAccessBinding, binding_id)
        if binding is None or binding.project_id != project_id:
            raise HTTPException(status_code=404, detail="Binding not found")
        await audit_required(
            db, action="access.revoke", severity="warn",
            actor_email=current_user.email,
            target_type="access_binding",
            target_id=binding.id,
            detail={
                "user_identity": binding.user_identity,
                "role": binding.role,
                "project_id": str(project_id),
            },
        )
        await emit_webhook(current_user.tenant_id, "access.revoked", {
            "user_identity": binding.user_identity,
            "role": binding.role,
            "project_id": str(project_id),
        })
        await db.delete(binding)
        await db.commit()
