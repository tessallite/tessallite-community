"""FastAPI dependencies for JWT auth, shared across every Tessallite service.

Exposes:
    CurrentUser            — lightweight dataclass carrying claims off the JWT.
    get_current_user       — dependency: requires any valid JWT.
    require_system_admin   — dependency: requires role=system_admin on the JWT.
    require_tenant_admin   — dependency: requires system_admin OR tenant_admin.

Token extraction priority:
    1. ``Authorization: Bearer <token>`` header (inter-service, API tools)
    2. ``access_token`` httpOnly cookie (browser SPA)

``services/model-service/src/auth/middleware.py`` is a thin re-export for
backwards compatibility with existing model-service imports.
"""
from __future__ import annotations

import logging
from collections.abc import Collection

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import func, select

from shared.auth.jwt import decode_access_token
from shared.auth.service_principal import SERVICE_AUDIENCE, validate_service_payload
from shared.db.models import LocalUser
from shared.db.session import get_tenant_db

logger = logging.getLogger(__name__)


def extract_token_optional(request: Request) -> str | None:
    """Return the raw JWT from Authorization Bearer or cookie, or None."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        return token or None
    cookie_token = request.cookies.get("access_token")
    if cookie_token:
        return cookie_token
    return None


def _extract_token(request: Request) -> str:
    """Return the raw JWT string from header or cookie, or raise 401."""
    token = extract_token_optional(request)
    if token:
        return token
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


class CurrentUser:
    """Lightweight view of the JWT payload used by every request handler."""

    is_embed: bool = False

    def __init__(
        self,
        user_id: str,
        tenant_id: str,
        email: str,
        role: str | None = None,
        groups: list[str] | None = None,
        claims: dict | None = None,
        roles: list[str] | None = None,
        raw_token: str = "",
    ):
        self.user_id = user_id
        self.tenant_id = tenant_id
        self.email = email
        self.role = role
        # Bug-8017 / F-007-03: the multi-valued named-role subject for
        # row-level-security OR-of-grants. ``role`` stays the SINGLE RBAC tier
        # every authorization guard keys off; ``roles`` is ADDITIVE and used
        # ONLY by the row-security principal adapter so a caller entitled to
        # several named RLS roles sees the UNION of their row access. Populated
        # from the JWT ``roles`` claim; empty for legacy tokens (no claim), in
        # which case the principal adapter falls back to ``{role}`` — identical
        # to pre-fix behaviour.
        self.roles: list[str] = roles or []
        self.groups: list[str] = groups or []
        # Arbitrary IdP claims (SAML attributes / OIDC claims + scopes)
        # carried on the JWT for row-security attribute matching (F-007-02).
        self.claims: dict = claims or {}
        self.raw_token = raw_token


# Sentinel role for an embed session that carries NO admin-authored RLS role.
# It is NOT a real IdP role: it exists so RBAC guards that must reject embed
# tokens can key off ``is_embed`` (they never inspect the role string), while the
# row-security principal adapter treats it as "no RLS role" — a bare embed token
# must yield an empty role set so a role-governed model fails closed (deny-all)
# rather than spuriously matching a rule that literally targets the word "embed".
EMBED_ROLE_SENTINEL = "embed"


class CurrentEmbedUser(CurrentUser):
    """Extended context for embed-token sessions.

    Bug-7995 / F-024-01: an embed token may now carry an admin-authored
    row-security subject (``rls_role`` / ``groups`` / ``claims``). These are
    surfaced under the SAME attribute names the row-security principal adapter
    reads (``role`` / ``groups`` / ``claims``) so an embedded principal is built
    identically to an interactive one. When no RLS role is present, ``role`` is
    the ``EMBED_ROLE_SENTINEL`` so existing embed-vs-human RBAC guards (which key
    off ``is_embed``) are unaffected and the principal adapter maps the sentinel
    to an empty role set (fail closed on role-governed models).
    """

    is_embed: bool = True

    def __init__(
        self,
        user_id: str,
        tenant_id: str,
        email: str,
        persona_id: str | None = None,
        project_persona_id: str | None = None,
        project_ids: list[str] | None = None,
        model_ids: list[str] | None = None,
        capabilities: list[str] | None = None,
        rls_role: str | None = None,
        groups: list[str] | None = None,
        claims: dict | None = None,
    ):
        super().__init__(
            user_id=user_id, tenant_id=tenant_id, email=email,
            role=rls_role or EMBED_ROLE_SENTINEL,
            groups=groups,
            claims=claims,
            # Bug-8017: an embed session carries at most one admin-authored RLS
            # role. Surface it as the single-element ``roles`` subject when a real
            # rls_role is present; a bare embed token (sentinel role, no RLS role)
            # carries NO named role so a role-governed model still fails closed.
            roles=[rls_role] if rls_role else [],
        )
        self.persona_id = persona_id
        # ``persona_id`` is the model-service/query-router Persona claim.
        # Agent-service ProjectPersona rows live in a separate namespace and
        # must never be inferred from or aliased to that claim.
        self.project_persona_id = project_persona_id
        self.project_ids = project_ids
        self.model_ids = model_ids
        # F-021-08: omit/None means deny-all, not the historical all-capabilities
        # default. An embed session must opt in to query/chat/explore explicitly.
        self.capabilities: list[str] = capabilities if capabilities is not None else []


class CurrentServiceUser(CurrentUser):
    """Typed internal service-principal context."""

    is_service: bool = True

    def __init__(
        self, *, principal: str, tenant_id: str, role: str, scopes: list[str]
    ):
        super().__init__(
            user_id=f"service:{principal}",
            tenant_id=tenant_id,
            email=f"service:{principal}",
            role=role,
            # Bug-8017: a service principal carries exactly one role; surface it
            # as the single-element RLS ``roles`` subject for consistency with the
            # human path. Service tokens do not carry a multi-valued role grant.
            roles=[role] if role else [],
        )
        self.service_principal = principal
        self.service_scopes = scopes


def _build_user_from_payload(payload: dict) -> CurrentUser:
    """Route JWT payload to the correct user class based on audience."""
    sub = payload.get("sub")
    tenant_id = payload.get("tenant_id")
    if not sub or not tenant_id:
        raise ValueError("missing sub or tenant_id")

    if payload.get("aud") == "embed":
        raw_caps = payload.get("capabilities")
        if raw_caps is not None and not isinstance(raw_caps, list):
            raise ValueError("capabilities must be a list")
        caps = raw_caps if isinstance(raw_caps, list) else []
        raw_projects = payload.get("project_ids")
        project_ids = [str(p).lower() for p in raw_projects] if isinstance(raw_projects, list) else None
        raw_models = payload.get("model_ids")
        model_ids = [str(m).lower() for m in raw_models] if isinstance(raw_models, list) else None
        raw_project_persona = payload.get("project_persona_id")
        if raw_project_persona is not None and not isinstance(raw_project_persona, str):
            raise ValueError("project_persona_id must be a string")
        # Bug-7995 / F-024-01: read the admin-authored row-security subject from
        # the signed embed token. Same claim names as an interactive token so the
        # principal adapter builds an embed principal identically. A missing/blank
        # role yields the sentinel (no RLS role -> fail closed on role-governed
        # models); groups/claims default empty.
        raw_role = payload.get("role")
        rls_role = str(raw_role) if isinstance(raw_role, str) and raw_role.strip() else None
        raw_groups = payload.get("groups")
        groups = [str(g) for g in raw_groups] if isinstance(raw_groups, list) else None
        raw_claims = payload.get("claims")
        claims = dict(raw_claims) if isinstance(raw_claims, dict) else None
        return CurrentEmbedUser(
            user_id=sub,
            tenant_id=tenant_id,
            email=sub,
            persona_id=payload.get("persona_id"),
            project_persona_id=raw_project_persona,
            project_ids=project_ids,
            model_ids=model_ids,
            capabilities=caps,
            rls_role=rls_role,
            groups=groups,
            claims=claims,
        )

    if payload.get("aud") == SERVICE_AUDIENCE:
        principal, service_tenant_id, service_role, scopes = validate_service_payload(payload)
        return CurrentServiceUser(
            principal=principal,
            tenant_id=service_tenant_id,
            role=service_role,
            scopes=scopes,
        )

    raw_groups = payload.get("groups")
    groups = raw_groups if isinstance(raw_groups, list) else []
    raw_claims = payload.get("claims")
    claims = raw_claims if isinstance(raw_claims, dict) else {}
    # Bug-8017 / F-007-03: the multi-valued named-role subject for row-security.
    # Only a well-formed list of role strings is honoured; a legacy token with no
    # ``roles`` claim yields [] so the principal adapter falls back to {role}
    # (identical to pre-fix single-role behaviour, never a crash or widening).
    raw_roles = payload.get("roles")
    roles = (
        [str(r) for r in raw_roles if isinstance(r, str) and r]
        if isinstance(raw_roles, list)
        else []
    )
    return CurrentUser(
        user_id=sub,
        tenant_id=tenant_id,
        email=sub,
        role=payload.get("role"),
        groups=groups,
        claims=claims,
        roles=roles,
    )


async def get_current_user(request: Request) -> CurrentUser:
    """Require any valid JWT. Returns the decoded user context."""
    exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    token = _extract_token(request)
    try:
        payload = decode_access_token(token)
    except Exception:
        raise exc

    try:
        user = _build_user_from_payload(payload)
    except ValueError:
        raise exc

    if isinstance(user, CurrentEmbedUser):
        jti = payload.get("jti")
        if jti:
            from shared.auth.embed_revocation import is_embed_token_revoked
            try:
                # Bug-6306 / Bug-6352: scope the lookup to the token's own
                # tenant so a revocation recorded by a DIFFERENT tenant
                # cannot silence this token.
                revoked = await is_embed_token_revoked(str(jti), user.tenant_id)
            except Exception:
                # Bug-1033 defensive guard: if the revocation lookup itself
                # fails (missing tess_system.revoked_embed_tokens table, DB
                # outage), fail CLOSED with a clean 403 instead of a 500 —
                # an embed token whose revocation status cannot be verified
                # must not be honoured.
                logger.exception(
                    "Embed token revocation lookup failed — rejecting token (fail closed)"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Embed token revocation status could not be verified",
                )
            if revoked:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Token has been revoked",
                    headers={"WWW-Authenticate": "Bearer"},
                )
    elif not isinstance(user, CurrentServiceUser):
        await _validate_regular_session(user, payload, exc)

    user.raw_token = token
    return user


async def _validate_regular_session(
    user: CurrentUser,
    payload: dict,
    auth_exc: HTTPException,
) -> None:
    """Re-read tenant-local account state for regular JWTs.

    Regular sessions must not outlive account deactivation, deletion, password
    reset, or tenant-wide role demotion. Embed tokens keep their existing jti
    revocation path; canonical human system-admin tokens use the system tenant
    and have no tenant-local LocalUser row.
    """
    if user.role == "system_admin" and user.tenant_id == "__system__":
        await _validate_system_admin_token_version(payload, auth_exc)
        return
    if user.role == "system_admin" or user.tenant_id == "__system__":
        raise auth_exc
    if user.role is None:
        # No legitimate production token lacks a role claim. Reject rather
        # than silently skipping DB checks (review finding R1-F1).
        raise auth_exc
    try:
        async for db in get_tenant_db(user.tenant_id):
            result = await db.execute(
                select(LocalUser).where(func.lower(LocalUser.email) == user.user_id.lower())
            )
            local_user = result.scalar_one_or_none()
            if local_user is None or not bool(local_user.is_active):
                raise auth_exc
            if local_user.role != user.role:
                raise auth_exc
            local_version = int(getattr(local_user, "token_version", 0) or 0)
            raw_token_version = payload.get("token_version")
            if raw_token_version is None:
                # Compatibility: pre-migration regular tokens did not carry a
                # version claim. Honour them only while the account is still at
                # the initial version; the first invalidation bumps the durable
                # version and retires all legacy tokens.
                if local_version != 0:
                    raise auth_exc
            else:
                try:
                    token_version = int(raw_token_version)
                except (TypeError, ValueError):
                    raise auth_exc
                if token_version != local_version:
                    raise auth_exc
            return
    except HTTPException:
        raise
    except Exception:
        logger.warning("Regular session validation failed; rejecting token")
        raise auth_exc
    raise auth_exc


SYSTEM_ADMIN_TOKEN_VERSION_KEY = "system_admin.token_version"


async def get_system_admin_token_version() -> int:
    """Durable system-admin JWT version stored in tess_system.system_settings.

    Bug-9552: ``system_settings`` is migration 0016, so a fresh system (or
    one whose schema is behind it) cannot read it during bootstrap login.
    Missing-table errors fall back to the default version 0; any other
    error still raises.
    """
    from sqlalchemy.exc import ProgrammingError

    from shared.db.models import SystemSetting
    from shared.db.pre_migration import is_missing_table_error
    from shared.db.session import get_system_db

    async for db in get_system_db():
        try:
            result = await db.execute(
                select(SystemSetting.value_json).where(
                    SystemSetting.key == SYSTEM_ADMIN_TOKEN_VERSION_KEY
                )
            )
        except ProgrammingError as exc:
            if is_missing_table_error(exc):
                await db.rollback()
                return 0  # pre-migration: no persisted version exists yet
            raise
        raw = result.scalar_one_or_none()
        if raw is None:
            return 0
        if isinstance(raw, int):
            return int(raw)
        if isinstance(raw, dict) and "value" in raw:
            try:
                return int(raw["value"])
            except (TypeError, ValueError):
                return 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0
    return 0


async def bump_system_admin_token_version() -> int:
    """Increment the system-admin token version (logout / credential rotate)."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from shared.db.models import SystemSetting
    from shared.db.session import get_system_db

    current = await get_system_admin_token_version()
    new_version = current + 1
    async for db in get_system_db():
        stmt = (
            pg_insert(SystemSetting)
            .values(
                key=SYSTEM_ADMIN_TOKEN_VERSION_KEY,
                value_json=new_version,
                updated_by="logout",
            )
            .on_conflict_do_update(
                index_elements=[SystemSetting.key],
                set_={"value_json": new_version, "updated_by": "logout"},
            )
        )
        await db.execute(stmt)
        await db.commit()
    return new_version


async def _validate_system_admin_token_version(
    payload: dict,
    auth_exc: HTTPException,
) -> None:
    try:
        local_version = await get_system_admin_token_version()
    except Exception:
        logger.warning("System-admin token_version lookup failed; rejecting token")
        raise auth_exc
    raw_token_version = payload.get("token_version")
    if raw_token_version is None:
        if local_version != 0:
            raise auth_exc
        return
    try:
        token_version = int(raw_token_version)
    except (TypeError, ValueError):
        raise auth_exc
    if token_version != local_version:
        raise auth_exc


async def require_system_admin(
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Require a canonical human system-admin session."""
    exc_forbidden = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="System admin access required",
    )
    if isinstance(current_user, (CurrentServiceUser, CurrentEmbedUser)):
        raise exc_forbidden
    if current_user.role != "system_admin" or current_user.tenant_id != "__system__":
        raise exc_forbidden
    return current_user


def is_canonical_human_system_admin(current_user: CurrentUser) -> bool:
    return (
        not isinstance(current_user, (CurrentServiceUser, CurrentEmbedUser))
        and current_user.role == "system_admin"
        and current_user.tenant_id == "__system__"
    )


def is_human_tenant_admin(current_user: CurrentUser) -> bool:
    return (
        not isinstance(current_user, (CurrentServiceUser, CurrentEmbedUser))
        and current_user.role == "tenant_admin"
    )


def is_human_tenant_admin_or_system_admin(current_user: CurrentUser) -> bool:
    return is_human_tenant_admin(current_user) or is_canonical_human_system_admin(
        current_user
    )


async def require_tenant_admin(
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Allow only human tenant admins or canonical human system admins."""
    if is_human_tenant_admin_or_system_admin(current_user):
        return current_user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Tenant admin access required",
    )


def require_service_scope(scope: str):
    """Allow ONLY a typed internal service token carrying *scope*.

    Strictly narrower than :func:`require_service_scope_or_tenant_admin`: no
    human principal passes, not even a tenant or system admin. Used by internal
    service-to-service entry points that have no user-facing meaning — a human
    reaching one would be operating machinery the UI never exposes (Bug-8034
    durable advisor dispatch is the first such pair).

    ``get_current_user`` already rejects a missing or unverifiable token with
    401, so an unsigned caller never reaches this check.
    """

    async def _check(
        current_user: CurrentUser = Depends(get_current_user),
    ) -> CurrentUser:
        if not isinstance(current_user, CurrentServiceUser):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Internal service token required",
            )
        if scope not in current_user.service_scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Service token scope required",
            )
        return current_user

    return _check


def require_service_scope_or_tenant_admin(scope: str):
    """Allow a human tenant/system admin or a typed service token with scope."""

    async def _check(
        current_user: CurrentUser = Depends(get_current_user),
    ) -> CurrentUser:
        if isinstance(current_user, CurrentServiceUser):
            if scope in current_user.service_scopes:
                return current_user
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Service token scope required",
            )
        if is_human_tenant_admin_or_system_admin(current_user):
            return current_user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant admin access required",
        )

    return _check


def require_service_scope_or_system_admin(scope: str):
    """Allow a human system admin or a typed service token with scope."""

    async def _check(
        current_user: CurrentUser = Depends(get_current_user),
    ) -> CurrentUser:
        if isinstance(current_user, CurrentServiceUser):
            if scope in current_user.service_scopes:
                return current_user
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Service token scope required",
            )
        if is_canonical_human_system_admin(current_user):
            return current_user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="System admin access required",
        )

    return _check


def require_capability_or_service_scope(capability: str, scope: str):
    """Allow normal capability checks or a typed service token with scope."""

    async def _check(
        current_user: CurrentUser = Depends(get_current_user),
    ) -> CurrentUser:
        if isinstance(current_user, CurrentServiceUser):
            if scope in current_user.service_scopes:
                return current_user
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Service token scope required",
            )
        if isinstance(current_user, CurrentEmbedUser):
            if capability not in current_user.capabilities:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Embed token does not grant '{capability}' capability",
                )
        return current_user

    _check.__wrapped_capability__ = capability
    return _check


def require_capability_or_service_scopes(
    capability: str, scopes: Collection[str],
):
    """Allow a capability check or a typed service token with any *scopes*.

    This is intentionally separate from
    :func:`require_capability_or_service_scope`: existing routes retain their
    one-scope contract, while a boundary that deliberately admits more than
    one typed internal caller can state that allowlist explicitly. The service
    principal must still carry one of the listed scopes; human and embed
    behaviour is identical to the singular dependency.
    """
    allowed_scopes = frozenset(scopes)
    if not allowed_scopes:
        raise ValueError("at least one service scope is required")

    async def _check(
        current_user: CurrentUser = Depends(get_current_user),
    ) -> CurrentUser:
        if isinstance(current_user, CurrentServiceUser):
            if allowed_scopes.intersection(current_user.service_scopes):
                return current_user
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Service token scope required",
            )
        if isinstance(current_user, CurrentEmbedUser):
            if capability not in current_user.capabilities:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Embed token does not grant '{capability}' capability",
                )
        return current_user

    _check.__wrapped_capability__ = capability
    _check.__wrapped_service_scopes__ = allowed_scopes
    return _check


def require_service_scope_or_non_embed(scope: str):
    """Allow a scoped service token or any non-embed human user."""

    async def _check(
        current_user: CurrentUser = Depends(get_current_user),
    ) -> CurrentUser:
        if isinstance(current_user, CurrentServiceUser):
            if scope in current_user.service_scopes:
                return current_user
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Service token scope required",
            )
        if isinstance(current_user, CurrentEmbedUser):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Embed tokens cannot access management APIs",
            )
        return current_user

    return _check


def require_capability(capability: str):
    """FastAPI dependency factory that checks embed token capabilities.

    For non-embed users (regular login), all capabilities are allowed.
    For embed users, the capability must be in the token's capabilities list.
    """
    async def _check(
        current_user: CurrentUser = Depends(get_current_user),
    ) -> CurrentUser:
        if isinstance(current_user, CurrentEmbedUser):
            if capability not in current_user.capabilities:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Embed token does not grant '{capability}' capability",
                )
        return current_user
    _check.__wrapped_capability__ = capability
    return _check


def enforce_model_scope(
    current_user: CurrentUser, model_id: str, *, project_id: str | None = None,
) -> None:
    """Enforce an embed token's model (and, when given, project) scope.

    F-021-02 / Bug-7992: a project-scoped embed token
    (``project_ids=[P1], model_ids=null``) must NOT be able to read another
    project's model metadata. Checking only ``model_ids`` is a no-op when that
    claim is null — so on the generic model-service viewer routes the token's
    ``project_ids`` went unenforced and the token could cross its project scope.

    ``project_id`` is a keyword argument that the model-service metadata routes
    now always supply (they carry it in the path), so the project half of the
    scope is enforced there too. The query-router callers omit it because they
    immediately funnel through ``ensure_project_model_access`` /
    ``load_authorized_model``, which already enforces the token's ``project_ids``
    against the model's resolved project — passing it here as well would be
    redundant, not a gap. Non-embed users are never restricted by this helper;
    their access is decided by RBAC bindings.
    """
    if not isinstance(current_user, CurrentEmbedUser):
        return
    if project_id is not None and current_user.project_ids is not None:
        if str(project_id).lower() not in current_user.project_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Project not in embed token scope",
            )
    if current_user.model_ids is not None:
        if str(model_id).lower() not in current_user.model_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Model not in embed token scope",
            )


async def forbid_embed_user(
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Reject embed tokens outright — for management/admin APIs."""
    if isinstance(current_user, CurrentEmbedUser):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Embed tokens cannot access management APIs",
        )
    return current_user


async def require_human_user(
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Require an interactive human session; reject non-human token types.

    Generic authenticated *read* routes that back human-facing UI surfaces
    (the project tree, the tenant badge, edition/licensing metadata) only
    required ``get_current_user``/``forbid_embed_user``. Both admit typed
    ``CurrentServiceUser`` principals, so a leaked or misused scoped service
    token (e.g. the KPI-evaluate or pocket-refresh token) could enumerate
    projects, tenant info, and licensing metadata it has no business scope for
    (AUTH-RR-05 / Bug-7776).

    Internal service principals are strictly scope-based (see
    ``shared/auth/service_principal.py``) and never legitimately call these
    human discovery endpoints — they use their own scoped routes. This
    dependency therefore fails closed for both service and embed tokens, and
    admits only regular human sessions (including tenant/system admins).
    """
    if isinstance(current_user, (CurrentServiceUser, CurrentEmbedUser)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint is available to interactive users only",
        )
    return current_user


async def forbid_service_user(
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Reject typed service principals while still admitting embed sessions.

    For human-facing read routes that legitimately serve embed dashboards
    (e.g. the scoped project list) but must NOT be reachable by internal
    scoped service tokens (AUTH-RR-05 / Bug-7776). Embed tokens carry their
    own project/model scope enforcement in the handler; service principals
    have no business enumerating this human discovery surface.
    """
    if isinstance(current_user, CurrentServiceUser):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Service tokens cannot access this endpoint",
        )
    return current_user
