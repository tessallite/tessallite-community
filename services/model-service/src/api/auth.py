"""
Auth routes: system admin login, tenant login, user CRUD.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select

logger = logging.getLogger(__name__)

from shared.audit.logger import audit
from shared.auth.cookie import clear_auth_cookies, login_response
from shared.config.bootstrap import system_snapshot_get
from shared.webhooks.dispatcher import emit_webhook
from shared.db.models import LocalUser, SystemTenant
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
from src.auth.jit import jit_adopt_user
from src.auth.local_backend import (
    authenticate_system_admin,
    create_access_token,
    hash_password,
    verify_password,
)
from src.auth.middleware import (
    CurrentUser,
    forbid_embed_user,
    get_current_user,
    require_system_admin,
    require_tenant_admin,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# A precomputed valid bcrypt hash of a random string, used by login_discover to
# pay the bcrypt cost on a no-email-match so timing does not reveal whether the
# email exists in any tenant (F-021-07).
_DISCOVER_DUMMY_HASH = hash_password("tessallite-discover-dummy-no-match")


def _jwt_max_age_seconds() -> int:
    return int(system_snapshot_get("auth.jwt_expire_minutes")) * 60


def _is_system_admin(user: CurrentUser) -> bool:
    return user.role == "system_admin"


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


@router.post("/system/login")
async def system_login(body: SystemLoginRequest) -> Response:
    """
    Authenticate as system admin using infrastructure-level credentials.
    Sets an httpOnly JWT cookie and returns role in the body.
    """
    if not authenticate_system_admin(body.email, body.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid system admin credentials",
        )
    token = create_access_token(
        sub=body.email, tenant_id="__system__", role="system_admin"
    )
    return login_response(
        token=token, role="system_admin", tenant_id=None,
        max_age_seconds=_jwt_max_age_seconds(),
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
        chain = get_auth_chain()
        identity = await chain.authenticate(
            tenant_id=body.tenant_id, email=body.email, password=body.password,
        )
        if identity is None:
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
        async for db in get_tenant_db(body.tenant_id):
            local_user, role = await jit_adopt_user(db, identity, body.tenant_id)
            await audit(
                db, action="auth.login_success", severity="info",
                actor_id=getattr(local_user, "id", None),
                actor_email=identity.email, ip_address=client_ip,
                detail={"backend": identity.source_backend, "role": role},
            )
            await db.commit()
        token = create_access_token(
            sub=identity.email, tenant_id=body.tenant_id, role=role,
            groups=identity.groups or [],
            claims=identity.raw_claims or {},
        )
        return login_response(
            token=token, role=role, tenant_id=body.tenant_id,
            max_age_seconds=_jwt_max_age_seconds(),
        )
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid tenant or credentials",
        )


@router.post("/login/discover")
async def login_discover(body: LoginRequest) -> Response:
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
    - When a single credential pair matches more than one tenant we log
      a WARN with both slugs and return the first match (documented
      first-match-wins policy).

    The request body still carries tenant_id for schema compatibility,
    but it is ignored — all tenants are searched.
    """
    import logging
    from sqlalchemy import func as sa_func, select as sa_select

    logger = logging.getLogger(__name__)

    async for sys_db in get_system_db():
        result = await sys_db.execute(
            sa_select(SystemTenant)
            .where(SystemTenant.is_active == True)  # noqa: E712
            .order_by(SystemTenant.slug)
        )
        tenants = result.scalars().all()

    # F-021-07: resolve the candidate user with a cheap indexed email lookup per
    # tenant FIRST, then run bcrypt exactly once against the single matched user
    # — instead of one bcrypt per tenant-with-this-email (O(tenants) CPU that an
    # attacker could amplify and use as a cross-tenant timing oracle). Email
    # match is case-insensitive (F-021-09).
    email = (body.email or "").strip().lower()
    matched_slug: str | None = None
    matched_user: LocalUser | None = None
    extra_matches: list[str] = []

    for tenant in tenants:
        try:
            async for db in get_tenant_db(tenant.slug):
                user = (
                    await db.execute(
                        sa_select(LocalUser).where(
                            sa_func.lower(LocalUser.email) == email,
                            LocalUser.is_active == True,  # noqa: E712
                        )
                    )
                ).scalar_one_or_none()
        except Exception as exc:
            # Operational failure for this tenant — log and move on, but do not
            # treat it as a credential failure.
            logger.warning(
                "login_discover: tenant %r raised %s during email lookup: %s",
                tenant.slug, type(exc).__name__, exc,
            )
            continue

        if user is None:
            continue
        if matched_slug is None:
            matched_slug = tenant.slug
            matched_user = user
        else:
            extra_matches.append(tenant.slug)

    # Run a single bcrypt verify. On no match, verify against a fixed valid
    # dummy hash so the response timing does not reveal whether the email
    # exists (the bcrypt cost is paid either way).
    if matched_user is None:
        verify_password(body.password, _DISCOVER_DUMMY_HASH)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    if not verify_password(body.password, matched_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    matched_role = matched_user.role
    matched_token = create_access_token(
        sub=matched_user.email, tenant_id=matched_slug, role=matched_user.role
    )

    if extra_matches:
        logger.warning(
            "login_discover: credential pair matched more than one tenant "
            "(%s, %s); returning first by slug",
            matched_slug, ", ".join(extra_matches),
        )

    return login_response(
        token=matched_token, role=matched_role, tenant_id=matched_slug,
        max_age_seconds=_jwt_max_age_seconds(),
    )


@router.post("/logout")
async def logout() -> Response:
    """Clear auth cookies, ending the browser session."""
    response = Response(
        content='{"status":"ok"}',
        media_type="application/json",
    )
    clear_auth_cookies(response)
    return response


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
        count = await db.execute(select(LocalUser))
        if count.scalars().first() is not None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Bootstrap denied — users already exist. Use POST /auth/users with tenant authentication.",
            )
        user = LocalUser(
            username=body.username,
            email=body.email,
            hashed_password=hash_password(body.password),
            is_active=True,
            auth_source="local",
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
        async def _count_users() -> int:
            r = await db.execute(select(func.count()).select_from(LocalUser))
            return int(r.scalar() or 0)

        await enforce_create_cap("user", _count_users)

        existing = await db.execute(
            select(LocalUser).where(
                (LocalUser.email == body.email) | (LocalUser.username == body.username)
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Email or username already registered",
            )
        user = LocalUser(
            username=body.username,
            email=body.email,
            hashed_password=hash_password(body.password),
            is_active=True,
            role=body.role or "member",
            auth_source="local",
            has_completed_onboarding=False,
        )
        db.add(user)
        await db.flush()
        await audit(
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
    current_user: CurrentUser = Depends(get_current_user),
    _: None = Depends(forbid_embed_user),
) -> UserResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(
            select(LocalUser).where(LocalUser.email == current_user.email)
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        return UserResponse.model_validate(user)


@router.post("/users/me/complete-onboarding", response_model=UserResponse)
async def complete_onboarding(
    current_user: CurrentUser = Depends(get_current_user),
    _: None = Depends(forbid_embed_user),
) -> UserResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(
            select(LocalUser).where(LocalUser.email == current_user.email)
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
        if "email" in updates and updates["email"] != user.email:
            existing = await db.execute(
                select(LocalUser).where(LocalUser.email == updates["email"])
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

        for key, value in updates.items():
            setattr(user, key, value)
        await audit(
            db, action="user.update", severity="warn",
            actor_email=current_user.email,
            target_type="user", target_id=user.id, target_name=user.email,
            detail={"fields": list(updates.keys())},
        )
        await db.commit()
        await db.refresh(user)
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

        user.hashed_password = hash_password(body.password)
        await audit(
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
        user_email = user.email
        user_id_val = user.id
        await db.delete(user)
        await audit(
            db, action="user.delete", severity="critical",
            actor_email=current_user.email,
            target_type="user", target_id=user_id_val, target_name=user_email,
        )
        await db.commit()
        await emit_webhook(target_tenant_id, "user.deleted", {
            "user_id": str(user_id_val),
            "email": user_email,
            "actor": current_user.email,
        })
