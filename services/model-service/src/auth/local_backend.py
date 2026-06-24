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


def hash_password(plain: str) -> str:
    return _bcrypt.hashpw(plain.encode(), _bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return _bcrypt.checkpw(plain.encode(), hashed.encode())


def create_access_token(
    sub: str,
    tenant_id: str,
    role: str | None = None,
    groups: list[str] | None = None,
    claims: dict | None = None,
) -> str:
    """Mint a tenant-scoped access JWT.

    ``groups`` and ``claims`` carry the IdP identity attributes (IdP groups,
    SAML attributes, OIDC claims + granted scopes) so row-security
    ``idp_group`` / ``saml_claim`` / ``oidc_scope`` rules can be enforced at
    query time (F-007-02) — the runtime principal is built from this token.
    """
    expire_minutes = int(system_snapshot_get("auth.jwt_expire_minutes"))
    expire = datetime.now(timezone.utc) + timedelta(minutes=expire_minutes)
    payload: dict = {"sub": sub, "tenant_id": tenant_id, "exp": expire}
    if role:
        payload["role"] = role
    if groups:
        payload["groups"] = groups
    if claims:
        payload["claims"] = claims
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_embed_token(
    *,
    user_identity: str,
    tenant_id: str,
    persona_id: str | None = None,
    project_ids: list[str] | None = None,
    model_ids: list[str] | None = None,
    capabilities: list[str] | None = None,
    expiry_minutes: int = 180,
) -> tuple[str, datetime]:
    """Mint a scoped embed JWT for ISV embedding.

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
    if project_ids is not None:
        payload["project_ids"] = project_ids
    if model_ids is not None:
        payload["model_ids"] = model_ids
    if capabilities is not None:
        payload["capabilities"] = capabilities
    else:
        payload["capabilities"] = ["query", "chat", "explore"]
    return (
        jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM),
        expire,
    )


def authenticate_system_admin(email: str, password: str) -> bool:
    """Validate credentials against environment-level system admin config.

    F-021-10: use ``hmac.compare_digest`` for both fields so the platform's
    single most privileged credential is checked in constant time, removing the
    timing side-channel of a plain ``==`` on a secret. Both comparisons run
    unconditionally (no short-circuit) so the email check cannot leak the
    password-check timing.
    """
    import hmac

    email_ok = hmac.compare_digest(
        (email or "").encode(), (settings.SYSTEM_ADMIN_EMAIL or "").encode()
    )
    password_ok = hmac.compare_digest(
        (password or "").encode(), (settings.SYSTEM_ADMIN_PASSWORD or "").encode()
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
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user
