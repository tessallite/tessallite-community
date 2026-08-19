"""GCP IAM auth backend — Google Identity Token (OIDC) validation.

The client sends a Google ID token as the ``password`` field in the
login request.  This backend validates the token's signature, audience,
issuer, and optional domain restriction.

Flow:
1. Verify the ID token with Google's public keys (``google.auth``).
2. Check ``aud`` matches ``GCP_IAM_AUDIENCE``.
3. Optionally restrict to configured domains (``GCP_IAM_ALLOWED_DOMAINS``).
4. Return a ``UserIdentity`` with the token's email and name claims.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from shared.auth.backend import UserIdentity
from shared.config.settings import get_settings

logger = logging.getLogger(__name__)

try:
    from google.oauth2 import id_token as _google_id_token
    from google.auth.transport import requests as _google_requests
except ImportError:
    _google_id_token = None  # type: ignore[assignment]
    _google_requests = None  # type: ignore[assignment]

# Bug-7324: hard ceiling on the time a single Google certificate fetch
# may block.  Applied inside the thread wrapper so a stalled network
# call cannot pin the worker indefinitely.
_VERIFY_TIMEOUT_SECONDS = 10.0


class _TimeoutBoundRequest:
    """Wrapper around ``google.auth.transport.requests.Request`` that forces
    a network-level timeout on every HTTP call (AUTH-RR-04).

    The default google-auth transport uses ``timeout=120`` inside its
    ``__call__``; this wrapper overrides that so the thread terminates
    promptly on network hangs rather than relying solely on the outer
    ``asyncio.wait_for`` (which cannot cancel a running synchronous
    thread).
    """

    def __init__(self, timeout: float) -> None:
        self._inner = _google_requests.Request()
        self._timeout = timeout

    def __call__(self, url, method="GET", body=None, headers=None,
                 timeout=None, **kwargs):
        return self._inner(
            url, method=method, body=body, headers=headers,
            timeout=self._timeout, **kwargs,
        )


def _verify_token_sync(token: str, audience: str) -> dict | None:
    """Synchronous token verification -- runs inside a thread (Bug-7324).

    Uses a transport-level timeout on every underlying HTTP call so a
    stalled certificate fetch terminates the thread itself rather than
    relying solely on the outer asyncio ``wait_for`` cancellation
    (AUTH-RR-04: asyncio cancel cannot stop a running synchronous thread).
    """
    if _google_id_token is None or _google_requests is None:
        logger.error("google-auth package not installed")
        return None
    transport = _TimeoutBoundRequest(timeout=_VERIFY_TIMEOUT_SECONDS)
    return _google_id_token.verify_oauth2_token(
        token, transport, audience,
    )


class GcpIamAuthBackend:
    name: str = "gcp_iam"

    def __init__(self) -> None:
        s = get_settings()
        self._audience = s.GCP_IAM_AUDIENCE
        domains_raw = s.GCP_IAM_ALLOWED_DOMAINS
        self._allowed_domains: list[str] = [
            d.strip().lower()
            for d in domains_raw.split(",")
            if d.strip()
        ]

    async def authenticate(
        self, *, tenant_id: str, email: str, password: str, **kwargs: Any
    ) -> UserIdentity | None:
        if not self._audience:
            return None

        try:
            # Bug-7324: run the synchronous Google SDK verification in a
            # thread so it does not block the FastAPI event loop.  A slow
            # certificate fetch would otherwise stall all concurrent
            # requests handled by the same worker.
            claims = await asyncio.wait_for(
                asyncio.to_thread(_verify_token_sync, password, self._audience),
                timeout=_VERIFY_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "GCP IAM token verification timed out after %.0fs",
                _VERIFY_TIMEOUT_SECONDS,
            )
            return None
        except Exception as exc:
            logger.debug("GCP IAM token verification failed: %s", exc)
            return None

        if claims is None:
            return None

        token_email = claims.get("email", "")
        if not token_email:
            return None

        if not claims.get("email_verified", False):
            logger.info("GCP IAM: email %s not verified", token_email)
            return None

        if self._allowed_domains:
            domain = token_email.rsplit("@", 1)[-1].lower()
            if domain not in self._allowed_domains:
                logger.info(
                    "GCP IAM: domain %s not in allowed list %s",
                    domain, self._allowed_domains,
                )
                return None

        is_service_account = (
            claims.get("iss") == "accounts.google.com"
            and token_email.endswith(".iam.gserviceaccount.com")
        )

        groups: list[str] = []
        raw_groups = claims.get("groups")
        if isinstance(raw_groups, list):
            groups = [str(g) for g in raw_groups if g]
        hd = claims.get("hd")
        if hd and str(hd) not in groups:
            groups.append(str(hd))

        return UserIdentity(
            email=token_email,
            display_name=claims.get("name", ""),
            groups=groups,
            source_backend=self.name,
            raw_claims={
                "sub": claims.get("sub", ""),
                "hd": claims.get("hd", ""),
                "is_service_account": is_service_account,
            },
        )
