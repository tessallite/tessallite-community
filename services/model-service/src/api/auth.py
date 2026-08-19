"""
Auth routes: system admin login, tenant login, user CRUD.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm.exc import StaleDataError

logger = logging.getLogger(__name__)

from shared.audit.logger import audit, audit_required
from shared.audit.system import system_audit
from shared.auth.backend import UserIdentity
from shared.auth.cookie import clear_auth_cookies, login_response
from shared.auth.lockout import (
    DISCOVER_SCOPE,
    SYSTEM_SCOPE,
    assert_not_locked,
    record_login_failure,
    record_login_success,
)
from shared.auth.identity import (
    canonical_email,
    canonical_user_identity,
    user_identity_matches,
)
from shared.config.bootstrap import system_snapshot_get
from shared.webhooks.dispatcher import emit_webhook_logged as emit_webhook
from shared.db.models import LocalUser, SystemTenant, UserAccessBinding
from shared.db.session import get_system_db, get_tenant_db
from src.licensing_guard import enforce_create_cap
from shared.schemas.pydantic_models import (
    LoginRequest,
    SystemLoginRequest,
    UserCreate,
    UserPasswordReset,
    UserResponse,
    UserUpdate,
)
from src.auth.chain import get_auth_chain
from src.auth.jit import (
    external_identity_admitted,
    is_external_identity,
    jit_adopt_user,
    require_external_identity_admitted,
)
from src.auth.tenant_admin_guard import (
    _UNSET as _UNSET_UPDATE,
    applying_change_orphans_tenant,
)
from src.auth.local_backend import (
    authenticate_system_admin,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from src.auth.token_version import bump_local_user_token_version
from src.auth.middleware import (
    CurrentUser,
    bump_system_admin_token_version,
    extract_token_optional,
    get_system_admin_token_version,
    is_canonical_human_system_admin,
    require_human_user,
    require_system_admin,
    require_tenant_admin,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _validate_password_length(password: str) -> None:
    """Bug-7326: reject passwords that exceed bcrypt's 72-byte input limit.

    Raises HTTPException(400) so the user gets a clear validation error
    instead of the opaque 500 that bcrypt raises on overlong input. Called
    at every user-create and password-reset entry point.
    """
    if len(password.encode("utf-8")) > 72:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Password is too long. The maximum supported length is "
                "72 bytes (UTF-8 encoded). Please choose a shorter password."
            ),
        )


# A precomputed valid bcrypt hash of a random string, used by login_discover to
# pay the bcrypt cost on a no-email-match so timing does not reveal whether the
# email exists in any tenant (F-021-07).
_DISCOVER_DUMMY_HASH = hash_password("tessallite-discover-dummy-no-match")

# Bug-6304: retain strong references to fire-and-forget discovery-login audit
# tasks so the event loop does not GC them before they run.
_DISCOVER_AUDIT_TASKS: set[asyncio.Task] = set()


async def _audit_discover_login(
    *,
    tenant_slug: str,
    action: str,
    severity: str,
    actor_id,
    actor_email: str | None,
    ip_address: str | None,
    detail: dict,
) -> None:
    """Write a discovery-login audit event in its own DB session (Bug-6304).

    Runs fire-and-forget so the ``/login/discover`` response latency does NOT
    depend on whether the email resolved to a tenant. Awaiting a tenant-DB
    write + commit on the matched-but-wrong-password path — while the
    unknown-email path does not — would reintroduce the cross-tenant user
    enumeration timing oracle F-021-07 was built to close. Errors are swallowed;
    the audit is best-effort on this path.
    """
    try:
        async for db in get_tenant_db(tenant_slug):
            await audit(
                db, action=action, severity=severity,
                actor_id=actor_id, actor_email=actor_email,
                ip_address=ip_address, detail=detail,
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — audit must never break or slow the auth response
        logger.warning(
            "login_discover audit write failed (tenant=%s action=%s)",
            tenant_slug, action, exc_info=True,
        )


def _schedule_discover_audit(**kwargs) -> None:
    """Schedule a discovery-login audit write without blocking the response."""
    task = asyncio.create_task(_audit_discover_login(**kwargs))
    _DISCOVER_AUDIT_TASKS.add(task)
    task.add_done_callback(_DISCOVER_AUDIT_TASKS.discard)


async def _noop_audit_task() -> None:
    """Bug-6813: a no-op coroutine that mirrors the cost of scheduling a real
    audit task. Scheduled on the unknown-email discovery-login path so the
    task-creation overhead is symmetric with the known-email failure path,
    eliminating the residual timing side-channel."""
    pass


def _schedule_noop_audit() -> None:
    """Bug-6813: schedule the no-op audit task (same bookkeeping as the real one)."""
    task = asyncio.create_task(_noop_audit_task())
    _DISCOVER_AUDIT_TASKS.add(task)
    task.add_done_callback(_DISCOVER_AUDIT_TASKS.discard)


def _jwt_max_age_seconds() -> int:
    return int(system_snapshot_get("auth.jwt_expire_minutes")) * 60


def _is_system_admin(user: CurrentUser) -> bool:
    return is_canonical_human_system_admin(user)


def _resolve_target_tenant_id(current_user: CurrentUser, tenant_id: str | None) -> str:
    if _is_system_admin(current_user):
        if not tenant_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="tenant_id is required for system admin user management",
            )
        return tenant_id

    if tenant_id and tenant_id != current_user.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot manage users for another tenant",
        )
    return current_user.tenant_id


_DISCOVER_TENANT_CAP = 32


@router.post("/system/login")
async def system_login(body: SystemLoginRequest, request: Request) -> Response:
    """
    Authenticate as system admin using infrastructure-level credentials.
    Sets an httpOnly JWT cookie and returns role in the body.
    """
    client_ip = request.client.host if request.client else None
    async for sys_db in get_system_db():
        await assert_not_locked(sys_db, SYSTEM_SCOPE, body.email)
        if not authenticate_system_admin(body.email, body.password):
            await record_login_failure(sys_db, SYSTEM_SCOPE, body.email)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid system admin credentials",
            )
        await record_login_success(sys_db, SYSTEM_SCOPE, body.email)
        token_version = await get_system_admin_token_version()
        token = create_access_token(
            sub=canonical_email(body.email), tenant_id="__system__", role="system_admin",
            token_version=token_version,
            roles=["system_admin", "modeler"],
        )
        await system_audit(
            sys_db,
            action="auth.system_login_success",
            severity="warn",
            actor_email=canonical_email(body.email),
            ip_address=client_ip,
        )
        await sys_db.commit()
        return login_response(
            token=token, role="system_admin", tenant_id=None,
            max_age_seconds=_jwt_max_age_seconds(),
            request=request,
        )
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Authentication service unavailable",
    )


@router.post("/login")
async def login(body: LoginRequest, request: Request) -> Response:
    """
    Authenticate with email + password via the configured auth backend chain.
    tenant_id must be provided in the request body so we know which DB to query.
    Sets an httpOnly JWT cookie and returns role in the body.
    """
    client_ip = request.client.host if request.client else None
    try:
        async for sys_db in get_system_db():
            await assert_not_locked(sys_db, body.tenant_id, body.email)
        chain = get_auth_chain()
        identity = await chain.authenticate(
            tenant_id=body.tenant_id, email=body.email, password=body.password,
        )
        if identity is None:
            async for sys_db in get_system_db():
                await record_login_failure(sys_db, body.tenant_id, body.email)
            async for db in get_tenant_db(body.tenant_id):
                await audit(
                    db, action="auth.login_failure", severity="critical",
                    actor_email=body.email, ip_address=client_ip,
                    detail={"reason": "invalid_credentials"},
                )
                await db.commit()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
            )
        async for sys_db in get_system_db():
            await record_login_success(sys_db, body.tenant_id, body.email)
        async for db in get_tenant_db(body.tenant_id):
            await require_external_identity_admitted(db, identity)
            local_user, role = await jit_adopt_user(db, identity, body.tenant_id)
            await audit(
                db, action="auth.login_success", severity="info",
                actor_id=getattr(local_user, "id", None),
                actor_email=local_user.email, ip_address=client_ip,
                detail={"backend": identity.source_backend, "role": role},
            )
            await db.commit()
        token = create_access_token(
            sub=local_user.email, tenant_id=body.tenant_id, role=role,
            groups=identity.groups or [],
            claims=identity.raw_claims or {},
            token_version=getattr(local_user, "token_version", 0),
            roles=[role] if role else [],
        )
        return login_response(
            token=token, role=role, tenant_id=body.tenant_id,
            max_age_seconds=_jwt_max_age_seconds(),
            request=request,
        )
    except HTTPException:
        raise
    except ValueError:
        # Unknown tenant slugs surface as ValueError from get_tenant_db; keep
        # the response opaque, but LOG the cause — a ValueError raised deeper
        # in the flow (e.g. JIT adoption) is an operator-actionable defect,
        # not a credential problem, and a silent 401 hides it.
        import logging
        logging.getLogger(__name__).exception(
            "login failed with ValueError (tenant=%s, email=%s) — mapped to 401",
            body.tenant_id, body.email,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid tenant or credentials",
        )


@router.post("/login/discover")
async def login_discover(body: LoginRequest, request: Request) -> Response:
    """
    Cross-tenant login: find which tenant the email belongs to.

    Iterates active tenants in the system DB (ordered by slug for
    deterministic first-match-wins behaviour) and tries to authenticate
    the email/password against each tenant's local_users table. Returns
    the JWT scoped to the first matching tenant.

    CR-002 Finding 3 remediation:
    - Tenant iteration is explicit ``ORDER BY slug`` so the result does
      not depend on insertion order.
    - Operational failures (DB down, broken migration, timeout) are
      logged at WARN with the tenant slug instead of silently continuing
      to the next tenant. This keeps "wrong credentials" vs. "tenant is
      broken" distinguishable.
    - When a single credential pair matches more than one tenant we
      fail closed (401) unless the request body carries a tenant_id
      hint matching one of the admitted tenants (Bug-7319).

    The request body carries ``tenant_id`` as an optional disambiguation
    hint: all tenants are always searched, but when multiple tenants
    match the same credentials, the hint selects among authenticated
    matches without bypassing authentication.
    """
    import logging
    from sqlalchemy import func as sa_func, select as sa_select

    logger = logging.getLogger(__name__)
    client_ip = request.client.host if request.client else None

    async for sys_db in get_system_db():
        await assert_not_locked(sys_db, DISCOVER_SCOPE, body.email)
        result = await sys_db.execute(
            sa_select(SystemTenant)
            .where(SystemTenant.is_active == True)  # noqa: E712
            .order_by(SystemTenant.slug)
        )
        tenants = result.scalars().all()

    tenant_hint = (body.tenant_id or "").strip().lower()
    if tenant_hint == "_discover":
        # JDBC/BI discover clients send tenant_id=_discover as a sentinel, not a slug.
        tenant_hint = ""
    if len(tenants) > _DISCOVER_TENANT_CAP and not tenant_hint:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "tenant_id is required when more than "
                f"{_DISCOVER_TENANT_CAP} tenants exist"
            ),
        )
    if tenant_hint:
        tenants = [t for t in tenants if t.slug.lower() == tenant_hint]
        if not tenants:
            verify_password(body.password, _DISCOVER_DUMMY_HASH)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
            )

    # Discovery now uses the configured credential auth chain per tenant so
    # LDAP/GCP IAM users can authenticate through the BI-client fallback. The
    # local backend pays one real-or-dummy bcrypt cost per tenant it checks;
    # when no backend admits the identity we still pay one fixed dummy bcrypt
    # cost below so an all-miss path is not a zero-cost user-enumeration oracle.
    # Ambiguous admitted tenant matches fail closed instead of first-match-wins.
    email = (body.email or "").strip().lower()
    matches: list[tuple[str, UserIdentity]] = []
    evaluation_failed = False
    chain = get_auth_chain()

    for tenant in tenants:
        identity: UserIdentity | None = None
        try:
            async for db in get_tenant_db(tenant.slug):
                outcome = await chain.authenticate_outcome(
                    tenant_id=tenant.slug, email=email, password=body.password,
                )
                if outcome.backend_error:
                    evaluation_failed = True
                    logger.warning(
                        "login_discover: tenant %r backend %r raised %s during auth lookup",
                        tenant.slug,
                        outcome.backend_name,
                        type(outcome.error).__name__ if outcome.error else "error",
                    )
                    continue
                if not outcome.authenticated or outcome.identity is None:
                    continue
                identity = outcome.identity
                if is_external_identity(identity):
                    if not await external_identity_admitted(db, identity):
                        continue
                else:
                    user = (
                        await db.execute(
                            sa_select(LocalUser).where(
                                sa_func.lower(LocalUser.email) == identity.email.strip().lower(),
                                LocalUser.is_active == True,  # noqa: E712
                            )
                        )
                    ).scalar_one_or_none()
                    if user is None:
                        continue
                matches.append((tenant.slug, identity))
        except Exception as exc:
            # Operational failure for any active tenant means discovery cannot
            # prove uniqueness. Keep evaluating for logging, then fail closed
            # before any tenant-local mutation or token issuance.
            evaluation_failed = True
            logger.warning(
                "login_discover: tenant %r raised %s during auth lookup: %s",
                tenant.slug, type(exc).__name__, exc,
            )
            continue

    if evaluation_failed:
        logger.warning(
            "login_discover: one or more active tenants could not be evaluated; "
            "failing closed"
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service unavailable",
        )

    # Run a single bcrypt verify. On no match, verify against a fixed valid
    # dummy hash so the response timing does not reveal whether the email
    # exists (the bcrypt cost is paid either way).
    if not matches:
        verify_password(body.password, _DISCOVER_DUMMY_HASH)
        _schedule_noop_audit()
        async for sys_db in get_system_db():
            await record_login_failure(sys_db, DISCOVER_SCOPE, email)
        logger.warning(
            "login_discover: failed attempt for email=%r from ip=%s (no tenant match)",
            (body.email or "").strip().lower(), client_ip,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    if len(matches) != 1:
        # Bug-7319: when the request body carries a non-empty tenant_id
        # hint, use it to disambiguate instead of failing closed.  The
        # hint is trusted only for *selection* among already-authenticated
        # matches -- it cannot bypass authentication.
        tenant_hint = (body.tenant_id or "").strip().lower()
        hinted_match = None
        if tenant_hint:
            for slug, identity in matches:
                if slug.lower() == tenant_hint:
                    hinted_match = (slug, identity)
                    break
        if hinted_match is not None:
            logger.info(
                "login_discover: credential pair matched multiple tenants "
                "(%s); resolved via tenant_id hint %r",
                ", ".join(slug for slug, _ in matches),
                tenant_hint,
            )
            matches = [hinted_match]
        else:
            logger.warning(
                "login_discover: credential pair matched multiple admitted "
                "tenants; failing closed (%s)",
                ", ".join(slug for slug, _identity in matches),
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
            )

    matched_slug, matched_identity = matches[0]
    async for db in get_tenant_db(matched_slug):
        if is_external_identity(matched_identity):
            await require_external_identity_admitted(db, matched_identity)
            matched_user, matched_role = await jit_adopt_user(
                db, matched_identity, matched_slug
            )
        else:
            matched_user = (
                await db.execute(
                    sa_select(LocalUser).where(
                        sa_func.lower(LocalUser.email)
                        == matched_identity.email.strip().lower(),
                        LocalUser.is_active == True,  # noqa: E712
                    )
                )
            ).scalar_one_or_none()
            if matched_user is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid credentials",
                )
            matched_role = matched_user.role
    # F-021-16: pay a dummy bcrypt on success as well as miss so the
    # response timing does not reveal a hit.
    verify_password(body.password, _DISCOVER_DUMMY_HASH)
    async for sys_db in get_system_db():
        await record_login_success(sys_db, DISCOVER_SCOPE, email)

    matched_token = create_access_token(
        sub=matched_user.email,
        tenant_id=matched_slug,
        role=matched_user.role,
        groups=matched_identity.groups or [],
        claims=matched_identity.raw_claims or {},
        token_version=getattr(matched_user, "token_version", 0),
        roles=[matched_user.role] if matched_user.role else [],
    )
    async for db in get_tenant_db(matched_slug):
        await audit_required(
            db, action="auth.login_success", severity="info",
            actor_id=getattr(matched_user, "id", None),
            actor_email=matched_user.email, ip_address=client_ip,
            detail={"role": matched_role, "via": "discover"},
        )
        await db.commit()

    return login_response(
        token=matched_token, role=matched_role, tenant_id=matched_slug,
        max_age_seconds=_jwt_max_age_seconds(),
        request=request,
    )


@router.post("/logout")
async def logout(request: Request) -> Response:
    """End the session: bump token_version / revoke embed jti, then clear cookies.

    F-021-01: a captured Bearer token must 401 after logout. Cookie clear alone
    does not invalidate the JWT.
    """
    token = extract_token_optional(request)
    client_ip = request.client.host if request.client else None
    if token:
        try:
            payload = decode_access_token(token)
        except Exception:
            payload = None
        if payload:
            aud = payload.get("aud")
            tenant_id = payload.get("tenant_id")
            sub = payload.get("sub")
            role = payload.get("role")
            if aud == "embed":
                jti = payload.get("jti")
                if jti and tenant_id:
                    from shared.auth.embed_revocation import revoke_embed_token
                    try:
                        await revoke_embed_token(str(jti), str(tenant_id))
                    except Exception:
                        logger.warning("embed logout revocation failed", exc_info=True)
            elif role == "system_admin" and tenant_id == "__system__":
                await bump_system_admin_token_version()
                async for sys_db in get_system_db():
                    await system_audit(
                        sys_db,
                        action="auth.logout",
                        severity="warn",
                        actor_email=str(sub) if sub else None,
                        ip_address=client_ip,
                    )
                    await sys_db.commit()
            elif tenant_id and sub:
                from sqlalchemy import func as sa_func
                from shared.db.models import LocalUser as _LocalUser
                async for db in get_tenant_db(str(tenant_id)):
                    result = await db.execute(
                        select(_LocalUser).where(
                            sa_func.lower(_LocalUser.email) == str(sub).lower()
                        )
                    )
                    local_user = result.scalar_one_or_none()
                    if local_user is not None:
                        await bump_local_user_token_version(db, local_user)
                    await audit_required(
                        db, action="auth.logout", severity="warn",
                        actor_email=str(sub), ip_address=client_ip,
                    )
                    await db.commit()
    response = Response(
        content='{"status":"ok"}',
        media_type="application/json",
    )
    clear_auth_cookies(response)
    return response


@router.post("/refresh")
async def refresh_session(
    request: Request,
    current_user: CurrentUser = Depends(require_human_user),
) -> Response:
    """Sliding session: re-mint a JWT with the same token_version and a new exp (G-021-03)."""
    if current_user.role == "system_admin" and current_user.tenant_id == "__system__":
        version = await get_system_admin_token_version()
        token = create_access_token(
            sub=current_user.email,
            tenant_id="__system__",
            role="system_admin",
            token_version=version,
            roles=["system_admin", "modeler"],
        )
        return login_response(
            token=token, role="system_admin", tenant_id=None,
            max_age_seconds=_jwt_max_age_seconds(),
            request=request,
        )
    async for db in get_tenant_db(current_user.tenant_id):
        from sqlalchemy import func as sa_func
        from shared.db.models import LocalUser as _LocalUser
        result = await db.execute(
            select(_LocalUser).where(
                sa_func.lower(_LocalUser.email) == current_user.email.lower()
            )
        )
        local_user = result.scalar_one_or_none()
        if local_user is None or not local_user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
            )
        token = create_access_token(
            sub=local_user.email,
            tenant_id=current_user.tenant_id,
            role=local_user.role,
            groups=current_user.groups or [],
            claims=current_user.claims or {},
            token_version=getattr(local_user, "token_version", 0),
            roles=[local_user.role] if local_user.role else [],
        )
        return login_response(
            token=token, role=local_user.role, tenant_id=current_user.tenant_id,
            max_age_seconds=_jwt_max_age_seconds(),
            request=request,
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
    )


@router.post("/users/bootstrap", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def bootstrap_user(
    body: UserCreate,
    tenant_id: str,
    _admin: CurrentUser = Depends(require_system_admin),
) -> UserResponse:
    """
    Create the first user for a tenant. Requires system admin auth.
    Only works when no users exist yet in the tenant.
    """
    async for db in get_tenant_db(tenant_id):
        email = canonical_email(body.email)
        count = await db.execute(select(LocalUser))
        if count.scalars().first() is not None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Bootstrap denied — users already exist. Use POST /auth/users with tenant authentication.",
            )
        # Bug-7326: reject overlong passwords before they reach bcrypt.
        _validate_password_length(body.password)
        # Bug-5944: honour the ``role`` field from UserCreate so the first
        # tenant user can be created as tenant_admin (or another allowed role)
        # matching the normal user-create path.
        user = LocalUser(
            username=body.username,
            email=email,
            hashed_password=hash_password(body.password),
            is_active=True,
            role=body.role or "member",
            # Bug-6597: operator-created role is manual intent; never SSO-demoted.
            role_source="manual",
            auth_source="local",
            token_version=0,
            has_completed_onboarding=False,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return UserResponse.model_validate(user)


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate,
    current_user: CurrentUser = Depends(require_tenant_admin),
    tenant_id: str | None = None,
) -> UserResponse:
    target_tenant_id = _resolve_target_tenant_id(current_user, tenant_id)
    async for db in get_tenant_db(target_tenant_id):
        email = canonical_email(body.email)
        async def _count_users() -> int:
            r = await db.execute(
                select(func.count()).select_from(LocalUser).where(
                    LocalUser.is_active == True  # noqa: E712
                )
            )
            return int(r.scalar() or 0)

        # Bug-6567: pass db so the count-then-create is serialised with an
        # advisory lock, preventing two concurrent creates at cap-1.
        await enforce_create_cap("user", _count_users, db=db)

        existing = await db.execute(
            select(LocalUser).where(
                (func.lower(LocalUser.email) == email) | (LocalUser.username == body.username)
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Email or username already registered",
            )
        # Bug-7326: reject overlong passwords before they reach bcrypt.
        _validate_password_length(body.password)
        user = LocalUser(
            username=body.username,
            email=email,
            hashed_password=hash_password(body.password),
            is_active=True,
            role=body.role or "member",
            # Bug-6597: operator-created role is manual intent; never SSO-demoted.
            role_source="manual",
            auth_source="local",
            token_version=0,
            has_completed_onboarding=False,
        )
        db.add(user)
        await db.flush()
        # B3-F2 / F-022-02: creating a user (can mint a tenant_admin) is a
        # protected access mutation. Fail closed.
        await audit_required(
            db, action="user.create", severity="warn",
            actor_email=current_user.email,
            target_type="user", target_id=user.id, target_name=user.email,
            detail={"role": user.role},
        )
        await db.commit()
        await emit_webhook(target_tenant_id, "user.created", {
            "user_id": str(user.id),
            "email": user.email,
            "role": user.role,
            "actor": current_user.email,
        })
        await db.refresh(user)
        return UserResponse.model_validate(user)


@router.get("/users/me", response_model=UserResponse)
async def get_me(
    current_user: CurrentUser = Depends(require_human_user),
) -> UserResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(
            select(LocalUser).where(func.lower(LocalUser.email) == canonical_email(current_user.email))
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        return UserResponse.model_validate(user)


@router.post("/users/me/complete-onboarding", response_model=UserResponse)
async def complete_onboarding(
    current_user: CurrentUser = Depends(require_human_user),
) -> UserResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(
            select(LocalUser).where(func.lower(LocalUser.email) == canonical_email(current_user.email))
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        user.has_completed_onboarding = True
        await db.commit()
        await db.refresh(user)
        return UserResponse.model_validate(user)


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    current_user: CurrentUser = Depends(require_tenant_admin),
    tenant_id: str | None = None,
) -> list[UserResponse]:
    target_tenant_id = _resolve_target_tenant_id(current_user, tenant_id)
    async for db in get_tenant_db(target_tenant_id):
        result = await db.execute(select(LocalUser).order_by(LocalUser.email))
        users = result.scalars().all()
        return [UserResponse.model_validate(u) for u in users]


@router.patch("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    body: UserUpdate,
    current_user: CurrentUser = Depends(require_tenant_admin),
    tenant_id: str | None = None,
) -> UserResponse:
    target_tenant_id = _resolve_target_tenant_id(current_user, tenant_id)
    async for db in get_tenant_db(target_tenant_id):
        user = await db.get(LocalUser, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        updates = body.model_dump(exclude_unset=True)
        old_email = canonical_email(user.email)
        # F-022-01: snapshot the sensitive access-control fields before mutation
        # so the audit record is reconstructive (who changed a role/status from
        # what to what), not just a list of touched field names.
        _audit_before = {
            "role": user.role,
            "is_active": user.is_active,
            "email": old_email,
        }
        if "email" in updates:
            updates["email"] = canonical_email(updates["email"])
        if "email" in updates and updates["email"] != old_email:
            existing = await db.execute(
                select(LocalUser).where(func.lower(LocalUser.email) == updates["email"])
            )
            if existing.scalar_one_or_none():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Email already registered",
                )
        if "username" in updates and updates["username"] != user.username:
            existing = await db.execute(
                select(LocalUser).where(LocalUser.username == updates["username"])
            )
            if existing.scalar_one_or_none():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Username already registered",
                )

        # Bug-6597 last-admin guard: block an update that would strip the LAST
        # active tenant_admin — either a role change away from tenant_admin or a
        # deactivation — unless another active tenant_admin remains. This keeps
        # tenant administration recoverable without the env-hardcoded system
        # admin. Only a change to an admin-affecting field can orphan the tenant;
        # the guard re-reads the target under a row lock so the decision is not
        # made from the unlocked ``db.get`` snapshot (Bug-6640).
        if "role" in updates or "is_active" in updates:
            orphans = await applying_change_orphans_tenant(
                db,
                user.id,
                new_role=updates.get("role", _UNSET_UPDATE),
                new_is_active=updates.get("is_active", _UNSET_UPDATE),
            )
            if orphans:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Cannot remove the last tenant administrator. Promote "
                        "another user to tenant_admin first."
                    ),
                )

        if updates.get("is_active") is True and not bool(user.is_active):
            async def _count_active_users() -> int:
                r = await db.execute(
                    select(func.count()).select_from(LocalUser).where(
                        LocalUser.is_active == True  # noqa: E712
                    )
                )
                return int(r.scalar() or 0)

            await enforce_create_cap("user", _count_active_users, db=db)

        for key, value in updates.items():
            setattr(user, key, value)
        if "email" in updates and updates["email"] != old_email:
            bindings = (
                await db.execute(
                    select(UserAccessBinding).where(
                        user_identity_matches(UserAccessBinding.user_identity, old_email)
                    )
                )
            ).scalars().all()
            for binding in bindings:
                binding.user_identity = canonical_user_identity(updates["email"])
        # A role explicitly set by an operator is manual intent — mark it so the
        # SSO reconcile path can never auto-demote it (Bug-6597).
        if "role" in updates:
            user.role_source = "manual"
        if {"role", "is_active", "email"} & set(updates):
            await bump_local_user_token_version(db, user)
        # F-022-01/F-022-02: a role/status/email change is a protected access
        # mutation. Record reconstructive before/after and fail closed so the
        # change cannot commit if its evidence cannot be persisted.
        _audit_after = {
            "role": user.role,
            "is_active": user.is_active,
            "email": canonical_email(user.email),
        }
        await audit_required(
            db, action="user.update", severity="warn",
            actor_email=current_user.email,
            target_type="user", target_id=user.id, target_name=user.email,
            detail={
                "fields": list(updates.keys()),
                "before": _audit_before,
                "after": _audit_after,
            },
        )
        # Bug-6668: if the row was deleted between the guard read and flush,
        # SQLAlchemy raises StaleDataError. Surface as 404, not 500.
        try:
            await db.commit()
        except StaleDataError:
            raise HTTPException(status_code=404, detail="User not found")
        await db.refresh(user)
        await emit_webhook(target_tenant_id, "user.updated", {
            "user_id": str(user.id),
            "email": canonical_email(user.email),
            "fields": list(updates.keys()),
            "before": _audit_before,
            "after": _audit_after,
            "actor": current_user.email,
        })
        return UserResponse.model_validate(user)


@router.post("/users/{user_id}/reset-password", response_model=UserResponse)
async def reset_user_password(
    user_id: str,
    body: UserPasswordReset,
    current_user: CurrentUser = Depends(require_tenant_admin),
    tenant_id: str | None = None,
) -> UserResponse:
    target_tenant_id = _resolve_target_tenant_id(current_user, tenant_id)
    async for db in get_tenant_db(target_tenant_id):
        user = await db.get(LocalUser, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        if getattr(user, "auth_source", "local") != "local":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Password is managed by the external identity provider",
            )

        # Bug-7326: reject overlong passwords before they reach bcrypt.
        _validate_password_length(body.password)
        user.hashed_password = hash_password(body.password)
        await bump_local_user_token_version(db, user)
        # B3-F2 / F-022-02: password reset is a critical account-takeover vector.
        # Fail closed.
        await audit_required(
            db, action="user.password_reset", severity="critical",
            actor_email=current_user.email,
            target_type="user", target_id=user.id, target_name=user.email,
        )
        await db.commit()
        await db.refresh(user)
        return UserResponse.model_validate(user)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    current_user: CurrentUser = Depends(require_tenant_admin),
    tenant_id: str | None = None,
) -> None:
    target_tenant_id = _resolve_target_tenant_id(current_user, tenant_id)
    async for db in get_tenant_db(target_tenant_id):
        user = await db.get(LocalUser, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        # Bug-6597 last-admin guard: refuse to delete the last active
        # tenant_admin so the tenant can never be locked out of administration.
        # Modelled as removing the target from the active-admin set; the guard
        # re-reads the target under a row lock (Bug-6640).
        if await applying_change_orphans_tenant(db, user.id, new_is_active=False):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Cannot delete the last tenant administrator. Promote "
                    "another user to tenant_admin first."
                ),
            )
        user_email = user.email
        user_id_val = user.id
        await db.delete(user)
        # B3-F2 / F-022-02: deleting a user is a destructive critical mutation.
        # Fail closed.
        await audit_required(
            db, action="user.delete", severity="critical",
            actor_email=current_user.email,
            target_type="user", target_id=user_id_val, target_name=user_email,
        )
        # Bug-6668: if the row vanished between the guard read and flush,
        # SQLAlchemy raises StaleDataError. Surface as 404, not 500.
        try:
            await db.commit()
        except StaleDataError:
            raise HTTPException(status_code=404, detail="User not found")
        await emit_webhook(target_tenant_id, "user.deleted", {
            "user_id": str(user_id_val),
            "email": user_email,
            "actor": current_user.email,
        })
