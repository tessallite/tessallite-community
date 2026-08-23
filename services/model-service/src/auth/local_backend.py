"""
Local username/password auth backend backed by local_users table in the tenant DB.

Token DECODING lives in `shared/auth/jwt.py` so every service can verify
tokens without pulling in bcrypt/passlib. This file only owns token
ISSUANCE plus the password-hashing helpers that model-service uses at
user-creation time.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import bcrypt as _bcrypt
from jose import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.identity import canonical_user_identity
from shared.auth.jwt import decode_access_token
from shared.config.bootstrap import system_snapshot_get
from shared.config.settings import get_settings
from shared.db.models import LocalUser

settings = get_settings()

# Re-exported so existing `from src.auth.local_backend import decode_access_token`
# imports inside model-service continue to work after the move to shared/.
__all__ = [
    "hash_password",
    "verify_password",
    "create_access_token",
    "create_embed_token",
    "authenticate_system_admin",
    "decode_access_token",
    "authenticate_user",
]


def _check_password_length(plain: str) -> None:
    """Bug-7326: reject passwords that exceed bcrypt's 72-byte input limit.

    bcrypt truncates (or raises ValueError on newer library versions) for
    passwords whose UTF-8 encoding exceeds 72 bytes. Rather than silently
    truncating (which weakens security by making two distinct passwords hash
    identically) or letting the library raise an opaque 500, reject up front
    with a clear error that callers map to HTTP 400.
    """
    if len(plain.encode("utf-8")) > 72:
        raise ValueError(
            "Password too long: bcrypt supports a maximum of 72 bytes. "
            "Please choose a shorter password (up to 72 bytes in UTF-8)."
        )


def hash_password(plain: str) -> str:
    _check_password_length(plain)
    return _bcrypt.hashpw(plain.encode(), _bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Check a plaintext password against a bcrypt hash.

    Bug-7326: if the password exceeds bcrypt's 72-byte limit, return False
    rather than raising. No valid stored hash could have been produced from
    such a password (hash_password rejects it), so a mismatch is the only
    correct answer. This keeps the login path from crashing (500) on
    overlong input while giving no information about WHY the attempt failed.
    """
    if len(plain.encode("utf-8")) > 72:
        return False
    return _bcrypt.checkpw(plain.encode(), hashed.encode())


_LOGIN_DUMMY_HASH = hash_password("tessallite-login-dummy-no-match")


def create_access_token(
    sub: str,
    tenant_id: str,
    role: str | None = None,
    groups: list[str] | None = None,
    claims: dict | None = None,
    token_version: int | None = None,
    expire_minutes: int | None = None,
    roles: list[str] | None = None,
) -> str:
    """Mint a tenant-scoped access JWT.

    ``groups`` and ``claims`` carry the IdP identity attributes (IdP groups,
    SAML attributes, OIDC claims + granted scopes) so row-security
    ``idp_group`` / ``saml_claim`` / ``oidc_scope`` rules can be enforced at
    query time (F-007-02) — the runtime principal is built from this token.

    ``roles`` (Bug-8017 / F-007-03) is the multi-valued NAMED-role subject for
    ``jwt_role`` row-security OR-of-grants. It is distinct from the single
    ``role`` RBAC-tier claim: authorization keys off ``role``, while
    row-security ORs the row access of every entry in ``roles``. Callers pass
    the effective named-role set (today a single-element ``[role]`` for a local
    user — the local data model has no multi-named-role grant; see the mint call
    sites). When absent/empty no ``roles`` claim is written and the runtime
    principal falls back to ``{role}``, so legacy tokens behave unchanged.

    ``expire_minutes`` overrides the configured session lifetime; internal
    service-to-service tokens (Bug-6204) pass a small value so an elevated
    token is short-lived rather than carrying a full user-session TTL.
    """
    if expire_minutes is None:
        expire_minutes = int(system_snapshot_get("auth.jwt_expire_minutes"))
    sub = canonical_user_identity(sub)
    issued_at = datetime.now(timezone.utc)
    expire = issued_at + timedelta(minutes=expire_minutes)
    payload: dict = {
        "sub": sub,
        "tenant_id": tenant_id,
        "iat": issued_at,
        "exp": expire,
    }
    if role:
        payload["role"] = role
    if token_version is not None:
        payload["token_version"] = int(token_version)
    if groups:
        payload["groups"] = groups
    if claims:
        payload["claims"] = claims
    # Bug-8017 / F-007-03: mint the multi-valued named-role subject only when a
    # non-empty list is supplied. A missing/empty ``roles`` writes no claim, so
    # the decode path yields [] and the row-security principal falls back to
    # {role} — a legacy token stays byte-compatible.
    if roles:
        payload["roles"] = list(roles)
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_embed_token(
    *,
    user_identity: str,
    tenant_id: str,
    persona_id: str | None = None,
    project_persona_id: str | None = None,
    project_ids: list[str] | None = None,
    model_ids: list[str] | None = None,
    capabilities: list[str] | None = None,
    rls_role: str | None = None,
    rls_groups: list[str] | None = None,
    rls_claims: dict | None = None,
    expiry_minutes: int = 180,
) -> tuple[str, datetime]:
    """Mint a scoped embed JWT for ISV embedding.

    ``rls_role`` / ``rls_groups`` / ``rls_claims`` (Bug-7995 / F-024-01) carry the
    admin-authored row-security subject so ``jwt_role`` / ``idp_group`` /
    ``saml_claim`` / ``oidc_scope`` row-security rules fire for the embedded
    session, exactly as they do for an interactive one. They are written under the
    SAME top-level claim names an interactive access token uses (``role`` /
    ``groups`` / ``claims``) so a single decode path serves both token kinds.

    Returns (token_string, expiry_datetime).
    """
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=expiry_minutes)
    payload: dict = {
        "sub": user_identity,
        "tenant_id": tenant_id,
        "aud": "embed",
        "jti": str(uuid.uuid4()),
        "iat": now,
        "nbf": now,
        "exp": expire,
    }
    if persona_id:
        payload["persona_id"] = persona_id
    if project_persona_id:
        payload["project_persona_id"] = project_persona_id
    if project_ids is not None:
        payload["project_ids"] = project_ids
    if model_ids is not None:
        payload["model_ids"] = model_ids
    if capabilities is not None:
        payload["capabilities"] = capabilities
    else:
        # F-021-08: omit means deny-all, not the historical all-capabilities default.
        payload["capabilities"] = []
    # Row-security subject (same claim names as create_access_token). Only set
    # when non-empty so a bare embed token carries no spurious RLS subject.
    if rls_role:
        payload["role"] = rls_role
    if rls_groups:
        payload["groups"] = rls_groups
    if rls_claims:
        payload["claims"] = rls_claims
    return (
        jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM),
        expire,
    )


def authenticate_system_admin(email: str, password: str) -> bool:
    """Validate credentials against environment-level system admin config.

    Bug-7802: the email is NOT a secret, so a plain case-insensitive
    comparison is safe and avoids the hmac.compare_digest fragility on
    arbitrary Unicode email strings (the previous bytes-encode path was
    technically fine but unnecessarily tight coupling to hmac for a
    non-secret field). The PASSWORD is constant-time via
    ``hmac.compare_digest`` on UTF-8 bytes (the secret). Both comparisons
    run unconditionally (no short-circuit) so the email branch cannot leak
    the password-check timing.
    """
    import hmac

    email_ok = (email or "").strip().lower() == (settings.SYSTEM_ADMIN_EMAIL or "").strip().lower()
    password_ok = hmac.compare_digest(
        (password or "").encode("utf-8"), (settings.SYSTEM_ADMIN_PASSWORD or "").encode("utf-8")
    )
    return email_ok and password_ok


async def authenticate_user(
    db: AsyncSession, email: str, password: str
) -> LocalUser | None:
    # F-021-09: match email case-insensitively so a login with differing case
    # (and a JIT user stored lowercase) resolves to the same record.
    from sqlalchemy import func
    result = await db.execute(
        select(LocalUser).where(func.lower(LocalUser.email) == email.strip().lower())
    )
    user = result.scalar_one_or_none()
    hash_to_check = (
        user.hashed_password
        if user is not None and user.is_active
        else _LOGIN_DUMMY_HASH
    )
    password_ok = verify_password(password, hash_to_check)
    if user is None or not user.is_active:
        return None
    if not password_ok:
        return None
    return user
