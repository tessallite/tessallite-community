"""Bug-9054: agent-service readiness is DB-aware while health stays local.

Test escape: the process-only health response had no dependency assertion.
Guard: these route tests plus the shared metadata probe test.
Tier: T2 (service readiness contract).
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import Response


@pytest.mark.asyncio
async def test_readiness_fails_when_metadata_db_is_unavailable(monkeypatch):
    import src.main as agent_service

    monkeypatch.setattr(
        agent_service,
        "_metadata_db_ready",
        AsyncMock(return_value=(False, "down")),
    )
    response = Response()

    body = await agent_service.readiness(response)

    assert response.status_code == 503
    assert body == {
        "status": "degraded",
        "service": "agent-service",
        "detail": "metadata database unreachable",
    }
    assert await agent_service.health() == {"status": "ok", "service": "agent-service"}
    assert await agent_service.liveness() == {"status": "ok", "service": "agent-service"}


@pytest.mark.asyncio
async def test_readiness_succeeds_when_metadata_db_is_available(monkeypatch):
    import src.main as agent_service

    monkeypatch.setattr(
        agent_service,
        "_metadata_db_ready",
        AsyncMock(return_value=(True, "ok")),
    )
    response = Response()

    body = await agent_service.readiness(response)

    assert response.status_code in (None, 200)
    assert body == {"status": "ok", "service": "agent-service"}
