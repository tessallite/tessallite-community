"""Gateway-client route matrix — Bug-8120 (F-007-08).

A green security validation must prove the required *client and route* matrix,
not a subset. This guard makes the query-router's client-facing route surface
explicit and **additive-only**: every route a BI/headless/plugin/agent client
can reach must be classified in ``_GATEWAY_CLIENT_ROUTES`` with the client
family that reaches it and whether it sits on the row-security-enforced data
path. A NEW client route that is not classified FAILS this test, forcing a
human to decide its client family and — the point of the finding — whether it
needs a row-security outcome probe before it can ship green.

This is a producer-derived classification guard, not a live security test:
the deployed-session ``LIVE-SECURITY-*`` scenarios own proving enforcement
against real clients. This guard owns proving the matrix is COMPLETE, so a new
client entry point cannot silently escape that live coverage.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.routing import Route, WebSocketRoute

from src.main import app

_SKIP_METHODS = {"HEAD", "OPTIONS"}

# Infrastructure / framework routes no client's data path reaches. These are the
# NARROW, explicitly-named exclusions (health, metrics, and the docs/openapi
# leaves FastAPI auto-mounts as raw Starlette Routes). Everything else that is an
# HTTP leaf — APIRoute OR raw Starlette Route — must be classified below, so a
# newly exposed client endpoint of either shape fails CI.
_INFRA_ROUTES: set[str] = {
    "GET /health",
    # CP-11 F-030-01: process-liveness ping. No client's data path reaches it and
    # it returns no model data — a non-client, non-data-path infrastructure route,
    # classified here exactly like /health and /metrics.
    "GET /liveness",
    "GET /metrics",
    "GET /openapi.json",
    "GET /docs",
    "GET /docs/oauth2-redirect",
    "GET /redoc",
}


@dataclass(frozen=True)
class RouteClass:
    """How a client-facing route is classified.

    client: the client family that reaches it (JDBC and XMLA arrive through the
      gateway, which forwards to ``/api/v1/execute``; HEADLESS, PLUGIN, REST and
      AGENT reach the query-router directly).
    security_enforced: True when the route returns model DATA and must apply
      row/column security for the caller's persona — i.e. it needs a
      deployed-session security outcome probe. False for metadata/catalogue,
      plan-only, validation, and admin/diagnostic routes.
    """

    client: str
    security_enforced: bool


_DATA = True
_META = False

# The matrix. Every client-facing query-router route, classified. Keep this in
# sync with the route surface: the tests below fail if a route is missing here
# (unclassified) or if an entry here no longer maps to a real route (stale).
_GATEWAY_CLIENT_ROUTES: dict[str, RouteClass] = {
    # -- Data execution paths: row security MUST be enforced --
    "POST /api/v1/execute": RouteClass("JDBC/XMLA/REST", _DATA),
    "POST /api/v1/headless/query": RouteClass("HEADLESS", _DATA),
    "POST /api/v1/plugin/execute": RouteClass("EXCEL_PLUGIN", _DATA),
    "POST /api/v1/discover/members": RouteClass("JDBC/XMLA/REST", _DATA),
    "POST /api/v1/measures/{measure_id}/drill-through": RouteClass("REST/XMLA", _DATA),
    # -- Metadata / catalogue: persona-scoped, no row data --
    "GET /api/v1/headless/models": RouteClass("HEADLESS", _META),
    "GET /api/v1/headless/models/{model_id}/dimensions": RouteClass("HEADLESS", _META),
    "GET /api/v1/headless/models/{model_id}/measures": RouteClass("HEADLESS", _META),
    # Bug-9219/9224 SPA contract: the DEPLOYED @-object catalogue (parameters,
    # named sets, Named Queries) a SQL client needs to offer an @-token picker.
    # _META, and deliberately so: it publishes governed model DEFINITIONS —
    # names, types, defaults, allowed values, member COUNTS — and never a member
    # VALUE or a source row, so there is nothing here for a row-security
    # predicate to filter. Membership itself is reached only by running a query
    # through /execute, which is _DATA and already probed. Model access is gated
    # by ``enforce_model_scope`` + ``load_authorized_model(min_role="viewer")``,
    # the same gate the other metadata routes use.
    "GET /api/v1/models/{model_id}/named-objects": RouteClass("REST/SPA", _META),
    "POST /api/v1/measures/{measure_id}/drill-options": RouteClass("REST/XMLA", _META),
    # -- Plan / validation only: no rows returned --
    "POST /api/v1/explain": RouteClass("REST", _META),
    "POST /api/v1/validate": RouteClass("REST", _META),
    # -- Introspection: modeller/admin connection tooling, not client data --
    "POST /api/v1/introspect": RouteClass("REST_ADMIN", _META),
    "POST /api/v1/introspect/batch": RouteClass("REST_ADMIN", _META),
    "POST /api/v1/introspect/connection/discover-columns": RouteClass("REST_ADMIN", _META),
    "POST /api/v1/introspect/connection/discover-tables": RouteClass("REST_ADMIN", _META),
    "POST /api/v1/introspect/connection/profile": RouteClass("REST_ADMIN", _META),
    "POST /api/v1/introspect/connection/test": RouteClass("REST_ADMIN", _META),
    "POST /api/v1/introspect/connection/test-draft": RouteClass("REST_ADMIN", _META),
    # -- Diagnostic / admin: not a client data path --
    "GET /api/v1/diagnostics/query-rewrites": RouteClass("DIAGNOSTIC", _META),
    "DELETE /api/v1/cache/models/{model_id}": RouteClass("ADMIN", _META),
}


class _UnknownRouteShape(TypeError):
    """A route object the matrix walker does not recognise. Raised so the walker
    FAILS CLOSED on an un-enumerated shape instead of silently dropping it —
    the exact false-green the flat scan produced for a raw Starlette Route."""


# Non-HTTP leaves the client route matrix does not classify (websockets).
_NON_HTTP_LEAF = (WebSocketRoute, APIWebSocketRoute)


def _walk(router):
    """Recursively yield every HTTP route leaf reachable from ``router``.

    Bug-8120 / L25-R1-01. The first cut yielded only ``APIRoute`` and silently
    discarded every other leaf, so a raw ``starlette.routing.Route`` client
    endpoint escaped classification and ``test_every_client_route_is_classified``
    stayed green on it. This walker recognises every SUPPORTED HTTP leaf and
    FAILS CLOSED on anything else:

      * ``APIRoute``                     -> yielded (FastAPI HTTP leaf).
      * raw ``Route`` (non-APIRoute)     -> yielded (Starlette HTTP leaf); it is
        then required to be classified OR in the narrow named ``_INFRA_ROUTES``.
      * a container exposing ``.routes`` -> descended (APIRouter / Mount / Host /
        FastAPI 0.139+ wrapper), matched structurally, not by class name.
      * ``WebSocketRoute`` / ``APIWebSocketRoute`` -> ignored (non-HTTP).

    Anything else raises ``_UnknownRouteShape`` — never silently skipped.
    """
    routes = getattr(router, "routes", None)
    if routes is None:
        raise _UnknownRouteShape(f"{router!r} exposes no .routes to walk")
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif isinstance(route, Route):
            # raw Starlette HTTP leaf (docs/openapi, or a hand-registered route)
            yield route
        elif getattr(route, "routes", None) is not None:
            yield from _walk(route)
        elif isinstance(route, _NON_HTTP_LEAF):
            continue
        else:
            raise _UnknownRouteShape(
                f"unrecognised route shape {type(route).__name__} "
                f"({route!r}); refusing to skip it silently"
            )


def _http_leaf_keys(router) -> set[str]:
    """All ``METHOD path`` keys for HTTP leaves under ``router`` (incl. infra)."""
    keys: set[str] = set()
    for route in _walk(router):
        methods = route.methods or set()
        for method in sorted(methods - _SKIP_METHODS):
            keys.add(f"{method} {route.path}")
    return keys


def _client_facing_routes(router=app) -> set[str]:
    return {k for k in _http_leaf_keys(router) if k not in _INFRA_ROUTES}


def test_every_client_route_is_classified():
    """Additive-only: a new client-facing route not in the matrix fails CI.

    This is the forcing function F-007-08 asks for — a route cannot ship green
    until a human classifies its client family and security posture, which is
    the moment they must decide whether it needs a live security probe.

    Test escape: security validation proved a subset of clients/routes green.
    Guard: this matrix. Tier: T2.
    """
    routes = _client_facing_routes()
    unclassified = sorted(routes - set(_GATEWAY_CLIENT_ROUTES))
    assert not unclassified, (
        "New client-facing route(s) are not classified in "
        f"_GATEWAY_CLIENT_ROUTES: {unclassified}. Classify each with its client "
        "family and whether it is on the row-security-enforced data path, then "
        "ensure a deployed-session LIVE-SECURITY-* probe covers the data paths."
    )


def test_matrix_has_no_stale_entries():
    """Every classified route still exists — a removed route must drop its row,
    so the matrix cannot rot into asserting coverage of a dead surface."""
    routes = _client_facing_routes()
    stale = sorted(set(_GATEWAY_CLIENT_ROUTES) - routes)
    assert not stale, (
        f"_GATEWAY_CLIENT_ROUTES names routes that no longer exist: {stale}"
    )


def test_data_paths_are_declared_security_enforced():
    """The query-execution paths a client sends rows-returning requests to must
    be marked security_enforced, so the matrix names exactly the routes a
    LIVE-SECURITY probe must cover."""
    enforced = {
        key for key, rc in _GATEWAY_CLIENT_ROUTES.items() if rc.security_enforced
    }
    # The known data-returning execution routes. If a new one is added it must
    # be enforced; if this set drifts from the matrix the assertion fails.
    required = {
        "POST /api/v1/execute",
        "POST /api/v1/headless/query",
        "POST /api/v1/plugin/execute",
    }
    missing = sorted(required - enforced)
    assert not missing, (
        f"data-execution routes not marked security_enforced: {missing}"
    )


# ── L25-R1-01: the matrix must fail closed on a supported non-APIRoute leaf ──
def test_raw_starlette_route_is_not_silently_classified():
    """A raw ``starlette.routing.Route`` HTTP endpoint added to the app MUST be
    seen by the matrix and flagged unclassified.

    Regression for the round-1 false green: the walker yielded only APIRoute, so
    appending ``Route("/api/v1/new-client-route", ...)`` left
    ``test_every_client_route_is_classified`` green. Mutating ``app.routes`` here
    reproduces exactly that probe. Fail-before: the raw route was absent from
    ``_client_facing_routes()``; pass-after: it appears and would fail CI.
    """
    async def _ep(request):  # pragma: no cover - never invoked
        return None

    probe = Route(
        "/api/v1/mutation-probe-unclassified", endpoint=_ep, methods=["POST"]
    )
    app.routes.append(probe)
    try:
        unclassified = _client_facing_routes() - set(_GATEWAY_CLIENT_ROUTES)
        assert "POST /api/v1/mutation-probe-unclassified" in unclassified, (
            "a raw Starlette HTTP route escaped the matrix — the walker is "
            "fail-open again"
        )
    finally:
        app.routes.remove(probe)


def test_walk_fails_closed_on_unknown_shape():
    """An unrecognised route object raises instead of being silently skipped."""

    class _FakeApp:
        routes = [object()]

    import pytest

    with pytest.raises(_UnknownRouteShape):
        list(_walk(_FakeApp()))


def test_docs_leaves_are_the_only_raw_routes_excluded():
    """The narrow infra exclusion covers exactly the framework raw-Route leaves
    present, so a NEW raw Route cannot hide behind a blanket 'ignore all Route'
    rule — every excluded key must actually exist as an HTTP leaf."""
    all_leaves = _http_leaf_keys(app)
    dangling = sorted(_INFRA_ROUTES - all_leaves)
    assert not dangling, (
        f"_INFRA_ROUTES names leaves that do not exist: {dangling}"
    )
