"""
Auth backend interface and JWT validation for the gateway service.

Primary authentication: JWT issued by model-service.
Google IAM and LDAP are scaffolded but disabled in V1.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from jose import JWTError, jwt

from shared.config.settings import get_settings

settings = get_settings()


# ---------------------------------------------------------------------------
# Token payload
# ---------------------------------------------------------------------------

@dataclass
class TokenPayload:
    sub: str          # user email or user_id
    tenant_id: str
    exp: int
    extra: dict[str, Any]


# ---------------------------------------------------------------------------
# JWT validation (used by both JDBC and XMLA paths)
# ---------------------------------------------------------------------------

def verify_jwt_token(token: str) -> TokenPayload:
    """
    Validate a JWT token signed by model-service.

    Raises:
        jose.JWTError on invalid signature, expiry, or missing claims.
    """
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except JWTError as exc:
        raise ValueError(f"Invalid JWT: {exc}") from exc

    sub = payload.get("sub")
    tenant_id = payload.get("tenant_id", "")
    exp = payload.get("exp", 0)

    if not sub:
        raise ValueError("JWT missing 'sub' claim")

    return TokenPayload(
        sub=sub,
        tenant_id=tenant_id,
        exp=exp,
        extra={k: v for k, v in payload.items() if k not in ("sub", "tenant_id", "exp")},
    )


# ---------------------------------------------------------------------------
# Auth backend Protocol
# ---------------------------------------------------------------------------

def extract_basic_credentials(authorization: str) -> Optional[tuple[str, str]]:
    """
    Parse an HTTP Basic auth header into (email, password).

    Returns (email, password) if the header is a valid Basic credential,
    or None if it is absent, malformed, or not a Basic scheme.
    """
    if not authorization.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(authorization[6:]).decode("utf-8")
        parts = decoded.split(":", 1)
        if len(parts) == 2:
            return parts[0], parts[1]
    except Exception:
        pass
    return None


class AuthBackend(Protocol):
    """Interface that all auth backends must satisfy."""

    async def authenticate(self, credentials: dict[str, str]) -> Optional[TokenPayload]:
        """
        Validate credentials and return a TokenPayload on success, None on failure.

        credentials keys depend on backend:
          - JWT:        {"token": "..."}
          - Google IAM: {"id_token": "..."}
          - LDAP:       {"username": "...", "password": "..."}
        """
        ...

    @property
    def is_enabled(self) -> bool:
        """Return True if this backend is enabled per settings."""
        ...
