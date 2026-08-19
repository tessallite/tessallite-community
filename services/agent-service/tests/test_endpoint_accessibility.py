"""Endpoint accessibility — verify every registered route is reachable and does not crash."""
from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.routing import Route, WebSocketRoute

from src.main import app
from src.auth.middleware import (
    CurrentUser,
    get_current_user,
    require_system_admin,
    require_tenant_admin,
)

_DUMMY_UUID = "00000000-0000-4000-8000-000000000001"
_TEST_TENANT = "test-tenant"
_SKIP_METHODS = {"HEAD", "OPTIONS"}

# Floor for Bug-8448: the recursive walker below must find at least this many
# real APIRoutes. Well under the ~51 the service actually registers, but far
# above zero, so a FastAPI upgrade that hides included routers (0.139+ wraps
# them in an opaque container the old flat scan could not see) makes this suite
# FAIL CLOSED instead of passing on an empty parameter set.
_MIN_ROUTES = 40

_INFRA_ROUTES: set[str] = {
    "POST /api/v1/projects/{project_id}/agent/webhook/rotate-secret",
}

_PATH_SUBS: dict[str, str] = {
    "project_id": _DUMMY_UUID,
    "conversation_id": _DUMMY_UUID,
    "persona_id": _DUMMY_UUID,
    "recipe_id": _DUMMY_UUID,
    "rubric_id": _DUMMY_UUID,
    "dlq_id": _DUMMY_UUID,
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


class _UnknownRouteShape(TypeError):
    """A route object the walker does not recognise. Raised so the walker
    FAILS CLOSED on an un-enumerated shape instead of silently skipping it."""


# Non-API leaf routes the accessibility property does not cover: the
# docs / openapi / redoc endpoints Starlette mounts, and websockets.
_IGNORED_LEAF = (Route, WebSocketRoute, APIWebSocketRoute)


def _walk(router):
    """Recursively yield every APIRoute reachable from ``router``.

    Bug-8448. Enumeration scope, audited per CLAUDE.md's coverage-tool
    blind-spot rule. The walker is keyed on STRUCTURE (the presence of a
    ``.routes`` collection), never on a path shape, so it is not blind to a
    ``{project_id}``-shaped bulk route the way a ``{model_id}``-keyed matcher
    would be. For each child route:

      * ``APIRoute``                     -> yielded (the property applies).
      * a container exposing ``.routes`` -> descended: ``APIRouter``, Starlette
        ``Router``/``Mount``/``Host``, and FastAPI 0.139+'s opaque
        ``_IncludedRouter`` wrapper — matched by having ``.routes``, not by
        class name, so a future wrapper class is handled the same way.
      * a recognised non-API leaf        -> ignored: ``Route`` (docs/openapi),
        ``WebSocketRoute``/``APIWebSocketRoute``.

    Anything else raises ``_UnknownRouteShape`` — FAIL CLOSED, never skip.
    """
    routes = getattr(router, "routes", None)
    if routes is None:
        raise _UnknownRouteShape(f"{router!r} exposes no .routes to walk")
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif getattr(route, "routes", None) is not None:
            yield from _walk(route)
        elif isinstance(route, _IGNORED_LEAF):
            continue
        else:
            raise _UnknownRouteShape(
                f"unrecognised route shape {type(route).__name__} "
                f"({route!r}); refusing to skip it silently"
            )


def _collect_routes():
    seen: set[str] = set()
    for route in _walk(app):
        for method in sorted(route.methods - _SKIP_METHODS):
            key = f"{method} {route.path}"
            if key not in seen:
                seen.add(key)
                marks = (
                    [pytest.mark.xfail(reason="requires real infrastructure")]
                    if key in _INFRA_ROUTES
                    else []
                )
                yield pytest.param(method, route.path, id=key, marks=marks)


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


# ── Bug-8448: coverage guard must fail closed on route drift ──────────────
def _dummy_api_route(path: str = "/x") -> APIRoute:
    async def _ep():  # pragma: no cover - never invoked
        return {}

    return APIRoute(path, endpoint=_ep, methods=["GET"])


def test_route_coverage_floor():
    """Fail closed if route collection collapses.

    Under a FastAPI that wraps ``include_router`` routes in an opaque
    container, a flat ``app.routes`` + ``isinstance(APIRoute)`` scan silently
    collected zero routes and the parametrised suite passed on nothing. The
    recursive walker plus this floor make that regression RED instead.

    Test escape: a parametrised suite with zero parameters is green.
    Guard: this floor. Tier: T1.
    """
    collected = list(_walk(app))
    assert len(collected) >= _MIN_ROUTES, (
        f"route coverage floor breached: {len(collected)} < {_MIN_ROUTES}; "
        "the accessibility guard would pass vacuously"
    )


def test_walk_descends_opaque_included_router():
    """A route reachable only through a container that just exposes ``.routes``
    (the shape FastAPI 0.139+ produces for ``include_router``) is still found —
    the walker descends by structure, not by class name."""
    inner = _dummy_api_route("/nested")

    class _FakeIncludedRouter:  # opaque wrapper: no APIRoute-ness, only .routes
        routes = [inner]

    class _FakeApp:
        routes = [_FakeIncludedRouter()]

    assert list(_walk(_FakeApp())) == [inner]


def test_walk_fails_closed_on_unknown_shape():
    """An unrecognised route object raises instead of being silently skipped —
    the walker's own enumeration blind-spot guard."""

    class _FakeApp:
        routes = [object()]

    with pytest.raises(_UnknownRouteShape):
        list(_walk(_FakeApp()))
