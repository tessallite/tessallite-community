"""Unit tests for the GCP IAM auth backend using mocked google-auth."""
from __future__ import annotations

from unittest.mock import patch
import pytest


@pytest.fixture
def gcp_settings(monkeypatch):
    monkeypatch.setenv("GCP_IAM_AUDIENCE", "my-app.example.com")
    monkeypatch.setenv("GCP_IAM_ALLOWED_DOMAINS", "example.com")
    from shared.config.settings import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_gcp_iam_valid_token(gcp_settings):
    claims = {
        "email": "alice@example.com",
        "email_verified": True,
        "name": "Alice User",
        "sub": "12345",
        "hd": "example.com",
    }

    with patch("src.auth.gcp_iam_backend._verify_token_sync", return_value=claims):
        from src.auth.gcp_iam_backend import GcpIamAuthBackend
        backend = GcpIamAuthBackend()
        identity = await backend.authenticate(
            tenant_id="t1", email="alice@example.com", password="google-id-token"
        )

    assert identity is not None
    assert identity.email == "alice@example.com"
    assert identity.display_name == "Alice User"
    assert identity.source_backend == "gcp_iam"


@pytest.mark.asyncio
async def test_gcp_iam_wrong_domain(gcp_settings):
    claims = {
        "email": "alice@other.com",
        "email_verified": True,
        "name": "Alice User",
        "sub": "12345",
    }

    with patch("src.auth.gcp_iam_backend._verify_token_sync", return_value=claims):
        from src.auth.gcp_iam_backend import GcpIamAuthBackend
        backend = GcpIamAuthBackend()
        identity = await backend.authenticate(
            tenant_id="t1", email="alice@other.com", password="token"
        )

    assert identity is None


@pytest.mark.asyncio
async def test_gcp_iam_expired_token(gcp_settings):
    with patch("src.auth.gcp_iam_backend._verify_token_sync", side_effect=ValueError("Token expired")):
        from src.auth.gcp_iam_backend import GcpIamAuthBackend
        backend = GcpIamAuthBackend()
        identity = await backend.authenticate(
            tenant_id="t1", email="alice@example.com", password="expired"
        )

    assert identity is None


@pytest.mark.asyncio
async def test_gcp_iam_unverified_email(gcp_settings):
    claims = {
        "email": "alice@example.com",
        "email_verified": False,
        "name": "Alice User",
    }

    with patch("src.auth.gcp_iam_backend._verify_token_sync", return_value=claims):
        from src.auth.gcp_iam_backend import GcpIamAuthBackend
        backend = GcpIamAuthBackend()
        identity = await backend.authenticate(
            tenant_id="t1", email="alice@example.com", password="token"
        )

    assert identity is None


@pytest.mark.asyncio
async def test_gcp_iam_service_account(gcp_settings):
    claims = {
        "email": "svc@my-project.iam.gserviceaccount.com",
        "email_verified": True,
        "iss": "accounts.google.com",
        "sub": "svc-sub",
    }

    with patch("src.auth.gcp_iam_backend._verify_token_sync", return_value=claims):
        # Allow the service account domain
        from shared.config.settings import get_settings
        import os
        os.environ["GCP_IAM_ALLOWED_DOMAINS"] = "my-project.iam.gserviceaccount.com"
        get_settings.cache_clear()

        from src.auth.gcp_iam_backend import GcpIamAuthBackend
        backend = GcpIamAuthBackend()
        identity = await backend.authenticate(
            tenant_id="t1", email="svc@my-project.iam.gserviceaccount.com", password="token"
        )

    assert identity is not None
    assert identity.raw_claims["is_service_account"] is True


@pytest.mark.asyncio
async def test_gcp_iam_returns_none_when_unconfigured(monkeypatch):
    monkeypatch.setenv("GCP_IAM_AUDIENCE", "")
    from shared.config.settings import get_settings
    get_settings.cache_clear()
    try:
        from src.auth.gcp_iam_backend import GcpIamAuthBackend
        backend = GcpIamAuthBackend()
        identity = await backend.authenticate(
            tenant_id="t1", email="a@b.com", password="token"
        )
        assert identity is None
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_gcp_iam_timeout_returns_none(gcp_settings):
    """Bug-7324: a slow Google certificate fetch must not block the event loop
    indefinitely -- the timeout guard must return None on timeout."""
    import asyncio
    import time

    def _slow_verify(*args, **kwargs):
        time.sleep(0.5)
        return {"email": "alice@example.com", "email_verified": True, "name": "A"}

    with patch("src.auth.gcp_iam_backend._verify_token_sync", side_effect=_slow_verify):
        with patch("src.auth.gcp_iam_backend._VERIFY_TIMEOUT_SECONDS", 0.1):
            from src.auth.gcp_iam_backend import GcpIamAuthBackend
            backend = GcpIamAuthBackend()
            identity = await backend.authenticate(
                tenant_id="t1", email="alice@example.com", password="token"
            )

    assert identity is None, "Timed-out verification should return None"
