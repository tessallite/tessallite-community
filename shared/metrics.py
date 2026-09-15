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
    Gauge,
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

# Bug-8121: durable evidence gap monitor. Incremented every time the RLS-bypass
# audit write fails. The bypass itself proceeds (non-blocking for the query path)
# but the counter lets operators detect when the durable audit trail is
# incomplete and alert on it.
RLS_BYPASS_AUDIT_FAILURES = Counter(
    "tessallite_rls_bypass_audit_failures_total",
    "RLS bypass audit writes that failed (bypass proceeded, but durable evidence is missing)",
)


# Bug-9834: JDBC accept-loop liveness. The watchdog restarts a wedged gateway
# (Bug-8533), which is the right recovery but makes the underlying defect
# INVISIBLE — a wedge recurring hourly self-heals and looks exactly like one
# that never came back.
#
# These counters are supplementary, NOT the durable signal. An in-process
# counter cannot survive the very exit it is meant to record, so the durable
# carrier is the structured ``jdbc_watchdog_exit`` log event the watchdog emits
# and flushes before terminating. The failure counter below is still useful on
# its own: it rises during the strike window, before any exit, and a sustained
# non-zero rate that never reaches the strike limit is a degrading listener
# nobody would otherwise see.
JDBC_PROBE_FAILURES = Counter(
    "tessallite_gateway_jdbc_probe_failures_total",
    "JDBC accept-loop liveness probes that did not answer",
)
JDBC_WATCHDOG_EXITS = Counter(
    "tessallite_gateway_jdbc_watchdog_exits_total",
    "Times the watchdog proved the JDBC accept loop dead and exited the process",
)

# Bug-9834 (review finding 5): monitor JDBC DIRECTLY.
#
# Every alert above is derived from the watchdog, and the watchdog only runs
# when the listener started. So the one failure it cannot describe is the
# listener never binding at all: the HTTP surface stays healthy, ``up`` stays
# 1, no probe ever fails, no exit is ever recorded, and JDBC is dead in total
# silence. Alerting on ``up{job="gateway"}`` cannot see it either — that is the
# scrape endpoint, not the accept loop, and the two are independent.
#
# These two gauges are the direct statement. They are set once at start-up and
# on shutdown rather than sampled, because both describe a decision the process
# made, not a quantity that drifts.
JDBC_LISTENER_UP = Gauge(
    "tessallite_gateway_jdbc_listener_up",
    "1 when the gateway's JDBC accept loop is bound and serving, 0 when it is not",
)
# A watchdog believed to be running while it is not is worse than none, because
# the auto-recovery Bug-8533 depends on is assumed to be there. Reported
# separately from the listener so "serving with no auto-recovery" is expressible.
JDBC_WATCHDOG_UP = Gauge(
    "tessallite_gateway_jdbc_watchdog_up",
    "1 when the JDBC liveness watchdog is running, 0 when it is not",
)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

# Paths that should not be tracked (e.g. health checks, metrics scrape)
_SKIP_PATHS = frozenset({"/metrics", "/health", "/readiness", "/liveness"})


def _route_template(request: Request) -> str:
    """Return the matched route template path, or a fixed constant if no route
    matched (404s).  Collapses entity UUIDs into ``{param}`` placeholders so the
    Prometheus path label has bounded cardinality (F-030-15).

    Bug-7675: the previous fallback returned ``request.url.path`` verbatim for
    unmatched routes, re-opening the unbounded-cardinality vector the F-030-15
    fix was meant to close.  An unauthenticated scanner issuing
    ``GET /<random-uuid>`` in a loop creates one permanent label set per path
    per replica -- slow memory growth and ballooning ``/metrics`` payloads.
    Now unmatched paths are collapsed to a single ``__unmatched__`` constant.
    """
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if template:
        return template
    return "__unmatched__"


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
        # Bug-8163: REQUEST_COUNT / REQUEST_LATENCY were only recorded on the
        # normal return path (after ``call_next`` returned). An uncaught
        # exception propagating out of ``call_next`` — the exact condition
        # operators most need telemetry for — skipped both metrics entirely,
        # making error rate and latency look healthier than reality during a
        # real outage. ``status`` defaults to "500" (an escaped exception
        # always surfaces to the client as a server error via Starlette's
        # ServerErrorMiddleware) and the ``finally`` block records both
        # metrics on every exit path — success or exception — before the
        # exception is re-raised unchanged.
        status_code = "500"
        try:
            response = await call_next(request)
            status_code = str(response.status_code)
            return response
        finally:
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
                status=status_code,
            ).inc()
            REQUEST_LATENCY.labels(
                service=self._service,
                method=method,
                path=path,
            ).observe(duration)


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
