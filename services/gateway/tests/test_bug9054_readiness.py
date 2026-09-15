"""Bug-9054: gateway readiness checks direct peers without changing liveness."""
from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Response


class _Client:
    def __init__(self, responses):
        self.responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, url):
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


def _response(url: str, status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("GET", url))


@pytest.mark.asyncio
async def test_gateway_readiness_checks_only_direct_dependencies(monkeypatch):
    import src.main as gateway

    monkeypatch.setattr(gateway.settings, "MODEL_SERVICE_URL", "http://model")
    monkeypatch.setattr(gateway.settings, "QUERY_ROUTER_URL", "http://router")
    monkeypatch.setattr(gateway.settings, "READINESS_PROBE_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(gateway.settings, "GATEWAY_HEALTH_JDBC_PROBE_ENABLED", True)
    monkeypatch.setattr(gateway.settings, "JDBC_PORT", 5433)
    monkeypatch.setattr(
        gateway,
        "probe_jdbc_accept_loop_async",
        AsyncMock(return_value=(True, "SSLRequest answered")),
    )
    responses = {
        "http://model/readiness": _response("http://model/readiness", 200),
        "http://router/readiness": _response("http://router/readiness", 200),
    }
    monkeypatch.setattr(
        gateway.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(responses),
    )

    response = Response()
    body = await gateway.readiness(response)

    assert response.status_code in (None, 200)
    assert body["status"] == "ok"
    assert body["jdbc_listening"] is True
    assert body["dependencies"] == {
        "jdbc": "SSLRequest answered",
        "model-service": "ok",
        "query-router": "ok",
    }


@pytest.mark.asyncio
async def test_gateway_readiness_returns_503_when_a_direct_dependency_is_down(monkeypatch):
    import src.main as gateway

    monkeypatch.setattr(gateway.settings, "MODEL_SERVICE_URL", "http://model")
    monkeypatch.setattr(gateway.settings, "QUERY_ROUTER_URL", "http://router")
    monkeypatch.setattr(gateway.settings, "READINESS_PROBE_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(gateway.settings, "GATEWAY_HEALTH_JDBC_PROBE_ENABLED", False)
    responses = {
        "http://model/readiness": _response("http://model/readiness", 503),
        "http://router/readiness": _response("http://router/readiness", 200),
    }
    monkeypatch.setattr(
        gateway.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(responses),
    )

    response = Response()
    body = await gateway.readiness(response)

    assert response.status_code == 503
    assert body["status"] == "degraded"
    assert body["jdbc_listening"] is None
    assert body["dependencies"] == {
        "jdbc": "unproven",
        "model-service": "unavailable",
        "query-router": "ok",
    }


@pytest.mark.asyncio
async def test_gateway_readiness_fails_closed_when_jdbc_probe_is_disabled(monkeypatch):
    """Bug-9054: unproven JDBC cannot satisfy gateway serving readiness.

    Test escape: a disabled JDBC probe previously left readiness at HTTP 200.
    Guard: the readiness contract now returns 503 and identifies JDBC as
    unproven while /health keeps its established null field.
    Tier: T2 (service readiness contract).
    """
    import src.main as gateway

    monkeypatch.setattr(gateway.settings, "MODEL_SERVICE_URL", "http://model")
    monkeypatch.setattr(gateway.settings, "QUERY_ROUTER_URL", "http://router")
    monkeypatch.setattr(gateway.settings, "READINESS_PROBE_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(gateway.settings, "GATEWAY_HEALTH_JDBC_PROBE_ENABLED", False)
    responses = {
        "http://model/readiness": _response("http://model/readiness", 200),
        "http://router/readiness": _response("http://router/readiness", 200),
    }
    monkeypatch.setattr(
        gateway.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(responses),
    )

    response = Response()
    body = await gateway.readiness(response)

    assert response.status_code == 503
    assert body["status"] == "degraded"
    assert body["jdbc_listening"] is None
    assert body["dependencies"] == {
        "jdbc": "unproven",
        "model-service": "ok",
        "query-router": "ok",
    }


@pytest.mark.asyncio
async def test_gateway_health_remains_jdbc_liveness_only(monkeypatch):
    import src.main as gateway

    monkeypatch.setattr(gateway.settings, "GATEWAY_HEALTH_JDBC_PROBE_ENABLED", True)
    monkeypatch.setattr(
        gateway,
        "probe_jdbc_accept_loop_async",
        AsyncMock(return_value=(True, "SSLRequest answered")),
    )
    response = Response()
    body = await gateway.health(response)

    assert response.status_code in (None, 200)
    assert body["status"] == "ok"
    assert body["jdbc_listening"] is True
