"""Shared Prometheus metrics and ASGI middleware for all Tessallite services.

Each service mounts ``PrometheusMiddleware`` and adds a ``/metrics`` route.
Metric names are prefixed with ``tessallite_``.
"""
from __future__ import annotations

import hmac
import os
import time
from typing import Callable

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# ---------------------------------------------------------------------------
# Metric definitions (module-level singletons — imported by each service)
# ---------------------------------------------------------------------------

REQUEST_COUNT = Counter(
    "tessallite_http_requests_total",
    "Total HTTP requests",
    ["service", "method", "path", "status"],
)

REQUEST_LATENCY = Histogram(
    "tessallite_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["service", "method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

QUERY_ROUTED_COUNT = Counter(
    "tessallite_query_router_queries_total",
    "Queries handled by the query router",
    ["routed_to"],  # aggregate | pocket | source | error
)

# ---------------------------------------------------------------------------
# Model health & usage analytics
# ---------------------------------------------------------------------------

MODEL_QUERY_COUNT = Counter(
    "tessallite_model_queries_total",
    "Queries executed per model",
    ["tenant", "project", "model_name", "protocol", "route_type"],
)

MODEL_QUERY_ERRORS = Counter(
    "tessallite_model_query_errors_total",
    "Failed queries per model",
    ["tenant", "project", "model_name", "error_type"],
)

MODEL_QUERY_DURATION = Histogram(
    "tessallite_model_query_duration_seconds",
    "Query execution duration per model",
    ["tenant", "project", "model_name"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)

MODEL_BYTES_PROCESSED = Counter(
    "tessallite_model_bytes_processed_total",
    "Total bytes scanned per model",
    ["tenant", "project", "model_name"],
)

MODEL_ROWS_RETURNED = Counter(
    "tessallite_model_rows_returned_total",
    "Total rows returned per model",
    ["tenant", "project", "model_name"],
)

REFRESH_RUN_DURATION = Histogram(
    "tessallite_refresh_run_duration_seconds",
    "Refresh run duration in seconds",
    ["mode"],  # full | incremental
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600),
)

REFRESH_RUN_COUNT = Counter(
    "tessallite_refresh_runs_total",
    "Total refresh runs",
    ["status"],  # completed | failed (AggregateRefreshRun.TERMINAL_STATUSES)
)

SLA_CHECK_ERRORS = Counter(
    "tessallite_sla_check_errors_total",
    "SLA checks that raised unexpectedly (that model was skipped this sweep; "
    "remaining models were still checked)",
    ["tenant"],
)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

# Paths that should not be tracked (e.g. health checks, metrics scrape)
_SKIP_PATHS = frozenset({"/metrics", "/health", "/readiness", "/liveness"})


def _route_template(request: Request) -> str:
    """Return the matched route template path, or the concrete path if no route
    matched (404s). Collapses entity UUIDs into ``{param}`` placeholders so the
    Prometheus path label has bounded cardinality (F-030-15)."""
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if template:
        return template
    return request.url.path


class PrometheusMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that increments request count and latency histograms."""

    def __init__(self, app, service_name: str) -> None:
        super().__init__(app)
        self._service = service_name

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.url.path in _SKIP_PATHS:
            return await call_next(request)

        method = request.method
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start

        # F-030-15: label with the matched route *template*
        # (e.g. "/api/v1/projects/{project_id}/models/{model_id}/metrics") rather
        # than the concrete request path. The concrete path embeds project/model/
        # alert UUIDs, so every distinct entity created a new permanent label set
        # — unbounded cardinality, a slow per-replica memory leak, and a ballooning
        # scrape. The template collapses all entities of a route to one series.
        path = _route_template(request)

        REQUEST_COUNT.labels(
            service=self._service,
            method=method,
            path=path,
            status=str(response.status_code),
        ).inc()
        REQUEST_LATENCY.labels(
            service=self._service,
            method=method,
            path=path,
        ).observe(duration)

        return response


# F-030-15: optional static-token guard for the /metrics scrape endpoint. The
# MODEL_QUERY_COUNT series carries tenant + model names, so an exposed service
# port would otherwise leak every tenant's model names and query volumes to any
# party that can reach it. When TESSALLITE_METRICS_TOKEN is set, the scraper must
# present it as a Bearer token (or X-Metrics-Token header). When unset, the
# endpoint behaves as before (network-policy-only protection) so existing
# in-cluster scrapers and local dev are not broken by a config-less upgrade —
# the deployment guides document setting the token in any internet-reachable
# (Cloud Run) deployment.
def _metrics_token() -> str:
    return os.environ.get("TESSALLITE_METRICS_TOKEN", "")


def _metrics_token_ok(request: Request) -> bool:
    expected = _metrics_token()
    if not expected:
        return True  # no token configured -> open (network-policy protected)
    presented = request.headers.get("x-metrics-token", "")
    if not presented:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            presented = auth[7:].strip()
    return bool(presented) and hmac.compare_digest(presented, expected)


def metrics_response(request: Request | None = None) -> Response:
    """Return a Prometheus exposition-format response for ``GET /metrics``.

    If ``TESSALLITE_METRICS_TOKEN`` is configured, the request must present a
    matching token (Bearer or ``X-Metrics-Token``) or a 401 is returned (F-030-15).
    """
    if request is not None and not _metrics_token_ok(request):
        return Response(
            content="metrics endpoint requires a valid token",
            status_code=401,
            media_type="text/plain",
        )
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)
