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


def _verify_token(token: str, audience: str) -> dict | None:
    if _google_id_token is None or _google_requests is None:
        logger.error("google-auth package not installed")
        return None
    return _google_id_token.verify_oauth2_token(
        token, _google_requests.Request(), audience,
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
            claims = _verify_token(password, self._audience)
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

        return UserIdentity(
            email=token_email,
            display_name=claims.get("name", ""),
            groups=[],
            source_backend=self.name,
            raw_claims={
                "sub": claims.get("sub", ""),
                "hd": claims.get("hd", ""),
                "is_service_account": is_service_account,
            },
        )
