"""JIT user adoption and IdP group-to-role mapping.

Shared by the credential-based login endpoint and redirect-based SSO
callbacks (SAML ACS, OIDC callback).
"""
from __future__ import annotations

import logging
import secrets

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.audit.logger import audit
from shared.auth.backend import UserIdentity
from shared.auth.identity import canonical_user_identity, user_identity_matches
from shared.auth.roles import PROJECT_ROLE_HIERARCHY, normalize_jit_default_role
from shared.db.models import (
    IdpGroupRoleMapping,
    LocalUser,
    ProjectSetting,
    UserAccessBinding,
)
from src.auth.local_backend import hash_password
from src.auth.tenant_admin_guard import other_active_tenant_admin_exists
from src.auth.token_version import bump_local_user_token_version
from src.licensing_guard import enforce_create_cap

logger = logging.getLogger(__name__)

# Group-mapping roles that materialise as a project ``UserAccessBinding``.
# ``model_technical`` is an audience role carried on ``LocalUser.role`` (the
# persona resolver consumes it); it is not a project RBAC tier, so it never
# becomes a binding.
_BINDING_ROLES = frozenset({"admin", "modeler", "viewer"})


def is_external_identity(identity: UserIdentity) -> bool:
    # "pat" (Bug-7314) is NOT an external identity: a Personal Access Token is
    # validated against — and only ever resolves to — an EXISTING local_users
    # row (validate_pat), so it must follow the internal look-up-by-email path,
    # never JIT-adoption or IdP-group admission. Treating it as external would
    # both spuriously JIT-provision and, worse, run the external-admission gate
    # against a token whose owner already exists.
    return identity.source_backend not in ("", "local", "pat")


def _identity_links_to_account(identity: UserIdentity, existing: LocalUser) -> bool:
    """F-021-01: does this external identity legitimately map to ``existing``?

    An external IdP identity may only resolve to a local_users row that was
    itself created by (or already linked to) the SAME external provider. Email
    is a display/login label, NOT a cross-provider account-link key — treating
    it as one lets anyone who can authenticate an IdP identity with a victim's
    email silently inherit that victim's account and privileges.

    Link is allowed only when the stored ``auth_source`` equals the incoming
    ``source_backend`` (same provider adopting its own returning user). A
    ``local`` account, or an account provisioned by a DIFFERENT external
    provider, is never auto-linked; that requires an explicit, deliberate
    account-link operation which this product does not yet expose, so we fail
    closed.
    """
    stored = (getattr(existing, "auth_source", "") or "").strip().lower()
    incoming = (identity.source_backend or "").strip().lower()
    return bool(stored) and stored == incoming


async def external_identity_admitted(db: AsyncSession, identity: UserIdentity) -> bool:
    """Return whether an external identity may be admitted to this tenant."""
    email = identity.email.strip().lower()
    existing = (
        await db.execute(
            select(LocalUser).where(
                func.lower(LocalUser.email) == email,
                LocalUser.is_active == True,  # noqa: E712
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # F-021-01: an active email match is only an admission ground when the
        # match belongs to the SAME provider. A ``local`` account or a
        # different external provider's account must not be adopted via email
        # alone — fall through to the group-mapping admission path instead of
        # returning True on the strength of a shared email.
        if _identity_links_to_account(identity, existing):
            return True
    groups = [group for group in (identity.groups or []) if group]
    if not groups:
        return False
    mapped = (
        await db.execute(
            select(IdpGroupRoleMapping.id)
            .where(IdpGroupRoleMapping.idp_group_name.in_(groups))
            .limit(1)
        )
    ).first()
    return mapped is not None


async def require_external_identity_admitted(
    db: AsyncSession, identity: UserIdentity
) -> None:
    if not is_external_identity(identity):
        return
    if await external_identity_admitted(db, identity):
        return
    from fastapi import HTTPException, status

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="External identity is not admitted to this tenant",
    )


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
    groups_claim_present: bool = False,
) -> None:
    """F-021-03 / Bug-6303: reconcile the user's SSO group-derived project
    ``UserAccessBinding`` rows against the IdP group-role mappings that match
    the user's *current* groups.

    Without the materialise half, a project-scoped ``IdpGroupRoleMapping`` was
    stored and managed via the API but never consulted — an admin who mapped
    ``analysts -> modeler`` on a project granted those SSO users no access.
    Each matching project-scoped mapping becomes (or updates) the user's
    ``source="sso_group"`` binding on that project, with the highest mapped role
    winning per project (matching ``resolve_group_role``'s rank order).

    Bug-6303 (revocation): every ``sso_group`` binding whose project is no
    longer matched by a current IdP group mapping is REVOKED here, so removing a
    user from an IdP group (de-provisioning) actually withdraws the privilege on
    their next login instead of it lingering forever.

    Invariants (SECURITY — fail-closed):
    - ``source="manual"`` bindings are NEVER touched: not created, not
      role-changed, not revoked. Manual grants (access API, project import, and
      every pre-existing row via the ``manual`` server_default) are the
      operator's explicit intent and outrank the SSO group sync. If a manual
      binding already occupies a mapped project scope, it is left as-is (the
      user already has access) and no duplicate ``sso_group`` row is created.
    - F-021-04: distinguish an ABSENT groups claim from a PRESENT-but-empty one.
      When ``groups_claim_present`` is False the IdP did not return the groups
      claim at all — that is indeterminate, so the function returns early WITHOUT
      revoking anything (an IdP that omits group claims must not wipe SSO grants).
      When ``groups_claim_present`` is True the IdP authoritatively returned the
      groups it stands behind (possibly empty) — a present-empty set is a real
      de-provisioning signal and DOES drive revocation of every ``sso_group``
      binding, because the user's current groups map to no project.

    Tenant-wide mappings (``project_id IS NULL``) set the cosmetic
    ``LocalUser.role`` and, for ``admin``, elevate to ``tenant_admin`` (see
    ``jit_adopt_user``); they do not fan out a binding to every project and are
    out of scope here.
    """
    # F-021-04: only an ABSENT groups claim is indeterminate. A non-empty group
    # set, or a present-but-empty claim, is authoritative and reconciled below
    # (a present-empty claim revokes every sso_group binding). A claim that is
    # both absent and empty (legacy callers, no presence signal) still short-
    # circuits — fail closed, never revoke on an undetermined set.
    if not groups and not groups_claim_present:
        return
    user_identity = canonical_user_identity(user_identity)

    result = await db.execute(
        select(IdpGroupRoleMapping).where(
            IdpGroupRoleMapping.idp_group_name.in_(groups),
            IdpGroupRoleMapping.project_id.is_not(None),
        )
    )
    mappings = result.scalars().all()

    # Highest-ranked binding role per project the user's CURRENT groups map to.
    # May be empty (the user's groups no longer map to any project) — that is an
    # authoritative "no SSO grants" and DOES drive revocation below. We do NOT
    # early-return on an empty mapping set; that was the original defect
    # (de-provisioned users kept stale bindings).
    best_per_project: dict[str, str] = {}
    for m in mappings:
        if m.role not in _BINDING_ROLES:
            continue
        pid = str(m.project_id)
        current = best_per_project.get(pid)
        if current is None or _ROLE_RANK.get(m.role, 0) > _ROLE_RANK.get(current, 0):
            best_per_project[pid] = m.role

    # Materialise / refresh sso_group bindings for currently-mapped projects.
    for pid, role in best_per_project.items():
        existing = await db.execute(
            select(UserAccessBinding).where(
                user_identity_matches(UserAccessBinding.user_identity, user_identity),
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
                source="sso_group",
            ))
        elif binding.source == "sso_group":
            # Only SSO-managed bindings are updated by the sync. A manual
            # binding on the same scope is left exactly as the operator set it.
            if binding.role != role:
                binding.role = role

    # Revoke stale SSO grants: any sso_group binding whose project is no longer
    # matched by the user's current groups. Manual bindings are excluded by the
    # source filter, so they can never be revoked here.
    result = await db.execute(
        select(UserAccessBinding).where(
            user_identity_matches(UserAccessBinding.user_identity, user_identity),
            UserAccessBinding.source == "sso_group",
        )
    )
    for binding in result.scalars().all():
        if str(binding.project_id) not in best_per_project:
            await db.delete(binding)


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


async def _audit_sso_reconcile(
    db: AsyncSession,
    local_user: LocalUser,
    outcome: str,
    *,
    from_role: str,
    to_role: str,
) -> None:
    """Bug-6641: record an automatic SSO tenant-role reconciliation to the audit
    table (not just the app log), so an operator can see WHY a user's admin power
    changed at login time. ``outcome`` is one of ``elevate`` / ``revoke`` /
    ``revoke_refused_last_admin``. Best-effort: ``audit`` swallows its own
    failures, so a logging fault never blocks the login/reconcile."""
    severity = "warn" if outcome == "elevate" else "critical"
    await audit(
        db,
        action="user.sso_role_reconcile",
        severity=severity,
        actor_id=getattr(local_user, "id", None),
        actor_email=getattr(local_user, "email", None),
        target_type="user",
        target_id=getattr(local_user, "id", None),
        target_name=getattr(local_user, "email", None),
        detail={
            "outcome": outcome,
            "from_role": from_role,
            "to_role": to_role,
            "reason": "idp_group_membership_change",
        },
    )


async def _reconcile_sso_tenant_role(
    db: AsyncSession,
    local_user: LocalUser,
    mapped_tenant_role: str | None,
    tenant_group_role: str | None,
    groups: list[str],
    groups_claim_present: bool = False,
) -> None:
    """Reconcile a returning user's tenant-wide ``LocalUser.role`` against the
    tenant-wide role their CURRENT IdP groups map to.

    Three cases:

    1. Current groups grant ``tenant_admin`` (an ``admin`` tenant-wide mapping):
       elevate the user if they are not already admin, stamping
       ``role_source="sso"`` so the grant is later revocable. An existing
       ``tenant_admin`` (manual OR sso) keeps its role and provenance; a
       ``system_admin`` is never touched.

    2. Bug-6597 demote-on-deprovision — current groups do NOT grant
       ``tenant_admin`` and the user is an SSO-elevated ``tenant_admin``
       (``role_source="sso"``): reconcile DOWN to the role a fresh SSO user with
       these groups would receive. A MANUALLY-promoted admin
       (``role_source="manual"``) is never auto-demoted. Guarded by the
       last-admin invariant: if no other active tenant_admin exists, the demotion
       is refused and the admin is kept, so IdP de-provisioning can never lock a
       tenant out of administration.

    3. Non-admin cosmetic sync — a tenant-wide non-admin mapping (e.g.
       ``model_technical``) refreshes the cosmetic ``LocalUser.role`` for a
       non-admin user, mirroring the pre-existing behaviour.

    Fail-closed on group claims (F-021-04): an ABSENT groups claim
    (``groups_claim_present`` False) is indeterminate — an IdP may simply omit
    group claims — so it never drives a demotion, mirroring
    ``_sync_group_bindings``. A groups claim that is PRESENT but no longer maps
    to ``admin`` (including a present-empty set) is an authoritative
    de-provisioning signal and DOES drive the Bug-6597 demotion (still guarded
    by the last-admin invariant).
    """
    # Case 1: current groups grant tenant_admin.
    if mapped_tenant_role == "tenant_admin":
        if local_user.role not in ("tenant_admin", "system_admin"):
            prior_role = local_user.role
            local_user.role = "tenant_admin"
            local_user.role_source = "sso"
            await bump_local_user_token_version(db, local_user)
            # Bug-6641: automatic privilege *elevation* is governance-relevant.
            await _audit_sso_reconcile(
                db, local_user, "elevate",
                from_role=prior_role, to_role="tenant_admin",
            )
        return

    # Case 2: SSO-elevated admin whose admin group is now GONE — demote down.
    # F-021-04: an authoritative groups claim (non-empty, OR present-but-empty)
    # that no longer grants admin drives the demotion. An ABSENT claim
    # (indeterminate) never does — fail closed.
    if (
        (groups or groups_claim_present)
        and local_user.role == "tenant_admin"
        and getattr(local_user, "role_source", "manual") == "sso"
    ):
        # Last-admin guard: never leave the tenant with zero active admins.
        if not await other_active_tenant_admin_exists(db, local_user.id):
            logger.warning(
                "Bug-6597: refusing to demote SSO tenant_admin %s — it is the "
                "last active tenant administrator; a successor must be created "
                "first. Admin role retained.",
                local_user.email,
            )
            # Bug-6641: a refused revocation is a security event operators must
            # be able to see (the tenant is running on its last admin).
            await _audit_sso_reconcile(
                db, local_user, "revoke_refused_last_admin",
                from_role="tenant_admin", to_role="tenant_admin",
            )
            return
        base_role = (
            tenant_group_role or await resolve_jit_default_role(db)
        )
        logger.info(
            "Bug-6597: reconciling SSO-elevated tenant_admin %s down to %s "
            "(IdP admin group removed).",
            local_user.email, base_role,
        )
        local_user.role = base_role
        await bump_local_user_token_version(db, local_user)
        # role_source stays "sso": the reconciled role is still SSO-derived.
        # Bug-6641: record the automatic privilege revocation with a trail.
        await _audit_sso_reconcile(
            db, local_user, "revoke",
            from_role="tenant_admin", to_role=base_role,
        )
        return

    # Case 3: non-admin cosmetic role sync for non-admin/system users.
    if mapped_tenant_role and local_user.role != mapped_tenant_role:
        if local_user.role not in ("tenant_admin", "system_admin"):
            local_user.role = mapped_tenant_role
            local_user.role_source = "sso"
            await bump_local_user_token_version(db, local_user)


def _jit_sentinel_password() -> str:
    """Unusable random password for a JIT-created SSO/LDAP user.

    The user always authenticates via the IdP, never with this value. 48
    url-safe bytes render as 64 chars — deliberately under bcrypt's 72-byte
    hard limit (bcrypt >= 4.1 raises ValueError beyond it, which the login
    endpoint would swallow into an opaque 401 for every first-time SSO login).
    """
    return secrets.token_urlsafe(48)


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
        # Bug-6435: enforce the licensed users cap before JIT-creating a user.
        # This reuses the same cap-enforcement path as the manual user-create
        # endpoint (auth.py), so Community edition caps cannot be bypassed by
        # routing user creation through SSO/LDAP JIT adoption.
        async def _count_users() -> int:
            r = await db.execute(
                select(func.count()).select_from(LocalUser).where(
                    LocalUser.is_active == True  # noqa: E712
                )
            )
            return int(r.scalar() or 0)

        # Bug-6567: pass db so the count-then-create is serialised with an
        # advisory lock, preventing two concurrent JIT creates at cap-1.
        await enforce_create_cap("user", _count_users, db=db)

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
            hashed_password=hash_password(_jit_sentinel_password()),
            is_active=True,
            role=jit_role,
            # Bug-6597: the initial role is SSO-derived. If it is ``tenant_admin``
            # (a tenant-wide admin group), this provenance lets the reconcile path
            # later demote the user when that admin group disappears.
            role_source="sso",
            auth_source=identity.source_backend,
            token_version=0,
            has_completed_onboarding=False,
        )
        db.add(local_user)
        await db.flush()
        logger.info(
            "JIT adopted user %s via %s (tenant=%s, role=%s)",
            email, identity.source_backend, tenant_id, jit_role,
        )

        # Bug-6666: a new SSO user created with tenant_admin (via a
        # tenant-wide "admin" group mapping) must emit the same audit event
        # the returning-user reconciliation path records. Without this, an
        # admin-level SSO JIT provision leaves no audit trail — only a log
        # line — while a subsequent login (returning-user path) does record
        # it via _reconcile_sso_tenant_role.
        if jit_role == "tenant_admin":
            await _audit_sso_reconcile(
                db, local_user, "grant",
                from_role="(new_user)", to_role="tenant_admin",
            )
    else:
        # F-021-01: fail closed on a cross-provider account collision. An
        # EXTERNAL identity (SAML/OIDC/LDAP/gcp_iam) whose email matches an
        # existing local_users row created by a DIFFERENT source (a ``local``
        # credential account, or another external provider) must NOT silently
        # adopt that row and inherit its tenant/project privileges. Only a
        # same-provider returning user is allowed through; anything else is
        # refused with an audited 403 and no session is issued. ``pat``/local
        # identities are not external (is_external_identity) and skip this gate.
        if is_external_identity(identity) and not _identity_links_to_account(
            identity, local_user
        ):
            await audit(
                db,
                action="auth.sso_account_link_refused",
                severity="critical",
                actor_email=local_user.email,
                target_type="user",
                target_id=getattr(local_user, "id", None),
                target_name=local_user.email,
                detail={
                    "reason": "email_matches_account_with_different_auth_source",
                    "incoming_provider": identity.source_backend,
                    "existing_auth_source": getattr(
                        local_user, "auth_source", None
                    ),
                },
            )
            await db.commit()
            from fastapi import HTTPException, status

            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "An account with this email already exists and is not linked "
                    "to this identity provider. Account linking must be performed "
                    "explicitly by an administrator."
                ),
            )
        # Returning user: reconcile the tenant-wide role against the CURRENT
        # IdP groups (elevate, sync, or Bug-6597 demote-on-deprovision).
        await _reconcile_sso_tenant_role(
            db, local_user, mapped_tenant_role, tenant_group_role,
            identity.groups,
            groups_claim_present=getattr(identity, "groups_claim_present", False),
        )

    # Materialise project bindings from project-scoped mappings (F-021-03).
    # F-021-04: pass the groups-claim presence so a present-empty claim revokes
    # stale sso_group bindings (de-provisioning) while an absent claim retains.
    await _sync_group_bindings(
        db, email, identity.groups,
        groups_claim_present=getattr(identity, "groups_claim_present", False),
    )

    await db.commit()
    await db.refresh(local_user)
    return local_user, local_user.role
