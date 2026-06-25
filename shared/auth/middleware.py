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

from fastapi import Depends, HTTPException, Request, status

from shared.auth.jwt import decode_access_token

logger = logging.getLogger(__name__)


def _extract_token(request: Request) -> str:
    """Return the raw JWT string from header or cookie, or raise 401."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header.split(" ", 1)[1]

    cookie_token = request.cookies.get("access_token")
    if cookie_token:
        return cookie_token

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
        raw_token: str = "",
    ):
        self.user_id = user_id
        self.tenant_id = tenant_id
        self.email = email
        self.role = role
        self.groups: list[str] = groups or []
        # Arbitrary IdP claims (SAML attributes / OIDC claims + scopes)
        # carried on the JWT for row-security attribute matching (F-007-02).
        self.claims: dict = claims or {}
        self.raw_token = raw_token


class CurrentEmbedUser(CurrentUser):
    """Extended context for embed-token sessions."""

    is_embed: bool = True

    def __init__(
        self,
        user_id: str,
        tenant_id: str,
        email: str,
        persona_id: str | None = None,
        project_ids: list[str] | None = None,
        model_ids: list[str] | None = None,
        capabilities: list[str] | None = None,
    ):
        super().__init__(
            user_id=user_id, tenant_id=tenant_id, email=email, role="embed",
        )
        self.persona_id = persona_id
        self.project_ids = project_ids
        self.model_ids = model_ids
        self.capabilities: list[str] = capabilities if capabilities is not None else ["query", "chat", "explore"]


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
        caps = raw_caps if isinstance(raw_caps, list) else ["query", "chat", "explore"]
        raw_projects = payload.get("project_ids")
        project_ids = [str(p).lower() for p in raw_projects] if isinstance(raw_projects, list) else None
        raw_models = payload.get("model_ids")
        model_ids = [str(m).lower() for m in raw_models] if isinstance(raw_models, list) else None
        return CurrentEmbedUser(
            user_id=sub,
            tenant_id=tenant_id,
            email=sub,
            persona_id=payload.get("persona_id"),
            project_ids=project_ids,
            model_ids=model_ids,
            capabilities=caps,
        )

    raw_groups = payload.get("groups")
    groups = raw_groups if isinstance(raw_groups, list) else []
    raw_claims = payload.get("claims")
    claims = raw_claims if isinstance(raw_claims, dict) else {}
    return CurrentUser(
        user_id=sub,
        tenant_id=tenant_id,
        email=sub,
        role=payload.get("role"),
        groups=groups,
        claims=claims,
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
                revoked = await is_embed_token_revoked(str(jti))
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

    user.raw_token = token
    return user


async def require_system_admin(request: Request) -> CurrentUser:
    """Require a system_admin JWT. Raises 403 for any other role."""
    exc_auth = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    exc_forbidden = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="System admin access required",
    )
    token = _extract_token(request)
    try:
        payload = decode_access_token(token)
    except Exception:
        raise exc_auth

    if payload.get("role") != "system_admin":
        raise exc_forbidden

    return CurrentUser(
        user_id=payload.get("sub", ""),
        tenant_id=payload.get("tenant_id", "__system__"),
        email=payload.get("sub", ""),
        role="system_admin",
    )


async def require_tenant_admin(
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Allow tenant_admin (within their tenant) or system_admin (any tenant)."""
    if current_user.role in ("tenant_admin", "system_admin"):
        return current_user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Tenant admin access required",
    )


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
    current_user: CurrentUser, model_id: str,
) -> None:
    """Raise 403 if embed token restricts models and this model is not in scope."""
    if isinstance(current_user, CurrentEmbedUser) and current_user.model_ids is not None:
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
