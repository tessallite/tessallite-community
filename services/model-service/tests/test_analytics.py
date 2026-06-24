"""Tests for usage analytics API endpoints (Block I)."""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from src.main import app
from src.auth.middleware import get_current_user, CurrentUser

pytestmark = pytest.mark.unit

NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
PROJECT_ID = uuid.uuid4()
MODEL_ID = uuid.uuid4()


def _user() -> CurrentUser:
    return CurrentUser(
        user_id="user@example.com",
        tenant_id="acme",
        email="user@example.com",
    )


@pytest.fixture(autouse=True)
def auth_override():
    app.dependency_overrides[get_current_user] = _user
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
async def client():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac


async def _yield(value):
    yield value


def _mock_db_returning(rows, *, model_in_project: bool = True):
    """Mock DB whose execute returns the given rows for .all() and .scalars().all().

    ``db.get`` returns a model whose ``project_id`` matches ``PROJECT_ID`` so
    the F-030-02 model-in-project guard (``ensure_model_in_project``) passes;
    set ``model_in_project=False`` to simulate a foreign/missing model (404).
    """
    db = AsyncMock()
    result = MagicMock()
    result.all.return_value = rows
    result.scalars.return_value.all.return_value = rows
    result.scalar_one.return_value = len(rows)
    result.first.return_value = rows[0] if rows else None
    result.one.return_value = rows[0] if rows else types.SimpleNamespace(total=0, hits=0)
    db.execute = AsyncMock(return_value=result)
    if model_in_project:
        db.get = AsyncMock(return_value=types.SimpleNamespace(
            id=MODEL_ID, project_id=PROJECT_ID,
        ))
    else:
        db.get = AsyncMock(return_value=None)
    return db


# ---------------------------------------------------------------------------
# Query volume
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_query_volume_returns_buckets(client):
    rows = [
        types.SimpleNamespace(bucket=datetime(2026, 4, 28, tzinfo=timezone.utc), cnt=5),
        types.SimpleNamespace(bucket=datetime(2026, 4, 29, tzinfo=timezone.utc), cnt=12),
    ]
    db = _mock_db_returning(rows)
    with patch("src.api.analytics.get_tenant_db", lambda tid: _yield(db)):
        resp = await client.get(
            f"/api/v1/projects/{PROJECT_ID}/models/{MODEL_ID}/analytics/query-volume?days=7"
        )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0]["count"] == 5
    assert data[1]["count"] == 12


# ---------------------------------------------------------------------------
# Top measures
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_top_measures_ranked(client):
    rows = [
        types.SimpleNamespace(requested_measures=["revenue"], total=42),
        types.SimpleNamespace(requested_measures=["cost"], total=18),
    ]
    db = _mock_db_returning(rows)
    with patch("src.api.analytics.get_tenant_db", lambda tid: _yield(db)):
        resp = await client.get(
            f"/api/v1/projects/{PROJECT_ID}/models/{MODEL_ID}/analytics/top-measures?limit=5"
        )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0]["measure_name"] == "revenue"
    assert data[0]["query_count"] == 42


# ---------------------------------------------------------------------------
# Top aggregates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_top_aggregates(client):
    agg_id = uuid.uuid4()
    agg = types.SimpleNamespace(
        id=agg_id,
        physical_table_name="agg_region_daily",
        grain=["region", "day"],
    )
    db = AsyncMock()
    call_count = 0

    async def _multi_execute(stmt):
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            result.all.return_value = [
                types.SimpleNamespace(aggregate_id=agg_id, cnt=150),
            ]
        elif call_count == 2:
            result.scalars.return_value.all.return_value = [agg]
        return result

    db.execute = _multi_execute
    db.get = AsyncMock(return_value=types.SimpleNamespace(id=MODEL_ID, project_id=PROJECT_ID))
    with patch("src.api.analytics.get_tenant_db", lambda tid: _yield(db)):
        resp = await client.get(
            f"/api/v1/projects/{PROJECT_ID}/models/{MODEL_ID}/analytics/top-aggregates"
        )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["query_count"] == 150
    assert data[0]["physical_table_name"] == "agg_region_daily"


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summary_aggregates_correctly(client):
    db = AsyncMock()
    call_count = 0

    async def _multi_execute(stmt):
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            result.scalar_one.return_value = 100
        elif call_count == 2:
            # F-030-08: hit_stmt now returns agg_hits (aggregate-only) and
            # accel_hits (aggregate + pocket) — 60 aggregate, 75 accelerated.
            result.one.return_value = types.SimpleNamespace(
                total=100, agg_hits=60, accel_hits=75,
            )
        elif call_count == 3:
            result.scalar_one.return_value = 42.5
        elif call_count == 4:
            result.first.return_value = types.SimpleNamespace(
                requested_measures=["revenue"], total=50,
            )
        return result

    db.execute = _multi_execute
    db.get = AsyncMock(return_value=types.SimpleNamespace(id=MODEL_ID, project_id=PROJECT_ID))
    with patch("src.api.analytics.get_tenant_db", lambda tid: _yield(db)):
        resp = await client.get(
            f"/api/v1/projects/{PROJECT_ID}/models/{MODEL_ID}/analytics/summary?days=7"
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_queries"] == 100
    assert data["aggregate_hit_rate"] == 60.0
    # F-030-08: headline combined acceleration rate counts aggregate + pocket.
    assert data["acceleration_rate"] == 75.0
    assert data["avg_response_ms"] == 42.5
    assert data["top_measure"] == "revenue"


# ---------------------------------------------------------------------------
# Empty model (no queries)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summary_empty_model(client):
    db = AsyncMock()
    call_count = 0

    async def _multi_execute(stmt):
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            result.scalar_one.return_value = 0
        elif call_count == 2:
            result.one.return_value = types.SimpleNamespace(total=0, agg_hits=0, accel_hits=0)
        elif call_count == 3:
            result.scalar_one.return_value = None
        elif call_count == 4:
            result.first.return_value = None
        return result

    db.execute = _multi_execute
    db.get = AsyncMock(return_value=types.SimpleNamespace(id=MODEL_ID, project_id=PROJECT_ID))
    with patch("src.api.analytics.get_tenant_db", lambda tid: _yield(db)):
        resp = await client.get(
            f"/api/v1/projects/{PROJECT_ID}/models/{MODEL_ID}/analytics/summary"
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_queries"] == 0
    assert data["aggregate_hit_rate"] == 0.0
    assert data["acceleration_rate"] == 0.0
    assert data["avg_response_ms"] is None
    assert data["top_measure"] is None


# ---------------------------------------------------------------------------
# Query volume with no data
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_query_volume_empty(client):
    db = _mock_db_returning([])
    with patch("src.api.analytics.get_tenant_db", lambda tid: _yield(db)):
        resp = await client.get(
            f"/api/v1/projects/{PROJECT_ID}/models/{MODEL_ID}/analytics/query-volume"
        )
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# F-030-02: project RBAC + model-in-project enforcement (fail-closed)
# ---------------------------------------------------------------------------


def _rbac_db(role: str | None):
    """RBAC resolution DB: a binding with ``role`` (or no binding → bootstrap)."""
    from contextlib import contextmanager

    @contextmanager
    def _cm():
        mock_db = AsyncMock()
        result = MagicMock()
        if role is None:
            # No binding rows + bindings exist for the project → 403 (a user
            # bound to no project cannot read another project's analytics).
            result.scalar_one_or_none.return_value = None
            result.first.return_value = (uuid.uuid4(),)  # bindings exist probe
        else:
            result.scalar_one_or_none.return_value = types.SimpleNamespace(
                id=uuid.uuid4(),
                user_identity="user@example.com",
                project_id=PROJECT_ID,
                model_id=None,
                role=role,
            )
            result.first.return_value = (uuid.uuid4(),)
        mock_db.execute = AsyncMock(return_value=result)
        with patch("src.auth.rbac.get_tenant_db", lambda tid: _yield(mock_db)):
            yield

    return _cm()


@pytest.mark.asyncio
async def test_analytics_403_for_user_with_no_project_binding(client):
    """A tenant user with no binding to the project (and the project HAS other
    bindings, so the bootstrap-admin path does not apply) cannot read its
    usage analytics — 403, not the cross-project data leak F-030-02 describes."""
    db = _mock_db_returning([])
    with _rbac_db(None):
        with patch("src.api.analytics.get_tenant_db", lambda tid: _yield(db)):
            resp = await client.get(
                f"/api/v1/projects/{PROJECT_ID}/models/{MODEL_ID}/analytics/summary"
            )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_analytics_viewer_allowed(client):
    """A project viewer may read analytics (the gate admits viewer+)."""
    db = _mock_db_returning([])
    with _rbac_db("viewer"):
        with patch("src.api.analytics.get_tenant_db", lambda tid: _yield(db)):
            resp = await client.get(
                f"/api/v1/projects/{PROJECT_ID}/models/{MODEL_ID}/analytics/query-volume"
            )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_analytics_404_for_foreign_model(client):
    """A model id that does not belong to the path project must 404 — without
    the model-in-project guard an analytics path with a foreign model_id would
    read another project's per-model usage (F-030-02)."""
    db = _mock_db_returning([], model_in_project=False)
    with patch("src.api.analytics.get_tenant_db", lambda tid: _yield(db)):
        resp = await client.get(
            f"/api/v1/projects/{PROJECT_ID}/models/{MODEL_ID}/analytics/summary"
        )
    assert resp.status_code == 404
