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
