"""F-030-01 / F-105-12 / G-028-01: model-service /health is serving-readiness.

Test escape: existing suites treated /health as "uvicorn is up". Guard: this module.
Tier: T3 (serving health; Compose/Helm/SPA front door).
"""
from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from src.main import app


class _DownSession:
    async def __aenter__(self):
        raise RuntimeError("connection refused")

    async def __aexit__(self, *a):
        return False


class _OkResult:
    def scalar(self):
        return "0212"


class _OkSession:
    async def execute(self, _stmt):
        return _OkResult()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _client(monkeypatch, *, db_ok: bool) -> TestClient:
    monkeypatch.setattr("src.main.refresh_system_snapshot", AsyncMock())
    monkeypatch.setattr("shared.source_pool.close_all_pools", AsyncMock())
    monkeypatch.setattr("shared.licensing.beacon.beacon_startup_validate", lambda: None)
    monkeypatch.setattr(
        "shared.db.session.SystemSessionLocal",
        (lambda: _OkSession()) if db_ok else (lambda: _DownSession()),
    )
    return TestClient(app)


def test_health_returns_503_when_metadata_db_is_down(monkeypatch) -> None:
    with _client(monkeypatch, db_ok=False) as client:
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["service"] == "model-service"


def test_liveness_stays_200_when_metadata_db_is_down(monkeypatch) -> None:
    with _client(monkeypatch, db_ok=False) as client:
        resp = client.get("/liveness")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health_returns_200_and_schema_revision_when_db_is_up(monkeypatch) -> None:
    with _client(monkeypatch, db_ok=True) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body.get("schema_revision") == "0212"
