"""Gateway-level session revocation enforcement tests (Bug-7322 gateway half).

Verifies that the gateway rejects JWTs for deactivated users, demoted users,
and tokens with stale ``token_version`` claims by calling model-service's
``/api/v1/auth/users/me`` endpoint upstream.  The model-service side runs
``_validate_regular_session`` which checks is_active, role, token_version.

These tests exercise ``verify_jwt_token`` (signature/expiry) and
``validate_session_upstream`` (session-revocation) independently, then test
the combined flow via the gateway auth path.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from jose import jwt

from shared.config.settings import get_settings

from src.auth.base import (
    TokenPayload,
    _session_check_reset_for_tests,
    validate_session_upstream,
    verify_jwt_token,
)

pytestmark = pytest.mark.unit

settings = get_settings()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mint_jwt(
    sub: str = "user@example.com",
    tenant_id: str = "acme",
    role: str = "member",
    token_version: int = 0,
    expire_minutes: int = 60,
    **extra_claims: object,
) -> str:
    """Mint a test JWT with the same secret/algorithm as the gateway."""
    now = datetime.now(timezone.utc)
    payload: dict = {
        "sub": sub,
        "tenant_id": tenant_id,
        "iat": now,
        "exp": now + timedelta(minutes=expire_minutes),
        "role": role,
        "token_version": token_version,
        **extra_claims,
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def _mock_upstream_response(status_code: int, json_data: dict | None = None):
    """Create a mock httpx.Response for the model-service call."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    return resp


@pytest.fixture(autouse=True)
def _clear_session_check_cache():
    """Reset the per-token validation cache between tests."""
    _session_check_reset_for_tests()
    yield
    _session_check_reset_for_tests()


# ---------------------------------------------------------------------------
# verify_jwt_token: basic signature/expiry validation (unchanged)
# ---------------------------------------------------------------------------

class TestVerifyJwtToken:
    def test_valid_token_accepted(self):
        token = _mint_jwt()
        payload = verify_jwt_token(token)
        assert payload.sub == "user@example.com"
        assert payload.tenant_id == "acme"
        assert payload.extra.get("role") == "member"
        assert payload.extra.get("token_version") == 0

    def test_expired_token_rejected(self):
        token = _mint_jwt(expire_minutes=-1)
        with pytest.raises(ValueError, match="Invalid JWT"):
            verify_jwt_token(token)

    def test_missing_sub_rejected(self):
        now = datetime.now(timezone.utc)
        payload = {
            "tenant_id": "acme",
            "exp": now + timedelta(minutes=60),
        }
        token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
        with pytest.raises(ValueError, match="JWT missing 'sub' claim"):
            verify_jwt_token(token)

    def test_tampered_signature_rejected(self):
        token = _mint_jwt()
        # Tamper with the last character
        tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
        with pytest.raises(ValueError, match="Invalid JWT"):
            verify_jwt_token(tampered)

    def test_token_version_carried_in_extra(self):
        token = _mint_jwt(token_version=5)
        payload = verify_jwt_token(token)
        assert payload.extra["token_version"] == 5


# ---------------------------------------------------------------------------
# validate_session_upstream: model-service re-validation
# ---------------------------------------------------------------------------

class TestValidateSessionUpstream:
    @pytest.mark.asyncio
    async def test_active_session_accepted(self):
        """Model-service returns 200 -> session is valid."""
        token = _mint_jwt()
        mock_resp = _mock_upstream_response(200, {"email": "user@example.com"})
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.auth.base.httpx.AsyncClient", return_value=mock_client):
            # Should NOT raise
            await validate_session_upstream(token)

    @pytest.mark.asyncio
    async def test_deactivated_user_rejected(self):
        """Model-service returns 401 (user deactivated) -> session revoked."""
        token = _mint_jwt()
        mock_resp = _mock_upstream_response(401)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.auth.base.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ValueError, match="Session has been revoked"):
                await validate_session_upstream(token)

    @pytest.mark.asyncio
    async def test_stale_token_version_rejected(self):
        """Model-service returns 401 because token_version is stale."""
        # The token carries version 0, but model-service has bumped to 1
        # (password reset). _validate_regular_session in model-service
        # rejects the token and returns 401.
        token = _mint_jwt(token_version=0)
        mock_resp = _mock_upstream_response(401)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.auth.base.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ValueError, match="Session has been revoked"):
                await validate_session_upstream(token)

    @pytest.mark.asyncio
    async def test_demoted_user_rejected(self):
        """Model-service returns 401 because role was demoted."""
        token = _mint_jwt(role="tenant_admin")
        mock_resp = _mock_upstream_response(401)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.auth.base.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ValueError, match="Session has been revoked"):
                await validate_session_upstream(token)

    @pytest.mark.asyncio
    async def test_model_service_unreachable_fails_closed(self):
        """If model-service is down, reject the token (fail-closed)."""
        token = _mint_jwt()
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.auth.base.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ValueError, match="Session validation unavailable"):
                await validate_session_upstream(token)

    @pytest.mark.asyncio
    async def test_validation_cached_within_ttl(self):
        """A second call within TTL uses the cache, not model-service."""
        token = _mint_jwt()
        mock_resp = _mock_upstream_response(200, {"email": "user@example.com"})
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.auth.base.httpx.AsyncClient", return_value=mock_client):
            await validate_session_upstream(token)
            # Second call should NOT hit model-service
            await validate_session_upstream(token)

        # Only one HTTP call should have been made
        assert mock_client.get.call_count == 1

    @pytest.mark.asyncio
    async def test_validation_cache_expires(self):
        """After the TTL, a fresh upstream call is made."""
        token = _mint_jwt()
        mock_resp = _mock_upstream_response(200, {"email": "user@example.com"})
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.auth.base.httpx.AsyncClient", return_value=mock_client):
            await validate_session_upstream(token)

        # Expire the cache entry by patching time
        with patch("src.auth.base.time.monotonic", return_value=time.monotonic() + 60):
            with patch("src.auth.base.httpx.AsyncClient", return_value=mock_client):
                await validate_session_upstream(token)

        # Two HTTP calls total
        assert mock_client.get.call_count == 2

    @pytest.mark.asyncio
    async def test_rejected_token_not_cached(self):
        """A rejected token is not cached -- every attempt re-validates."""
        token = _mint_jwt()
        mock_resp_401 = _mock_upstream_response(401)
        mock_resp_200 = _mock_upstream_response(200, {"email": "user@example.com"})

        mock_client_reject = AsyncMock(spec=httpx.AsyncClient)
        mock_client_reject.get = AsyncMock(return_value=mock_resp_401)
        mock_client_reject.__aenter__ = AsyncMock(return_value=mock_client_reject)
        mock_client_reject.__aexit__ = AsyncMock(return_value=False)

        mock_client_accept = AsyncMock(spec=httpx.AsyncClient)
        mock_client_accept.get = AsyncMock(return_value=mock_resp_200)
        mock_client_accept.__aenter__ = AsyncMock(return_value=mock_client_accept)
        mock_client_accept.__aexit__ = AsyncMock(return_value=False)

        # First call: rejected
        with patch("src.auth.base.httpx.AsyncClient", return_value=mock_client_reject):
            with pytest.raises(ValueError, match="revoked"):
                await validate_session_upstream(token)

        # Second call with a new mock returning 200: should re-validate
        with patch("src.auth.base.httpx.AsyncClient", return_value=mock_client_accept):
            await validate_session_upstream(token)

    @pytest.mark.asyncio
    async def test_server_error_fails_closed(self):
        """A 500 from model-service rejects the token (fail-closed)."""
        token = _mint_jwt()
        mock_resp = _mock_upstream_response(500)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.auth.base.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ValueError, match="Session validation failed"):
                await validate_session_upstream(token)

    @pytest.mark.asyncio
    async def test_valid_active_token_works(self):
        """A valid, active token with matching token_version is accepted."""
        token = _mint_jwt(token_version=3)
        mock_resp = _mock_upstream_response(200, {
            "email": "user@example.com",
            "role": "member",
            "is_active": True,
            "token_version": 3,
        })
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.auth.base.httpx.AsyncClient", return_value=mock_client):
            # Should NOT raise
            await validate_session_upstream(token)

    @pytest.mark.asyncio
    async def test_calls_correct_endpoint(self):
        """validate_session_upstream calls GET /api/v1/auth/users/me."""
        token = _mint_jwt()
        mock_resp = _mock_upstream_response(200, {"email": "user@example.com"})
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.auth.base.httpx.AsyncClient", return_value=mock_client):
            await validate_session_upstream(token)

        call_args = mock_client.get.call_args
        url = call_args[0][0] if call_args[0] else call_args[1].get("url", "")
        assert "/api/v1/auth/users/me" in url
        headers = call_args[1].get("headers", {}) if len(call_args) > 1 else call_args[0][1] if len(call_args[0]) > 1 else {}
        assert f"Bearer {token}" in headers.get("Authorization", "")

    @pytest.mark.asyncio
    async def test_deleted_user_404_rejected(self):
        """A 404 from /users/me (user deleted) is rejected fail-closed."""
        token = _mint_jwt()
        mock_resp = _mock_upstream_response(404)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.auth.base.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ValueError, match="Session validation failed"):
                await validate_session_upstream(token)

    @pytest.mark.asyncio
    async def test_redirect_302_rejected(self):
        """A 302 redirect (proxy misconfiguration) must not be treated as
        a successful validation.  Only an explicit 200 is accepted."""
        token = _mint_jwt()
        mock_resp = _mock_upstream_response(302)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.auth.base.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ValueError, match="Session validation failed"):
                await validate_session_upstream(token)

    @pytest.mark.asyncio
    async def test_redirect_301_rejected(self):
        """A 301 redirect is rejected fail-closed."""
        token = _mint_jwt()
        mock_resp = _mock_upstream_response(301)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.auth.base.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ValueError, match="Session validation failed"):
                await validate_session_upstream(token)
