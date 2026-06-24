"""Tests for the system settings API.

Covers:
  - GET /system/settings returns one item per registered system key
  - PUT /system/settings/{key} validates input and rejects unknown keys
  - PUT rejects bootstrap (env-only) keys
  - GET /system/settings/bootstrap masks sensitive values
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from shared.config.registry import surfaced_for_level
from src.main import app
from src.auth.middleware import CurrentUser, require_system_admin

pytestmark = pytest.mark.unit


def _system_admin() -> CurrentUser:
    return CurrentUser(
        user_id="sysadmin@example.com",
        tenant_id="_system",
        email="sysadmin@example.com",
        role="system_admin",
    )


def _mock_system_db(scalar_value=None):
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar_value
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.add = MagicMock()
    return db


@pytest.fixture
def admin_client():
    app.dependency_overrides[require_system_admin] = lambda: _system_admin()
    yield
    app.dependency_overrides.pop(require_system_admin, None)


@pytest.fixture
async def http_client(admin_client):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# GET /system/settings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_returns_every_surfaced_system_key(http_client):
    mock_db = _mock_system_db(None)
    async def _gen():
        yield mock_db

    with patch("shared.db.session.get_system_db", _gen):
        resp = await http_client.get("/api/v1/system/settings")

    assert resp.status_code == 200
    body = resp.json()
    keys_returned = {it["key"] for it in body["items"]}
    expected = {d.key for d in surfaced_for_level("system")}
    assert keys_returned == expected
    # Sanity: legacy/operational keys must not leak into the public listing.
    assert "xmla.session_ttl_seconds" not in keys_returned
    assert "llm.provider_endpoints" not in keys_returned
    assert "source_db.fallback_host" not in keys_returned


# ---------------------------------------------------------------------------
# PUT /system/settings/{key}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_put_unknown_key_returns_404(http_client):
    mock_db = _mock_system_db(None)
    async def _gen():
        yield mock_db

    with patch("shared.db.session.get_system_db", _gen):
        resp = await http_client.put(
            "/api/v1/system/settings/totally.not.a.key",
            json={"value": 1},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_put_invalid_value_returns_400(http_client):
    mock_db = _mock_system_db(None)
    async def _gen():
        yield mock_db

    with patch("shared.db.session.get_system_db", _gen):
        resp = await http_client.put(
            "/api/v1/system/settings/auth.jwt_expire_minutes",
            json={"value": -1},
        )
    assert resp.status_code == 400
    assert "positive integer" in resp.text


@pytest.mark.asyncio
async def test_put_env_var_key_returns_400(http_client):
    """meta.bootstrap_env_view is read-only — setting it must be rejected."""
    mock_db = _mock_system_db(None)
    async def _gen():
        yield mock_db

    with patch("shared.db.session.get_system_db", _gen):
        resp = await http_client.put(
            "/api/v1/system/settings/meta.bootstrap_env_view",
            json={"value": {}},
        )
    assert resp.status_code == 400
    assert "read-only" in resp.text


# ---------------------------------------------------------------------------
# GET /system/settings/bootstrap
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bootstrap_masks_secrets(http_client):
    resp = await http_client.get("/api/v1/system/settings/bootstrap")
    assert resp.status_code == 200
    body = resp.json()

    by_name = {it["name"]: it for it in body}
    # Secrets must be masked
    assert "*****" in by_name["JWT_SECRET_KEY"]["value"]
    assert "*****" in by_name["CREDENTIAL_ENCRYPTION_KEY"]["value"]
    assert "*****" in by_name["SYSTEM_ADMIN_PASSWORD"]["value"]
    # Non-secrets are visible
    assert by_name["JDBC_PORT"]["sensitive"] is False
    assert by_name["JWT_ALGORITHM"]["value"] == "HS256"
    # DSN password is redacted but user/host visible
    assert "*****" in by_name["SYSTEM_DATABASE_URL"]["value"]
    assert by_name["SYSTEM_DATABASE_URL"]["value"].startswith("postgresql+asyncpg://tessallite:")
