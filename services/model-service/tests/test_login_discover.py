"""Phase 3 (CR-002 Finding 3) — login_discover determinism and logging.

login_discover resolves the candidate user with an indexed per-tenant email
lookup in slug order, then runs bcrypt EXACTLY ONCE against the single matched
user (F-021-07 — no O(tenants) bcrypt fan-out). Behaviour preserved:

- Tenants are iterated in slug order (ORDER BY slug).
- Operational errors (non-credential exceptions) during the email lookup are
  logged WARN with the tenant slug and the loop continues.
- Multiple tenants matching the same email log a WARN listing all matched
  slugs and return the first-by-slug.
- No matching email returns 401 (after one dummy bcrypt to keep timing flat).
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.main import app
from src.auth.local_backend import decode_access_token

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _make_user(email: str, role: str = "member"):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        username="u",
        email=email,
        is_active=True,
        hashed_password="$2b$12$fakehash",
        role=role,
        created_at=NOW,
    )


def _make_tenant(slug: str):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        slug=slug,
        display_name=slug,
        db_schema_prefix=slug,
        is_active=True,
        encrypted_db_url=b"",
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
async def anon_client():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as ac:
        yield ac


def _install_system_tenants(monkeypatch, tenants: list):
    class _Result:
        def scalars(self):
            class _S:
                def __init__(self, xs):
                    self._xs = xs

                def all(self_inner):
                    return self_inner._xs
            return _S(tenants)

    class _SysDB:
        async def execute(self, stmt):
            return _Result()

    async def _gen():
        yield _SysDB()

    monkeypatch.setattr("src.api.auth.get_system_db", lambda: _gen())


def _install_tenant_lookup(monkeypatch, user_for_slug):
    """Patch get_tenant_db so each tenant's db.execute returns the user (or
    None) for that slug via ``user_for_slug(slug)``. A returned exception type
    is raised to simulate an operational failure."""
    async def fake_get_tenant_db(slug):
        outcome = user_for_slug(slug)

        class _UserResult:
            def scalar_one_or_none(self_inner):
                return outcome

        class _DB:
            _slug = slug

            async def execute(self_inner, stmt):
                if isinstance(outcome, Exception):
                    raise outcome
                return _UserResult()

        yield _DB()

    monkeypatch.setattr("src.api.auth.get_tenant_db", fake_get_tenant_db)


@pytest.mark.asyncio
async def test_login_discover_returns_first_match_by_slug(anon_client, monkeypatch):
    tenants = [_make_tenant("acme"), _make_tenant("beta"), _make_tenant("gamma")]
    _install_system_tenants(monkeypatch, tenants)
    _install_tenant_lookup(
        monkeypatch,
        lambda slug: _make_user("u@example.com", role="tenant_admin") if slug == "beta" else None,
    )

    with patch("src.api.auth.verify_password", return_value=True):
        resp = await anon_client.post(
            "/api/v1/auth/login/discover",
            json={"tenant_id": "_discover", "email": "u@example.com", "password": "pw"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "tenant_admin"
    token = resp.cookies.get("access_token")
    assert token, "access_token cookie must be set"
    payload = decode_access_token(token)
    assert payload["tenant_id"] == "beta"


@pytest.mark.asyncio
async def test_login_discover_picks_first_by_slug_when_multiple_match(
    anon_client, monkeypatch, caplog
):
    import logging

    tenants = [_make_tenant("acme"), _make_tenant("beta")]
    _install_system_tenants(monkeypatch, tenants)
    _install_tenant_lookup(monkeypatch, lambda slug: _make_user("u@example.com", role="member"))

    with (
        patch("src.api.auth.verify_password", return_value=True),
        caplog.at_level(logging.WARNING, logger="src.api.auth"),
    ):
        resp = await anon_client.post(
            "/api/v1/auth/login/discover",
            json={"tenant_id": "_discover", "email": "u@example.com", "password": "pw"},
        )

    assert resp.status_code == 200
    payload = decode_access_token(resp.cookies.get("access_token"))
    assert payload["tenant_id"] == "acme"
    collision_logs = [
        r for r in caplog.records if "matched more than one tenant" in r.getMessage()
    ]
    assert collision_logs
    msg = collision_logs[0].getMessage()
    assert "acme" in msg and "beta" in msg


@pytest.mark.asyncio
async def test_login_discover_logs_operational_failure_and_continues(
    anon_client, monkeypatch, caplog
):
    import logging

    tenants = [_make_tenant("broken"), _make_tenant("healthy")]
    _install_system_tenants(monkeypatch, tenants)
    _install_tenant_lookup(
        monkeypatch,
        lambda slug: RuntimeError("simulated DB outage") if slug == "broken"
        else _make_user("u@example.com", role="member"),
    )

    with (
        patch("src.api.auth.verify_password", return_value=True),
        caplog.at_level(logging.WARNING, logger="src.api.auth"),
    ):
        resp = await anon_client.post(
            "/api/v1/auth/login/discover",
            json={"tenant_id": "_discover", "email": "u@example.com", "password": "pw"},
        )

    assert resp.status_code == 200
    payload = decode_access_token(resp.cookies.get("access_token"))
    assert payload["tenant_id"] == "healthy"
    op_logs = [r for r in caplog.records if "raised RuntimeError" in r.getMessage()]
    assert op_logs
    assert "broken" in op_logs[0].getMessage()


@pytest.mark.asyncio
async def test_login_discover_no_match_returns_401(anon_client, monkeypatch):
    tenants = [_make_tenant("alpha"), _make_tenant("beta")]
    _install_system_tenants(monkeypatch, tenants)
    _install_tenant_lookup(monkeypatch, lambda slug: None)

    # verify_password is still called once (dummy hash) to keep timing flat.
    with patch("src.api.auth.verify_password", return_value=False) as vp:
        resp = await anon_client.post(
            "/api/v1/auth/login/discover",
            json={"tenant_id": "_discover", "email": "u@example.com", "password": "pw"},
        )
    assert resp.status_code == 401
    assert vp.call_count == 1, "exactly one bcrypt verify even on no match (F-021-07)"


@pytest.mark.asyncio
async def test_login_discover_runs_bcrypt_once_on_match(anon_client, monkeypatch):
    """F-021-07: even with the email present in several tenants, bcrypt runs
    exactly once (against the first-by-slug match), not once per tenant."""
    tenants = [_make_tenant("a"), _make_tenant("b"), _make_tenant("c")]
    _install_system_tenants(monkeypatch, tenants)
    _install_tenant_lookup(monkeypatch, lambda slug: _make_user("u@example.com"))

    with patch("src.api.auth.verify_password", return_value=True) as vp:
        resp = await anon_client.post(
            "/api/v1/auth/login/discover",
            json={"tenant_id": "_discover", "email": "u@example.com", "password": "pw"},
        )
    assert resp.status_code == 200
    assert vp.call_count == 1, "bcrypt must run once, not once per matching tenant"
