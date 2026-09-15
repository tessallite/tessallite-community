"""Bug-9946 guards for typed tenant readiness across authentication APIs."""
from __future__ import annotations

import httpx
import pytest
from unittest.mock import AsyncMock

from shared.db.tenant_readiness import TenantReadinessError
from src.auth.chain import PatTerminalAuthChain
from src.main import app

pytestmark = pytest.mark.unit


def _error(operation: str = "tenant login") -> TenantReadinessError:
    return TenantReadinessError(
        tenant_slug="beta",
        operation=operation,
        cause="tenant schema revision is older than the service",
        current_revision="0222",
        required_revision="0223",
    )


@pytest.mark.asyncio
async def test_direct_login_returns_typed_503_for_unavailable_tenant(monkeypatch):
    async def system_db():
        yield AsyncMock()

    class _Chain:
        async def authenticate(self, **_kwargs):
            raise _error("tenant login")

    monkeypatch.setattr("src.api.auth.get_system_db", system_db)
    monkeypatch.setattr("src.api.auth.assert_not_locked", AsyncMock())
    monkeypatch.setattr("src.api.auth.get_auth_chain", lambda: _Chain())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"tenant_id": "beta", "email": "user@example.test", "password": "pw"},
        )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["condition"] == "tenant_database_unavailable"
    assert detail["tenant_slug"] == "beta"
    assert detail["current_revision"] == "0222"
    assert detail["required_revision"] == "0223"


@pytest.mark.asyncio
async def test_discovery_does_not_convert_tenant_readiness_to_generic_auth_error(
    monkeypatch,
):
    class _Tenant:
        slug = "beta"

    class _ScalarResult:
        def all(self):
            return [_Tenant()]

    class _Result:
        def scalars(self):
            return _ScalarResult()

    sys_db = AsyncMock()
    sys_db.execute = AsyncMock(return_value=_Result())

    async def system_db():
        yield sys_db

    async def broken_tenant(_slug):
        raise _error("tenant discovery login")
        yield  # pragma: no cover - keeps this an async generator

    monkeypatch.setattr("src.api.auth.get_system_db", system_db)
    monkeypatch.setattr("src.api.auth.get_tenant_db", broken_tenant)
    monkeypatch.setattr("src.api.auth.assert_not_locked", AsyncMock())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/auth/login/discover",
            json={"tenant_id": "beta", "email": "user@example.test", "password": "pw"},
        )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["condition"] == "tenant_database_unavailable"
    assert detail["operation"] == "tenant discovery login"


@pytest.mark.asyncio
async def test_pat_terminal_chain_preserves_tenant_readiness_503():
    class _BrokenPAT:
        name = "pat"

        async def authenticate(self, **_kwargs):
            raise _error("PAT login")

    chain = PatTerminalAuthChain([_BrokenPAT()])
    with pytest.raises(TenantReadinessError) as raised:
        await chain.authenticate(
            tenant_id="beta",
            email="user@example.test",
            password="tesspat_abcdef012345_abcDEF-_0",
        )
    assert raised.value.status_code == 503
    assert raised.value.detail["condition"] == "tenant_database_unavailable"


@pytest.mark.asyncio
async def test_sso_overlay_preserves_tenant_readiness_503(monkeypatch):
    async def broken_tenant(_slug):
        raise _error("SSO overlay load")
        yield  # pragma: no cover - keeps this an async generator

    # sso_overlay imports the shared dependency inside the loader to avoid a
    # model-service import cycle, so patch the actual shared boundary.
    monkeypatch.setattr("shared.db.session.get_tenant_db", broken_tenant)
    from src.auth.sso_overlay import load_tenant_overlay

    with pytest.raises(TenantReadinessError) as raised:
        await load_tenant_overlay("beta")
    assert raised.value.status_code == 503
    assert raised.value.detail["condition"] == "tenant_database_unavailable"
