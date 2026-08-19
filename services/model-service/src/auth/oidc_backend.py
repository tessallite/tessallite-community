"""OIDC authorization-code flow backend.

Handles discovery, authorization redirect construction, and
code-for-token exchange + ID token validation.  Uses authlib.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import urlencode

import httpx
from joserfc import jwt as joserfc_jwt
from joserfc.jwk import KeySet

from shared.auth.backend import UserIdentity
from shared.config.settings import get_settings

logger = logging.getLogger(__name__)

# Bug-5734: bounded LRU cache with TTL eviction for OIDC discovery documents.
# Max 64 issuers; entries expire after 1 hour. Prevents unbounded memory growth
# when many distinct issuers are discovered (e.g. per-tenant OIDC config).
_DISCOVERY_CACHE_MAX_SIZE = 64
_DISCOVERY_CACHE_TTL_SECONDS = 3600  # 1 hour

_discovery_cache: OrderedDict[str, tuple[dict[str, Any], float]] = OrderedDict()
_discovery_cache_lock = asyncio.Lock()  # M-05 fix: protect concurrent discovery


def _hmac_equal(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a.encode(), b.encode())


def derive_code_challenge(code_verifier: str) -> str:
    """Bug-8142: derive the PKCE (RFC 7636 §4.2) S256 code_challenge.

    ``code_challenge = BASE64URL-ENCODE(SHA256(ASCII(code_verifier)))`` with the
    ``=`` padding stripped. The verifier is generated with the flow state
    (``sso_state.create_state``) and persisted; only this challenge is placed on
    the authorization request. On the token exchange the RP replays the verifier,
    so an authorization code intercepted in transit cannot be redeemed by an
    attacker who never held the verifier.
    """
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


async def _discover(issuer: str) -> dict[str, Any] | None:
    """Fetch OpenID Connect discovery document.

    Bug-5734: the cache is bounded to ``_DISCOVERY_CACHE_MAX_SIZE`` entries
    with LRU eviction, and each entry expires after
    ``_DISCOVERY_CACHE_TTL_SECONDS``. This prevents unbounded memory growth
    when many distinct issuers are discovered.
    """
    now = time.monotonic()

    # M-05/F-01 fix: check cache under lock, fetch HTTP outside lock, insert under lock
    async with _discovery_cache_lock:
        if issuer in _discovery_cache:
            doc, cached_at = _discovery_cache[issuer]
            if now - cached_at < _DISCOVERY_CACHE_TTL_SECONDS:
                # Move to end (most recently used) for LRU ordering
                _discovery_cache.move_to_end(issuer)
                return doc
            else:
                # TTL expired — remove stale entry
                del _discovery_cache[issuer]

    # Fetch outside lock to avoid blocking concurrent requests for different issuers
    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    doc = await _ssrf_get_json(url)
    if doc is None:
        return None

    # Insert under lock with LRU eviction
    async with _discovery_cache_lock:
        # Double-check in case another request cached it while we fetched
        if issuer not in _discovery_cache:
            # Evict oldest entries if at capacity
            while len(_discovery_cache) >= _DISCOVERY_CACHE_MAX_SIZE:
                _discovery_cache.popitem(last=False)
            _discovery_cache[issuer] = (doc, time.monotonic())
        else:
            _discovery_cache.move_to_end(issuer)
        return _discovery_cache[issuer][0]


async def _fetch_jwks(jwks_uri: str) -> dict[str, Any] | None:
    return await _ssrf_get_json(jwks_uri)


def get_oidc_config() -> dict[str, str] | None:
    """Return OIDC config from process settings plus the tenant overlay (G-021-02)."""
    settings = get_settings()
    ov: dict[str, Any] = {}
    try:
        from src.auth.sso_overlay import current_overlay
        ov = current_overlay().get("oidc") or {}
    except Exception:
        ov = {}
    issuer = (ov.get("issuer") or settings.OIDC_ISSUER or "").strip()
    client_id = (ov.get("client_id") or settings.OIDC_CLIENT_ID or "").strip()
    if not issuer or not client_id:
        return None
    secret = settings.OIDC_CLIENT_SECRET or ""
    enc = ov.get("client_secret_enc")
    if isinstance(enc, str) and enc.strip():
        try:
            import base64
            from shared.security.credential_crypto import decrypt_str
            secret = decrypt_str(base64.b64decode(enc.strip()))
        except Exception:
            logger.warning("OIDC overlay client_secret_enc could not be decrypted")
    elif isinstance(ov.get("client_secret"), str) and ov["client_secret"].strip():
        secret = ov["client_secret"].strip()
    return {
        "issuer": issuer,
        "client_id": client_id,
        "client_secret": secret,
        "scopes": ov.get("scopes") or settings.OIDC_SCOPES,
        "groups_claim": ov.get("groups_claim") or settings.OIDC_GROUPS_CLAIM,
    }


async def _ssrf_get_json(url: str) -> dict[str, Any] | None:
    """GET JSON through the shared webhook SSRF guard (F-021-10)."""
    from shared.webhooks.ssrf import ssrf_safe_transport, validate_webhook_url

    try:
        safe_url = validate_webhook_url(url)
    except ValueError:
        logger.warning("OIDC URL rejected by SSRF pre-flight: %s", url)
        return None
    try:
        async with httpx.AsyncClient(
            transport=ssrf_safe_transport(), timeout=10.0, follow_redirects=False
        ) as client:
            resp = await client.get(safe_url)
            resp.raise_for_status()
            doc = resp.json()
            return doc if isinstance(doc, dict) else None
    except Exception:
        logger.exception("OIDC HTTP GET failed for %s", url)
        return None


async def _ssrf_post_form(url: str, data: dict[str, str]) -> dict[str, Any] | None:
    """POST form-encoded data through the shared webhook SSRF guard (Bug-9302).

    Pre-flight rejects blocked hosts (including localhost) and does not follow
    redirects. Programming errors from the HTTP client (e.g. TypeError) are
    not swallowed here — callers catch only transport/parse failures.
    """
    from shared.webhooks.ssrf import ssrf_safe_transport, validate_webhook_url

    try:
        safe_url = validate_webhook_url(url)
    except ValueError:
        logger.warning("OIDC token URL rejected by SSRF pre-flight: %s", url)
        return None
    async with httpx.AsyncClient(
        transport=ssrf_safe_transport(), timeout=15.0, follow_redirects=False
    ) as client:
        resp = await client.post(
            safe_url,
            data=data,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        doc = resp.json()
        return doc if isinstance(doc, dict) else None


async def build_authorization_url(
    base_url: str,
    state: str,
    tenant_id: str,
    nonce: str | None = None,
    code_verifier: str | None = None,
) -> str | None:
    """Construct the OIDC authorization redirect URL.

    F-021-09: ``nonce`` is included in the authorization request and bound to
    the SSO flow; the callback verifies it against the id_token's ``nonce``
    claim to defeat id_token replay.

    Bug-8142: when ``code_verifier`` is supplied the request carries the PKCE
    (RFC 7636) ``code_challenge`` + ``code_challenge_method=S256``. Only the
    S256 challenge — never the verifier — travels on the front-channel redirect;
    the verifier is replayed on the back-channel token exchange.
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
    if code_verifier:
        params["code_challenge"] = derive_code_challenge(code_verifier)
        params["code_challenge_method"] = "S256"
    authorize_endpoint = discovery["authorization_endpoint"]
    return f"{authorize_endpoint}?{urlencode(params)}"


async def exchange_code(
    base_url: str,
    code: str,
    expected_nonce: str | None = None,
    code_verifier: str | None = None,
) -> UserIdentity | None:
    """Exchange authorization code for tokens and extract identity.

    Bug-8142: when ``code_verifier`` is supplied it is replayed on the token
    request as the PKCE (RFC 7636 §4.5) ``code_verifier`` parameter. The
    authorization server recomputes ``S256(code_verifier)`` and rejects the
    exchange unless it matches the ``code_challenge`` sent at authorization
    time, so an intercepted authorization code is useless without the verifier.
    """
    cfg = get_oidc_config()
    if cfg is None:
        return None

    discovery = await _discover(cfg["issuer"])
    if discovery is None:
        return None

    callback_url = f"{base_url}/api/v1/auth/oidc/callback"
    token_endpoint = discovery["token_endpoint"]

    token_body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": callback_url,
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
    }
    if code_verifier:
        token_body["code_verifier"] = code_verifier

    # Bug-8142 TRAP: fail CLOSED on a genuine token-exchange failure (network,
    # timeout, non-2xx, non-JSON body) by returning None -> the callback audits
    # a critical sso_failure and answers 401. We catch ONLY those runtime
    # exchange failures. A programming error here (e.g. a malformed call with no
    # url or body) is NOT swallowed — it propagates and surfaces loudly rather
    # than masquerading as "auth failed", which is exactly how the ported-from
    # source silently disabled every OIDC login.
    # Bug-9302: the token POST uses the same SSRF-safe transport as discovery
    # and does not follow redirects to internal hosts.
    try:
        token_data = await _ssrf_post_form(token_endpoint, token_body)
    except (httpx.HTTPError, ValueError):
        logger.exception("OIDC token exchange failed")
        return None
    if token_data is None:
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

        # Bug-5942: validate registered time claims (OIDC Core §2 requires
        # exp; nbf/iat are optional but must be honoured when present).
        _CLOCK_SKEW = 120  # seconds tolerance for IdP/server clock drift
        now = time.time()
        exp = claims.get("exp")
        if exp is None:
            # OIDC Core §2 mandates exp; §3.1.3.7 step 9 requires the RP
            # to reject the token when the expiry check cannot be performed.
            # Fail closed: a token without exp is non-compliant.
            logger.warning("OIDC ID token missing required exp claim")
            return None
        if now > exp + _CLOCK_SKEW:
            logger.warning("OIDC ID token expired (exp=%s, now=%s)", exp, now)
            return None
        nbf = claims.get("nbf")
        if nbf is not None and now < nbf - _CLOCK_SKEW:
            logger.warning("OIDC ID token not yet valid (nbf=%s, now=%s)", nbf, now)
            return None
        iat = claims.get("iat")
        if iat is not None and now - iat > 86400 + _CLOCK_SKEW:
            logger.warning("OIDC ID token issued over 24h ago (iat=%s, now=%s)", iat, now)
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
    # F-021-04: record whether the IdP actually returned the groups claim so the
    # JIT reconciliation can tell "IdP omitted groups" (indeterminate, retain)
    # from "IdP returned an empty group set" (de-provisioning, revoke).
    groups_claim_present = groups_claim in claims
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
        groups_claim_present=groups_claim_present,
    )
