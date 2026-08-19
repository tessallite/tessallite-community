"""
Tests for the Prometheus /metrics endpoint on the query-router.

Run from tessallite/services/query-router/:
    pytest tests/test_metrics.py
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shared.metrics import PrometheusMiddleware, metrics_response, QUERY_ROUTED_COUNT


@pytest.fixture()
def metrics_app():
    """Minimal FastAPI app with Prometheus middleware and /metrics endpoint."""
    app = FastAPI()
    app.add_middleware(PrometheusMiddleware, service_name="test-service")

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics():
        return metrics_response()

    return app


def test_metrics_endpoint_returns_200(metrics_app):
    with TestClient(metrics_app) as client:
        resp = client.get("/metrics")
    assert resp.status_code == 200


def test_metrics_content_type(metrics_app):
    with TestClient(metrics_app) as client:
        resp = client.get("/metrics")
    assert "text/plain" in resp.headers["content-type"]


def test_query_routed_counter_present(metrics_app):
    QUERY_ROUTED_COUNT.labels(routed_to="aggregate").inc()
    with TestClient(metrics_app) as client:
        resp = client.get("/metrics")
    assert "tessallite_query_router_queries_total" in resp.text


# ---------------------------------------------------------------------------
# Bug-8163: REQUEST_COUNT must be incremented (status="500") even when the
# route handler raises an uncaught exception. Previously the counter/latency
# recording sat entirely after ``response = await call_next(request)``, so an
# exception propagating out of ``call_next`` skipped both metrics — the exact
# condition (a live outage) where operators most need the counter to move.
# ---------------------------------------------------------------------------


def _read_counter_value(metrics_text: str, metric_name: str, **labels: str) -> float | None:
    """Find the exposition line for ``metric_name{labels...}`` (in any label
    order) and return its trailing value, or None if no line matches."""
    for line in metrics_text.splitlines():
        if not line.startswith(metric_name + "{"):
            continue
        if all(f'{key}="{value}"' in line for key, value in labels.items()):
            return float(line.rsplit(" ", 1)[-1])
    return None


def test_request_count_includes_uncaught_exception_bug8163():
    """A route that raises an uncaught exception must still increment
    REQUEST_COUNT with status="500" — proving the middleware records the
    metric on the exception path, not only the normal return path."""
    app = FastAPI()
    app.add_middleware(PrometheusMiddleware, service_name="test-service-bug8163")

    @app.get("/boom")
    async def boom():
        raise RuntimeError("uncaught failure — must still be counted")

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics():
        return metrics_response()

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/boom")
        assert resp.status_code == 500
        metrics_resp = client.get("/metrics")

    value = _read_counter_value(
        metrics_resp.text,
        "tessallite_http_requests_total",
        service="test-service-bug8163",
        method="GET",
        path="/boom",
        status="500",
    )
    assert value == 1.0, (
        "REQUEST_COUNT{status=500} must be incremented for an uncaught "
        f"exception; got {value!r} from:\n{metrics_resp.text}"
    )


def test_exception_is_reraised_unchanged_bug8163():
    """The middleware must not swallow or transform the exception — it only
    records the metric and re-raises. With raise_server_exceptions=True (the
    TestClient default) the original exception type must surface."""
    app = FastAPI()
    app.add_middleware(PrometheusMiddleware, service_name="test-service-bug8163-reraise")

    @app.get("/boom")
    async def boom():
        raise RuntimeError("uncaught failure — must propagate unchanged")

    with TestClient(app) as client:
        with pytest.raises(RuntimeError, match="must propagate unchanged"):
            client.get("/boom")


# ---------------------------------------------------------------------------
# Bug-7675: _route_template must NOT return the concrete path for 404s.
# The previous fallback returned request.url.path verbatim, so an
# unauthenticated scanner issuing GET /<random> in a loop created one
# permanent Prometheus label set per distinct path per replica — unbounded
# cardinality, slow memory growth, and ballooning /metrics payloads.
# ---------------------------------------------------------------------------

from shared.metrics import _route_template
from unittest.mock import MagicMock


def _make_request(scope_route=None, url_path="/some/path"):
    """Build a minimal mock Request for _route_template testing."""
    req = MagicMock(spec=["scope", "url"])
    req.scope = {}
    if scope_route is not None:
        req.scope["route"] = scope_route
    req.url = MagicMock()
    req.url.path = url_path
    return req


def test_route_template_uses_template_when_route_matches():
    """When a route matches, return its template (e.g. /api/v1/models/{id})."""
    route = MagicMock()
    route.path = "/api/v1/models/{model_id}"
    req = _make_request(scope_route=route, url_path="/api/v1/models/abc-123")
    assert _route_template(req) == "/api/v1/models/{model_id}"


def test_route_template_returns_constant_for_unmatched_404():
    """Bug-7675: unmatched routes must collapse to __unmatched__, NOT the
    concrete path. A random scanner must not inflate label cardinality."""
    req = _make_request(scope_route=None, url_path="/totally-random-scanner-path")
    result = _route_template(req)
    assert result == "__unmatched__"
    assert "/totally-random-scanner-path" not in result


def test_route_template_returns_constant_for_various_404_paths():
    """Verify multiple distinct 404 paths all map to the same constant."""
    paths = [
        "/aaa-bbb-ccc-ddd",
        "/wp-admin/login.php",
        "/.env",
        "/api/v999/nonexistent",
    ]
    results = set()
    for path in paths:
        req = _make_request(scope_route=None, url_path=path)
        results.add(_route_template(req))
    # All paths must resolve to the same single label value
    assert results == {"__unmatched__"}
