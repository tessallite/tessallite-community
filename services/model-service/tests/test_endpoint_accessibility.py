"""Endpoint accessibility — verify every registered route is reachable and does not crash.

For each route registered on the FastAPI app, fires a minimal request and
asserts the response status is not a 5xx server error.  Expected non-5xx
codes include 200 (success), 400/404 (missing entity with mock DB),
422 (empty body on POST/PUT/PATCH), and 403 (role checks beyond what the
mock covers).  The test does NOT validate business logic — only wiring.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import re
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.routing import Route, WebSocketRoute

from src.main import app
from src.auth.middleware import (
    CurrentUser,
    get_current_user,
    require_system_admin,
    require_tenant_admin,
)

# ── Constants ────────────────────────────────────────────────────────────
_DUMMY_UUID = "00000000-0000-4000-8000-000000000001"
_TEST_TENANT = "test-tenant"

_SKIP_METHODS = {"HEAD", "OPTIONS"}

# Floor for Bug-8448: the recursive walker below must find at least this many
# real APIRoutes. Well under the ~415 the service actually registers, but far
# above zero, so a FastAPI upgrade that hides included routers (0.139+ wraps
# them in an opaque container the old flat scan could not see) makes this suite
# FAIL CLOSED instead of passing on an empty parameter set.
_MIN_ROUTES = 300

_PATH_SUBS: dict[str, str] = {
    "project_id": _DUMMY_UUID,
    "model_id": _DUMMY_UUID,
    "tenant_id": _TEST_TENANT,
    "tenant_slug": _TEST_TENANT,
    "source_id": _DUMMY_UUID,
    "table_id": _DUMMY_UUID,
    "agg_id": _DUMMY_UUID,
    "hierarchy_id": _DUMMY_UUID,
    "health_id": _DUMMY_UUID,
    "dimension_id": _DUMMY_UUID,
    "measure_id": _DUMMY_UUID,
    "join_id": _DUMMY_UUID,
    "persona_id": _DUMMY_UUID,
    "rule_id": _DUMMY_UUID,
    "pocket_id": _DUMMY_UUID,
    "target_id": _DUMMY_UUID,
    "connection_id": _DUMMY_UUID,
    "attr_id": _DUMMY_UUID,
    "version_id": _DUMMY_UUID,
    "version_a_id": _DUMMY_UUID,
    "version_b_id": _DUMMY_UUID,
    "config_id": _DUMMY_UUID,
    "event_id": _DUMMY_UUID,
    "user_id": _DUMMY_UUID,
    "mapping_id": _DUMMY_UUID,
    "webhook_id": _DUMMY_UUID,
    "delivery_id": _DUMMY_UUID,
    "tag_id": _DUMMY_UUID,
    "restriction_id": _DUMMY_UUID,
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


# Routes that legitimately require real infrastructure (an Alembic subprocess)
# and cannot run with mocks.
#
# Bug-8922: two more routes used to sit here. ``POST .../refresh/policy`` had
# been reachable under mocks for some time (a stale entry, XPASSing), and
# ``POST /projects/{project_id}/export`` became reachable once the DB
# isolation below stopped missing the snapshot-session entry point. A route
# parked here is a route this suite does not actually assert, so an entry that
# starts passing is removed rather than left XPASSing.
_INFRA_ROUTES: set[str] = {
    "POST /api/v1/admin/migrate/system",
}

# SSE / streaming endpoints that never return a complete response under mocks.
# A 3-second window is enough to confirm wiring (no 5xx on startup).
_STREAMING_ROUTES: set[str] = {
    "GET /api/v1/projects/{project_id}/models/{model_id}/refresh/stream",
}


# ── Route discovery ──────────────────────────────────────────────────────
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


# ── Mock DB session ──────────────────────────────────────────────────────
#
# Every read API of ``AsyncSession`` has to be stubbed with the RESULT SHAPE it
# really returns, because the default is worse than useless: an un-stubbed
# ``AsyncMock`` attribute is a coroutine function whose awaited value is ANOTHER
# ``AsyncMock``, so ``(await db.scalars(q)).all()`` hands back a coroutine and
# ``list(...)`` raises ``TypeError``. The route is correct against PostgreSQL;
# only the double is wrong — and this suite then reports it as a 5xx.
#
# That is not hypothetical. ``execute`` was the only read API stubbed, which
# covered every route until the admin system-log routes (``GET
# /admin/system-logs``, ``POST /admin/system-logs/purge``) used ``scalars``.
# The two shapes are genuinely different — ``execute`` returns a ``Result``
# (rows via ``.scalars().all()``), ``scalars`` a ``ScalarResult`` (rows via
# ``.all()``) — so one stub could never have covered both.
#
# The shapes below are therefore DISCOVERED from ``AsyncSession``'s own return
# annotations rather than remembered, and the guard at the bottom of this file
# fails closed if the class grows a read API this double does not model.
# Listing them by hand is what let the last one through.


def _sync_result_double() -> MagicMock:
    """``Result``: rows through ``.scalars()``, or directly off the result."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.scalar.return_value = None
    result.scalars.return_value = _sync_scalar_result_double()
    result.all.return_value = []
    result.first.return_value = None
    result.one_or_none.return_value = None
    return result


def _sync_scalar_result_double() -> MagicMock:
    """``ScalarResult``: already projected, so rows come straight off it."""
    scalars = MagicMock()
    scalars.all.return_value = []
    scalars.first.return_value = None
    scalars.one_or_none.return_value = None
    scalars.unique.return_value = scalars
    return scalars


def _async_result_double() -> MagicMock:
    """``AsyncResult``: same shape, but the row accessors are awaitable."""
    result = MagicMock()
    result.scalars.return_value = _async_scalar_result_double()
    result.all = AsyncMock(return_value=[])
    result.first = AsyncMock(return_value=None)
    result.one_or_none = AsyncMock(return_value=None)
    return result


def _async_scalar_result_double() -> MagicMock:
    scalars = MagicMock()
    scalars.all = AsyncMock(return_value=[])
    scalars.first = AsyncMock(return_value=None)
    scalars.one_or_none = AsyncMock(return_value=None)
    scalars.unique.return_value = scalars
    return scalars


# Keyed on the result class ``AsyncSession`` declares it returns, so a new or
# renamed read API is matched by SHAPE rather than by a remembered method name.
_RESULT_DOUBLES = {
    "Result": _sync_result_double,
    "ScalarResult": _sync_scalar_result_double,
    "AsyncResult": _async_result_double,
    "AsyncScalarResult": _async_scalar_result_double,
}


def _result_returning_session_apis() -> dict[str, str]:
    """``AsyncSession``'s coroutine read APIs, mapped to their result class.

    SQLAlchemy uses ``from __future__ import annotations``, so a return
    annotation arrives as the SOURCE TEXT (``'ScalarResult[Any]'``), the same
    way ``_discover_session_entry_points`` above sees ``'async_sessionmaker'``.
    Take the leading identifier and drop any subscript.
    """
    found: dict[str, str] = {}
    for name, function in vars(AsyncSession).items():
        if name.startswith("_") or not inspect.iscoroutinefunction(function):
            continue
        annotation = getattr(function, "__annotations__", {}).get("return")
        if not isinstance(annotation, str):
            annotation = getattr(annotation, "__name__", "")
        result_cls = annotation.split("[")[0].strip()
        if result_cls in _RESULT_DOUBLES:
            found[name] = result_cls
    return found


def _make_mock_session() -> AsyncMock:
    s = AsyncMock()
    s.add = MagicMock()
    s.flush = AsyncMock()
    s.commit = AsyncMock()
    s.delete = AsyncMock()
    s.rollback = AsyncMock()

    for name, result_cls in _result_returning_session_apis().items():
        setattr(s, name, AsyncMock(return_value=_RESULT_DOUBLES[result_cls]()))

    # ``scalar`` and ``get`` return a VALUE, not a result object, so they are
    # annotated ``Any`` / ``Optional[_O]`` and the discovery above cannot type
    # them. An empty database answers both with None.
    s.scalar = AsyncMock(return_value=None)
    s.get = AsyncMock(return_value=None)
    return s


# ── Fixtures ─────────────────────────────────────────────────────────────
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


# ── DB isolation ─────────────────────────────────────────────────────────
#
# Bug-8922: this isolation used to name its stubs one at a time
# (``get_tenant_session_factory`` + ``SystemSessionLocal``). A route reaching
# the database through any OTHER entry point of ``shared.db.session`` opened a
# REAL connection and 500'd — which is exactly what the two export routes did
# via ``consistent_read_session`` → ``get_tenant_snapshot_session_factory``.
# Patching those two names would have re-opened the same hole the next time a
# session entry point was added, so the entry points are DISCOVERED
# structurally instead of listed.


def _discover_session_entry_points() -> tuple[list[str], list[str]]:
    """Return ``shared.db.session``'s session handles as (factories, getters).

    Structural, not name-based: an ``async_sessionmaker`` bound at module
    level, and a coroutine annotated ``-> async_sessionmaker``, are the only
    two shapes that hand a caller something it can open a real connection
    from. Anything else in the module goes through one of these two.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from shared.db import session as session_mod

    factories: list[str] = []
    factory_getters: list[str] = []
    for name, obj in vars(session_mod).items():
        if isinstance(obj, async_sessionmaker):
            factories.append(name)
        elif inspect.iscoroutinefunction(obj):
            annotation = getattr(obj, "__annotations__", {}).get("return")
            # ``session.py`` uses ``from __future__ import annotations``, so
            # the annotation arrives as a string; accept the object too in
            # case that import is ever dropped.
            if annotation is async_sessionmaker or (
                isinstance(annotation, str)
                and annotation.strip() == "async_sessionmaker"
            ):
                factory_getters.append(name)
    return factories, factory_getters


# Taken ONCE at import, before any fixture has replaced these attributes — a
# scan run from inside the fixture would only rediscover its own stubs.
_SESSION_FACTORIES, _SESSION_FACTORY_GETTERS = _discover_session_entry_points()


def _patch_session_name(stack: contextlib.ExitStack, name, replacement) -> None:
    """Replace ``shared.db.session.<name>`` AND every from-import of it.

    ``from shared.db.session import X`` binds X into the importing module's
    namespace, so patching only the definition module leaves that copy live.
    Rebinding by identity covers every current and future importer without
    this file having to know their names.
    """
    from shared.db import session as session_mod

    original = getattr(session_mod, name)
    stack.enter_context(patch.object(session_mod, name, replacement))
    for module in list(sys.modules.values()):
        if module is None or module is session_mod:
            continue
        try:
            bound = getattr(module, name, None)
        except Exception:  # pragma: no cover - lazy / partially-initialised
            continue
        # Only the lookup is tolerant. A failure to REPLACE a copy we found
        # must raise: swallowing it would leave a live entry point behind and
        # fail open, which is the shape of the hole this closes.
        if bound is original:
            stack.enter_context(patch.object(module, name, replacement))


@pytest.fixture(autouse=True)
def _mock_handler_db():
    mock_session = _make_mock_session()

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_cm)

    async def _fake_get_factory(tenant_id: str):
        return mock_factory

    # Fail closed. A rename or refactor that empties the discovery would
    # silently restore the exact hole Bug-8922 closed, and every route in this
    # suite would start talking to a real database.
    assert _SESSION_FACTORIES, "no async_sessionmaker found in shared.db.session"
    assert _SESSION_FACTORY_GETTERS, (
        "no '-> async_sessionmaker' coroutine found in shared.db.session"
    )

    with contextlib.ExitStack() as stack:
        for name in _SESSION_FACTORIES:
            _patch_session_name(stack, name, mock_factory)
        for name in _SESSION_FACTORY_GETTERS:
            _patch_session_name(stack, name, _fake_get_factory)
        yield mock_session


@pytest.fixture
async def client():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as ac:
        yield ac


# ── Tests ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("method,path", list(_collect_routes()))
@pytest.mark.asyncio
async def test_endpoint_accessible(client, method, path):
    url = _resolve_path(path)
    kwargs: dict = {}
    if method in ("POST", "PUT", "PATCH"):
        kwargs["json"] = {}

    key = f"{method} {path}"
    if key in _STREAMING_ROUTES:
        try:
            resp = await asyncio.wait_for(
                client.request(method, url, **kwargs), timeout=3.0,
            )
        except asyncio.TimeoutError:
            return
        assert resp.status_code < 500, (
            f"{method} {url} -> {resp.status_code}: {resp.text[:300]}"
        )
        return

    resp = await client.request(method, url, **kwargs)
    assert resp.status_code < 500, (
        f"{method} {url} -> {resp.status_code}: {resp.text[:300]}"
    )


@pytest.mark.asyncio
async def test_bug_8922_every_session_entry_point_is_isolated(_mock_handler_db):
    """Bug-8922: the DB isolation must cover EVERY way to open a real session.

    Two export routes reached the tenant database through
    ``consistent_read_session`` → ``get_tenant_snapshot_session_factory``,
    which the isolation did not stub, so they opened a real connection and
    returned 500 (``ValueError: Tenant test-tenant not found in system DB``).

    Without this guard the hole is only visible while some route in this suite
    happens to use the missed entry point — the routes above would go green
    again the moment they changed, and the next entry point would land
    uncovered. Assert the property directly instead: every session handle
    ``shared.db.session`` exposes, and every from-import copy of one, resolves
    to the fixture's mock while the fixture is active.

    Test escape: the isolation named its stubs one at a time, so a new entry
    point was silently uncovered. Guard: this test. Tier: T1.
    """
    from shared.db import session as session_mod
    from shared.model_snapshot import consistent_read

    assert "SystemSessionLocal" in _SESSION_FACTORIES
    assert {
        "get_tenant_session_factory",
        "get_tenant_snapshot_session_factory",
    } <= set(_SESSION_FACTORY_GETTERS)

    for name in _SESSION_FACTORY_GETTERS:
        factory = await getattr(session_mod, name)(_TEST_TENANT)
        async with factory() as session:
            assert session is _mock_handler_db, f"{name} escaped the isolation"
    for name in _SESSION_FACTORIES:
        async with getattr(session_mod, name)() as session:
            assert session is _mock_handler_db, f"{name} escaped the isolation"

    # The from-import copy — the exact miss in Bug-8922. ``consistent_read``
    # bound ``get_tenant_snapshot_session_factory`` into its own namespace, so
    # patching the definition module alone left the real one live.
    async with consistent_read.consistent_read_session(_TEST_TENANT) as session:
        assert session is _mock_handler_db


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


@pytest.mark.asyncio
async def test_every_result_returning_session_api_is_modelled(_mock_handler_db):
    """The mock session must answer every read API ``AsyncSession`` offers.

    ``execute`` was the only one stubbed. A route that read through ``scalars``
    instead got the ``AsyncMock`` default — a coroutine where a ``ScalarResult``
    belongs — so ``list(...)`` raised ``TypeError`` and this suite reported a
    5xx against a route that is correct against PostgreSQL. Two admin
    system-log routes did exactly that.

    Assert the property rather than the two routes: every coroutine on
    ``AsyncSession`` that returns a ``Result``/``ScalarResult`` is stubbed, and
    what it returns hands back real rows instead of a coroutine. Discovery is
    off the class itself, so a read API added by a SQLAlchemy upgrade fails here
    instead of surfacing later as an unexplained 500 on one route.

    Test escape: the double modelled one read API and the rest defaulted to a
    coroutine. Guard: this test. Tier: T1.
    """
    apis = _result_returning_session_apis()
    assert {"execute", "scalars"} <= set(apis), (
        f"discovery stopped seeing AsyncSession's read APIs: {sorted(apis)}"
    )

    for name, result_cls in apis.items():
        result = await getattr(_mock_handler_db, name)("statement")
        assert not inspect.iscoroutine(result), f"{name} was not stubbed"
        rows = result.all()
        if result_cls.startswith("Async"):
            rows = await rows
        assert rows == [], f"{name} -> {result_cls}.all() did not return rows"

    # ``Result`` projects through ``.scalars()``; ``ScalarResult`` is already
    # projected. Conflating the two is what a single shared stub would do.
    assert (await _mock_handler_db.execute("statement")).scalars().all() == []
    assert (await _mock_handler_db.scalars("statement")).all() == []
