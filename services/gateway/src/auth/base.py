"""
Auth backend interface and JWT validation for the gateway service.

Primary authentication: JWT issued by model-service.
Google IAM and LDAP are scaffolded but disabled in V1.

Bug-7322 (gateway consumer half): session-revocation enforcement.
The model-service bumps ``token_version`` on deactivation, role change,
email change, and password reset.  ``verify_jwt_token`` validates the JWT
signature and expiry, but that is not enough: the gateway must also confirm
the session has not been revoked (user deactivated, demoted, or
token_version stale).  Because the gateway has no direct DB access
(gateway-only-data-access invariant), it calls the model-service
``/api/v1/auth/users/me`` endpoint, which runs ``_validate_regular_session``
internally.  A short per-token validation cache avoids hitting model-service
on every single BI request.
"""
from __future__ import annotations

import base64
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

import httpx
from jose import JWTError, jwt

from shared.config.settings import get_settings
from shared.middleware.internal_bypass import internal_request_headers

logger = logging.getLogger(__name__)

settings = get_settings()

# ---------------------------------------------------------------------------
# Session-revocation validation cache (Bug-7322)
# ---------------------------------------------------------------------------
# Per-token cache of upstream validation results.  Keyed by the raw JWT
# string; value is the monotonic timestamp of the last successful upstream
# validation.  Entries older than ``_SESSION_CHECK_TTL_SECONDS`` trigger a
# fresh upstream call.  A REJECTED token is never cached — it raises on every
# attempt until a new (valid) JWT is presented.
#
# The TTL defaults to 30 s — the same order as the XMLA credential cache so
# a deactivated account is blocked within one burst window.  It is
# configurable via the ``GATEWAY_SESSION_CHECK_TTL`` environment variable
# (integer seconds) for operational tuning.  Hard-capped at 300 s (5 min) so
# a misconfiguration cannot keep revoked sessions alive for arbitrarily long.
_SESSION_CHECK_TTL_MAX = 300
_SESSION_CHECK_TTL_SECONDS = min(
    int(os.environ.get("GATEWAY_SESSION_CHECK_TTL", "30")),
    _SESSION_CHECK_TTL_MAX,
)
_session_check_lock = threading.Lock()
_session_check_cache: dict[str, float] = {}

# Hard cap on cache entries.  Each entry is one JWT string (~1 KB) + a float
# timestamp, so 10 000 entries ≈ 10 MB worst case.  When the cap is reached,
# evict the oldest entry (simple sweep, not LRU — the cache is a burst
# de-duplicator, not a hot-path data structure).
_SESSION_CHECK_MAX_ENTRIES = 10_000


def _session_check_ok(token: str) -> bool:
    """Return True if *token* was validated within the TTL window."""
    now = time.monotonic()
    with _session_check_lock:
        ts = _session_check_cache.get(token)
        if ts is None:
            return False
        if (now - ts) >= _SESSION_CHECK_TTL_SECONDS:
            _session_check_cache.pop(token, None)
            return False
        return True


def _session_check_record(token: str) -> None:
    """Record a successful upstream validation for *token*.

    If the cache has reached its size cap, sweep expired entries first; if
    still over cap, evict the oldest entry.  This bounds memory regardless of
    how many distinct tokens the gateway sees.
    """
    now = time.monotonic()
    with _session_check_lock:
        if len(_session_check_cache) >= _SESSION_CHECK_MAX_ENTRIES:
            # Sweep expired entries.
            expired = [
                k for k, ts in _session_check_cache.items()
                if (now - ts) >= _SESSION_CHECK_TTL_SECONDS
            ]
            for k in expired:
                _session_check_cache.pop(k, None)
        if len(_session_check_cache) >= _SESSION_CHECK_MAX_ENTRIES:
            # Still at cap: evict the single oldest entry.
            oldest_key = min(_session_check_cache, key=_session_check_cache.get)  # type: ignore[arg-type]
            _session_check_cache.pop(oldest_key, None)
        _session_check_cache[token] = now


def _session_check_evict(token: str) -> None:
    """Evict *token* from the validation cache (revoked/invalid)."""
    with _session_check_lock:
        _session_check_cache.pop(token, None)


def _session_check_reset_for_tests() -> None:
    """Clear the validation cache (test isolation)."""
    with _session_check_lock:
        _session_check_cache.clear()


# ---------------------------------------------------------------------------
# Token payload
# ---------------------------------------------------------------------------

@dataclass
class TokenPayload:
    sub: str          # user email or user_id
    tenant_id: str
    exp: int
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Base64url canonical encoding validation
# ---------------------------------------------------------------------------
# Module-level decode table (built once, reused on every call).
_B64URL_DECODE: list[int] = [-1] * 256
for _b, _c in enumerate(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"):
    _B64URL_DECODE[_c] = _b


# ---------------------------------------------------------------------------
# JWT validation (used by both JDBC and XMLA paths)
# ---------------------------------------------------------------------------

def _validate_base64url_canonical(token: str) -> None:
    """Reject JWTs with non-canonical base64url encoding.

    Base64url uses 6 bits per character.  When the encoded byte count is
    not a multiple of 3, the last base64url character contains 2 or 4
    padding bits that MUST be zero in a canonically-encoded token.  The
    ``jose`` library decodes and compares the raw HMAC bytes, but it does
    NOT enforce canonical encoding.  That means two different token
    strings (differing only in the padding bits of the last character)
    can decode to the same HMAC value and both pass signature
    verification -- an attacker who flips a padding bit produces a
    visually different token that ``jose`` still accepts.

    This pre-check rejects any token whose base64url parts have non-zero
    padding bits, closing the gap before ``jose.jwt.decode`` runs.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT: expected 3 dot-separated parts")

    for part in parts:
        if not part:
            raise ValueError("Invalid JWT: empty segment")

        # Validate all characters are valid base64url
        for ch in part:
            if _B64URL_DECODE[ord(ch)] < 0 if ord(ch) < 256 else True:
                raise ValueError("Invalid JWT: non-base64url character")

        # Check canonical padding bits on the last character.
        # Number of encoded bytes = ceil(len(part) * 6 / 8) when decoding;
        # the remainder determines how many trailing bits are padding:
        #   len % 4 == 0 -> 0 padding bits
        #   len % 4 == 1 -> invalid (can't represent whole bytes)
        #   len % 4 == 2 -> 4 padding bits (last char upper 2 bits used)
        #   len % 4 == 3 -> 2 padding bits (last char upper 4 bits used)
        remainder = len(part) % 4
        if remainder == 1:
            raise ValueError("Invalid JWT: invalid base64url segment length")

        if remainder in (2, 3):
            last_val = _B64URL_DECODE[ord(part[-1])]
            padding_bits = 4 if remainder == 2 else 2
            mask = (1 << padding_bits) - 1
            if last_val & mask:
                raise ValueError(
                    "Invalid JWT: non-canonical base64url encoding"
                )


def verify_jwt_token(token: str) -> TokenPayload:
    """
    Validate a JWT token signed by model-service.

    Raises:
        ValueError on invalid signature, expiry, or missing claims.
    """
    # Defence-in-depth: reject non-canonical base64url BEFORE the HMAC
    # check so that padding-bit-only mutations cannot slip through the
    # jose library's byte-level signature comparison.
    _validate_base64url_canonical(token)

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
# Upstream session-revocation check (Bug-7322)
# ---------------------------------------------------------------------------

async def validate_session_upstream(token: str) -> None:
    """Confirm that the JWT session is still valid by calling model-service.

    Calls ``GET /api/v1/auth/users/me`` with the token.  Model-service runs
    ``_validate_regular_session`` (checks is_active, role, token_version)
    before returning.  A 401 response means the session has been revoked.

    Results are cached for ``_SESSION_CHECK_TTL_SECONDS`` seconds so the
    gateway does not hit model-service on every BI request.

    System-admin tokens (``tenant_id == "__system__"``) are validated but
    tolerate a model-service outage: the system-admin validation path does
    not require a tenant DB lookup, but we still attempt the call for
    defence-in-depth.

    Raises:
        ValueError — session revoked, user deactivated/demoted, or
            token_version stale.
    """
    if _session_check_ok(token):
        return

    url = f"{settings.MODEL_SERVICE_URL}/api/v1/auth/users/me"
    headers = {
        "Authorization": f"Bearer {token}",
        **internal_request_headers(),
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            resp = await client.get(url, headers=headers)
    except Exception as exc:
        # Fail-closed: if model-service is unreachable, reject the token.
        # A freshly issued token (within the TTL window) will have been
        # cached and served from cache above, so a brief model-service
        # blip only affects tokens that have not been validated recently.
        logger.warning(
            "Session validation unavailable (model-service unreachable): %s", exc,
        )
        _session_check_evict(token)
        raise ValueError("Session validation unavailable") from exc

    if resp.status_code == 401:
        logger.info("Session revoked by model-service (401)")
        _session_check_evict(token)
        raise ValueError("Session has been revoked")

    if resp.status_code != 200:
        # Fail-closed on EVERY non-200 response: 3xx redirects (proxy
        # misconfiguration), 403 (embed token), 404 (deleted user), 5xx.
        # Only an explicit 200 from model-service proves the session is live.
        logger.warning(
            "Session validation returned HTTP %d; rejecting token", resp.status_code,
        )
        _session_check_evict(token)
        raise ValueError(f"Session validation failed (HTTP {resp.status_code})")

    # Success: cache the validation result.
    _session_check_record(token)
    logger.debug("Session validated upstream for %s", _safe_sub(token))


def _safe_sub(token: str) -> str:
    """Extract 'sub' from a JWT for logging without re-raising on error."""
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={"verify_exp": False},
        )
        return payload.get("sub", "<unknown>")
    except Exception:
        return "<invalid>"


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
