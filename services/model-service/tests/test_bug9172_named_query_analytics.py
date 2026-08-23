"""Bug-9172 Named Query analytics and tenant/model/NQ scope guards."""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.auth.middleware import CurrentUser, get_current_user
from src.main import app
from shared.config.resolver import clear_cache
from shared.db.session import get_system_db

pytestmark = pytest.mark.unit

PROJECT_ID = uuid.uuid4()
MODEL_ID = uuid.uuid4()
NQ_ID = uuid.uuid4()


def _user() -> CurrentUser:
    return CurrentUser(
        user_id="viewer@example.com",
        tenant_id="tenant-a",
        email="viewer@example.com",
    )


class _Result:
    def __init__(self, *, nq_exists=True, summary=None, reasons=()):
        self._nq_exists = nq_exists
        self._summary = summary
        self._reasons = list(reasons)

    def scalar_one_or_none(self):
        return NQ_ID if self._nq_exists else None

    def one(self):
        return self._summary

    def all(self):
        return list(self._reasons)


class _SystemSettingResult:
    def __init__(self, db):
        self._db = db

    def scalar_one_or_none(self):
        return self._db.value


async def _yield(db):
    yield db


def _system_db_override(db):
    async def _dependency():
        yield db

    return _dependency


def _db(*, nq_exists=True, summary=None, reasons=()):
    db = AsyncMock()
    db.get = AsyncMock(
        return_value=types.SimpleNamespace(id=MODEL_ID, project_id=PROJECT_ID)
    )
    db.execute = AsyncMock(
        side_effect=[
            _Result(nq_exists=nq_exists),
            _Result(summary=summary),
            _Result(reasons=reasons),
        ]
    )
    return db


@pytest.fixture(autouse=True)
def auth_override():
    app.dependency_overrides[get_current_user] = _user
    system_db = types.SimpleNamespace(value=None)
    system_db.execute = AsyncMock(
        side_effect=lambda _statement: _SystemSettingResult(system_db)
    )
    app.dependency_overrides[get_system_db] = _system_db_override(system_db)
    yield
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_system_db, None)


@pytest.fixture
async def client():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac


@pytest.mark.asyncio
async def test_named_query_analytics_reports_attribution_and_nq_recommendation(client):
    summary = types.SimpleNamespace(
        total_queries=4,
        fallback_queries=3,
        materialized_queries=1,
        fallback_failures=0,
        avg_fallback_execution_ms=120,
        avg_fallback_bytes_processed=4096,
    )
    reasons = [types.SimpleNamespace(reason="artifact_not_fresh", count=3)]
    db = _db(summary=summary, reasons=reasons)
    with patch(
        "src.api.named_queries.get_tenant_db", lambda _tenant: _yield(db)
    ):
        response = await client.get(
            f"/api/v1/projects/{PROJECT_ID}/models/{MODEL_ID}/named-queries/{NQ_ID}/analytics?days=7"
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["named_query_id"] == str(NQ_ID)
    assert body["fallback_queries"] == 3
    assert body["fallback_rate"] == 75.0
    assert body["fallback_reasons"] == [
        {"reason": "artifact_not_fresh", "count": 3}
    ]
    assert body["recommendation"] == "repair_named_query_materialisation"
    assert "aggregate" not in body["recommendation"]
    assert "aggregate" not in (body["recommendation_reason"] or "")


@pytest.mark.asyncio
async def test_named_query_analytics_does_not_leak_foreign_model_or_nq(client):
    db = _db(nq_exists=False)
    db.get = AsyncMock(
        return_value=types.SimpleNamespace(id=MODEL_ID, project_id=uuid.uuid4())
    )
    with patch(
        "src.api.named_queries.get_tenant_db", lambda _tenant: _yield(db)
    ):
        response = await client.get(
            f"/api/v1/projects/{PROJECT_ID}/models/{MODEL_ID}/named-queries/{NQ_ID}/analytics"
        )

    # Project/model authority fails before the Named Query and QueryLog reads.
    assert response.status_code == 404
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_named_query_analytics_unknown_nq_stops_before_querylog(client):
    summary = types.SimpleNamespace(
        total_queries=99,
        fallback_queries=99,
        materialized_queries=0,
        fallback_failures=0,
        avg_fallback_execution_ms=999,
        avg_fallback_bytes_processed=999,
    )
    db = _db(nq_exists=False, summary=summary)
    with patch(
        "src.api.named_queries.get_tenant_db", lambda _tenant: _yield(db)
    ):
        response = await client.get(
            f"/api/v1/projects/{PROJECT_ID}/models/{MODEL_ID}/named-queries/{uuid.uuid4()}/analytics"
        )

    assert response.status_code == 404
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_named_query_analytics_one_off_fallback_has_no_sustained_recommendation(client):
    summary = types.SimpleNamespace(
        total_queries=1,
        fallback_queries=1,
        materialized_queries=0,
        fallback_failures=0,
        avg_fallback_execution_ms=120,
        avg_fallback_bytes_processed=4096,
    )
    db = _db(summary=summary, reasons=[types.SimpleNamespace(reason="overdue", count=1)])
    with patch(
        "src.api.named_queries.get_tenant_db", lambda _tenant: _yield(db)
    ):
        response = await client.get(
            f"/api/v1/projects/{PROJECT_ID}/models/{MODEL_ID}/named-queries/{NQ_ID}/analytics"
        )

    assert response.status_code == 200
    assert response.json()["recommendation"] == "none"


@pytest.mark.asyncio
async def test_bug9172_sol_r1_f2_cost_average_excludes_cache_hit_sentinels(client):
    """B9172-SOL-R1-F2: cache-hit zero sentinels never enter cost averages."""
    summary = types.SimpleNamespace(
        total_queries=3,
        fallback_queries=3,
        materialized_queries=0,
        fallback_failures=0,
        avg_fallback_execution_ms=120,
        avg_fallback_bytes_processed=4096,
    )
    db = _db(summary=summary, reasons=[])
    with patch(
        "src.api.named_queries.get_tenant_db", lambda _tenant: _yield(db)
    ):
        response = await client.get(
            f"/api/v1/projects/{PROJECT_ID}/models/{MODEL_ID}/named-queries/{NQ_ID}/analytics"
        )

    assert response.status_code == 200
    summary_stmt = db.execute.await_args_list[1].args[0]
    sql = str(summary_stmt)
    assert "query_logs.cache_status IS NULL" in sql
    assert "query_logs.cache_status !=" in sql


@pytest.mark.asyncio
async def test_bug9172_sol_r2_f4_system_override_reaches_recommendation(client):
    """B9172-SOL-R1-F4: stored system policy changes the real route result."""
    def _summary(count: int):
        return types.SimpleNamespace(
            total_queries=count,
            fallback_queries=count,
            materialized_queries=0,
            fallback_failures=0,
            avg_fallback_execution_ms=120,
            avg_fallback_bytes_processed=4096,
        )

    db_three = _db(summary=_summary(3), reasons=[])
    system_db = types.SimpleNamespace(value=4)
    system_db.execute = AsyncMock(
        side_effect=lambda _statement: _SystemSettingResult(system_db)
    )
    app.dependency_overrides[get_system_db] = _system_db_override(system_db)
    clear_cache()
    with (
        patch(
            "src.api.named_queries.get_tenant_db",
            lambda _tenant: _yield(db_three),
        ),
    ):
        response_three = await client.get(
            f"/api/v1/projects/{PROJECT_ID}/models/{MODEL_ID}/named-queries/{NQ_ID}/analytics"
        )
    assert response_three.status_code == 200
    assert response_three.json()["recommendation"] == "none"

    db_four = _db(summary=_summary(4), reasons=[])
    with (
        patch(
            "src.api.named_queries.get_tenant_db",
            lambda _tenant: _yield(db_four),
        ),
    ):
        response_four = await client.get(
            f"/api/v1/projects/{PROJECT_ID}/models/{MODEL_ID}/named-queries/{NQ_ID}/analytics"
        )
    assert response_four.status_code == 200
    assert response_four.json()["recommendation"] == "repair_named_query_materialisation"
    assert system_db.execute.await_count == 1
