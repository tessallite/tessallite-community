"""Auth-gate regression for ``/diagnostics/query-rewrites``.

The endpoint surfaces distinct (raw_query, rewritten_query) pairs from
``query_logs`` so silent-drop bugs (Bug-102 — column-vs-column WHERE
dropped during rewrite) are catchable by inspection. It is tenant-scoped
and gated on ``require_tenant_admin``: ordinary members must be rejected.

Tests mint real JWTs against the configured signing key and hit the app
through ``httpx.ASGITransport``. The downstream DB call may fail (no
tenant DB is seeded for these auth-only tests) — that's fine, we only
assert the gate accepted/rejected the caller.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from jose import jwt

from shared.config.settings import get_settings

_settings = get_settings()


def _mint(role: str | None) -> str:
    payload: dict = {
        "sub": "user@example.com",
        "tenant_id": "t",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    if role:
        payload["role"] = role
    return jwt.encode(
        payload, _settings.JWT_SECRET_KEY, algorithm=_settings.JWT_ALGORITHM
    )


@pytest.fixture
async def client():
    from src.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as ac:
        yield ac


_PATH = "/api/v1/diagnostics/query-rewrites"


@pytest.mark.asyncio
async def test_query_rewrites_rejects_unauth(client):
    resp = await client.get(_PATH)
    assert resp.status_code in (401, 403), (
        f"{_PATH} allowed unauthenticated access (got {resp.status_code})"
    )


@pytest.mark.asyncio
async def test_query_rewrites_rejects_member(client):
    token = _mint(role="member")
    resp = await client.get(_PATH, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403, (
        f"{_PATH} allowed a member-role user (got {resp.status_code}; "
        "tenant_admin is required)"
    )


@pytest.mark.asyncio
async def test_query_rewrites_allows_tenant_admin(client):
    token = _mint(role="tenant_admin")
    resp = await client.get(_PATH, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code not in (401, 403), (
        f"{_PATH} rejected a tenant_admin (got {resp.status_code})"
    )
