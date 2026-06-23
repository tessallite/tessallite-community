"""Endpoint accessibility — verify every registered route is reachable and does not crash."""
from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.routing import APIRoute

from src.main import app
from shared.auth.middleware import (
    CurrentUser,
    get_current_user,
    require_system_admin,
    require_tenant_admin,
)

_DUMMY_UUID = "00000000-0000-4000-8000-000000000001"
_TEST_TENANT = "test-tenant"
_SKIP_METHODS = {"HEAD", "OPTIONS"}

_PATH_SUBS: dict[str, str] = {
    "model_id": _DUMMY_UUID,
    "measure_id": _DUMMY_UUID,
    "key": "test-key",
}


def _resolve_path(path: str) -> str:
    def _sub(m: re.Match) -> str:
        name = m.group(1)
        if name in _PATH_SUBS:
            return _PATH_SUBS[name]
        if "id" in name:
            return _DUMMY_UUID
        return "test-value"

    return re.sub(r"\{(\w+)\}", _sub, path)


def _collect_routes():
    seen: set[str] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in sorted(route.methods - _SKIP_METHODS):
            key = f"{method} {route.path}"
            if key not in seen:
                seen.add(key)
                yield pytest.param(method, route.path, id=key)


def _make_mock_session() -> AsyncMock:
    s = AsyncMock()
    s.add = MagicMock()
    s.flush = AsyncMock()
    s.commit = AsyncMock()
    s.delete = AsyncMock()
    s.rollback = AsyncMock()

    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.scalar.return_value = None
    result.scalars.return_value.all.return_value = []
    result.scalars.return_value.first.return_value = None
    result.all.return_value = []
    result.first.return_value = None
    s.execute = AsyncMock(return_value=result)
    s.get = AsyncMock(return_value=None)
    return s


@pytest.fixture(autouse=True)
def _admin_auth():
    user = CurrentUser(
        user_id="admin@test.com",
        tenant_id=_TEST_TENANT,
        email="admin@test.com",
        role="system_admin",
    )
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_system_admin] = lambda: user
    app.dependency_overrides[require_tenant_admin] = lambda: user
    yield
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(require_system_admin, None)
    app.dependency_overrides.pop(require_tenant_admin, None)


@pytest.fixture(autouse=True)
def _mock_handler_db():
    mock_session = _make_mock_session()

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_cm)

    async def _fake_get_factory(tenant_id: str):
        return mock_factory

    with (
        patch(
            "shared.db.session.get_tenant_session_factory",
            _fake_get_factory,
        ),
        patch("shared.db.session.SystemSessionLocal", mock_factory),
    ):
        yield mock_session


@pytest.fixture
async def client():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as ac:
        yield ac


@pytest.mark.parametrize("method,path", list(_collect_routes()))
@pytest.mark.asyncio
async def test_endpoint_accessible(client, method, path):
    url = _resolve_path(path)
    kwargs: dict = {}
    if method in ("POST", "PUT", "PATCH"):
        kwargs["json"] = {}
    resp = await client.request(method, url, **kwargs)
    assert resp.status_code < 500, (
        f"{method} {url} -> {resp.status_code}: {resp.text[:300]}"
    )
