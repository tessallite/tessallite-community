"""F-030-01 / F-105-12: query-router /health is serving-readiness, not process liveness.

Test escape: existing suites treated /health as "uvicorn is up". Guard: this module.
Tier: T3 (serving health; Compose/Helm/SPA front door).
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock

# Settings() rejects a weak SYSTEM_ADMIN_PASSWORD from a developer .env (Bug-9309).
# Override before importing the app so collection does not depend on local secrets.
os.environ["SYSTEM_ADMIN_PASSWORD"] = "Cp11HealthTest1Aa"

from fastapi.testclient import TestClient

from src.main import app


def _client(monkeypatch, *, db_ok: bool) -> TestClient:
    async def _ready() -> tuple[bool, str]:
        return (True, "ok") if db_ok else (False, "connection refused")

    monkeypatch.setattr("src.main._metadata_db_ready", _ready)
    monkeypatch.setattr("src.main.refresh_system_snapshot", AsyncMock())
    monkeypatch.setattr("shared.source_pool.close_all_pools", AsyncMock())
    return TestClient(app)


def test_health_returns_503_when_metadata_db_is_down(monkeypatch) -> None:
    with _client(monkeypatch, db_ok=False) as client:
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["service"] == "query-router"


def test_liveness_stays_200_when_metadata_db_is_down(monkeypatch) -> None:
    with _client(monkeypatch, db_ok=False) as client:
        resp = client.get("/liveness")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health_returns_200_when_metadata_db_is_up(monkeypatch) -> None:
    with _client(monkeypatch, db_ok=True) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
