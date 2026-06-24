"""OIDC authorization-code flow backend.

Handles discovery, authorization redirect construction, and
code-for-token exchange + ID token validation.  Uses authlib.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlencode

import httpx
from joserfc import jwt as joserfc_jwt
from joserfc.jwk import KeySet

from shared.auth.backend import UserIdentity
from shared.config.settings import get_settings

logger = logging.getLogger(__name__)

_discovery_cache: dict[str, dict[str, Any]] = {}
_discovery_cache_lock = asyncio.Lock()  # M-05 fix: protect concurrent discovery


def _hmac_equal(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a.encode(), b.encode())


async def _discover(issuer: str) -> dict[str, Any] | None:
    """Fetch OpenID Connect discovery document."""
    # M-05/F-01 fix: check cache under lock, fetch HTTP outside lock, insert under lock
    async with _discovery_cache_lock:
        if issuer in _discovery_cache:
            return _discovery_cache[issuer]

    # Fetch outside lock to avoid blocking concurrent requests for different issuers
    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            doc = resp.json()
    except Exception:
        logger.exception("OIDC discovery failed for %s", issuer)
        return None

    # Insert under lock
    async with _discovery_cache_lock:
        # Double-check in case another request cached it while we fetched
        if issuer not in _discovery_cache:
            _discovery_cache[issuer] = doc
        return _discovery_cache[issuer]


async def _fetch_jwks(jwks_uri: str) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(jwks_uri)
            resp.raise_for_status()
            return resp.json()
    except Exception:
        logger.exception("Failed to fetch JWKS from %s", jwks_uri)
        return None


def get_oidc_config() -> dict[str, str] | None:
    """Return OIDC config from bootstrap settings, or None if not configured."""
    settings = get_settings()
    if not settings.OIDC_ISSUER or not settings.OIDC_CLIENT_ID:
        return None
    return {
        "issuer": settings.OIDC_ISSUER,
        "client_id": settings.OIDC_CLIENT_ID,
        "client_secret": settings.OIDC_CLIENT_SECRET,
        "scopes": settings.OIDC_SCOPES,
        "groups_claim": settings.OIDC_GROUPS_CLAIM,
    }


async def build_authorization_url(
    base_url: str,
    state: str,
    tenant_id: str,
    nonce: str | None = None,
) -> str | None:
    """Construct the OIDC authorization redirect URL.

    F-021-09: ``nonce`` is included in the authorization request and bound to
    the SSO flow; the callback verifies it against the id_token's ``nonce``
    claim to defeat id_token replay.
    """
    cfg = get_oidc_config()
    if cfg is None:
        return None

    discovery = await _discover(cfg["issuer"])
    if discovery is None:
        return None

    callback_url = f"{base_url}/api/v1/auth/oidc/callback"
    params = {
        "response_type": "code",
        "client_id": cfg["client_id"],
        "redirect_uri": callback_url,
        "scope": cfg["scopes"],
        "state": state,
    }
    if nonce:
        params["nonce"] = nonce
    authorize_endpoint = discovery["authorization_endpoint"]
    return f"{authorize_endpoint}?{urlencode(params)}"


async def exchange_code(
    base_url: str,
    code: str,
    expected_nonce: str | None = None,
) -> UserIdentity | None:
    """Exchange authorization code for tokens and extract identity."""
    cfg = get_oidc_config()
    if cfg is None:
        return None

    discovery = await _discover(cfg["issuer"])
    if discovery is None:
        return None

    callback_url = f"{base_url}/api/v1/auth/oidc/callback"
    token_endpoint = discovery["token_endpoint"]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": callback_url,
                    "client_id": cfg["client_id"],
                    "client_secret": cfg["client_secret"],
                },
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            token_data = resp.json()
    except Exception:
        logger.exception("OIDC token exchange failed")
        return None

    id_token_raw = token_data.get("id_token")
    if not id_token_raw:
        logger.warning("OIDC token response missing id_token")
        return None

    jwks_uri = discovery.get("jwks_uri")
    if not jwks_uri:
        logger.warning("OIDC discovery missing jwks_uri")
        return None

    jwks = await _fetch_jwks(jwks_uri)
    if not jwks:
        return None

    try:
        key_set = KeySet.import_key_set(jwks)
        token = joserfc_jwt.decode(id_token_raw, key_set)
        claims = token.claims
        if claims.get("iss") != cfg["issuer"]:
            logger.warning("OIDC ID token issuer mismatch")
            return None
        aud = claims.get("aud")
        if isinstance(aud, list):
            if cfg["client_id"] not in aud:
                logger.warning("OIDC ID token audience mismatch")
                return None
        elif aud != cfg["client_id"]:
            logger.warning("OIDC ID token audience mismatch")
            return None
        # F-021-09: verify the nonce when one was issued for this flow. A
        # missing or mismatched nonce on a flow that demanded one is a replay
        # signal — fail closed.
        if expected_nonce is not None:
            token_nonce = claims.get("nonce")
            if not token_nonce or not _hmac_equal(str(token_nonce), expected_nonce):
                logger.warning("OIDC ID token nonce mismatch — rejecting (replay defence)")
                return None
    except Exception:
        logger.exception("OIDC ID token validation failed")
        return None

    email = claims.get("email", "")
    if not email:
        logger.warning("OIDC ID token missing email claim")
        return None

    display_name = claims.get("name", "")
    groups_claim = cfg["groups_claim"]
    groups = claims.get(groups_claim, [])
    if isinstance(groups, str):
        groups = [groups]

    # F-007-02: surface the full ID-token claim set (minus protocol claims)
    # plus the granted scopes so oidc_scope / saml_claim-style row-security
    # rules can match at query time. Per RFC 6749 §5.1 the token response
    # omits "scope" when the granted scopes equal the requested ones.
    _protocol_claims = {
        "iss", "aud", "exp", "iat", "nbf", "auth_time",
        "nonce", "at_hash", "c_hash", "azp", "jti",
    }
    raw_claims: dict = {
        k: v for k, v in claims.items() if k not in _protocol_claims
    }
    granted_scope = token_data.get("scope") or cfg["scopes"]
    if isinstance(granted_scope, str):
        raw_claims["scope"] = granted_scope.split()
    elif isinstance(granted_scope, list):
        raw_claims["scope"] = [str(s) for s in granted_scope]

    return UserIdentity(
        email=email,
        display_name=display_name,
        groups=groups,
        source_backend="oidc",
        raw_claims=raw_claims,
    )
