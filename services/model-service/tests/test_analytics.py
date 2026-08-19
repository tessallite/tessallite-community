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
                total=100, agg_hits=60, accel_hits=75, unacceleratable=0,
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


@pytest.mark.asyncio
async def test_summary_acceleration_rate_excludes_unacceleratable_raw_f030_05(client):
    """F-030-05 (Bug-9134): Usage Analytics must use the SAME eligible denominator
    as Model Health — structurally unacceleratable ``raw`` rows are excluded. With
    100 total, 75 accelerated, 20 raw: rate = 75 / (100-20) = 93.75, NOT 75/100=75."""
    db = AsyncMock()
    call_count = 0

    async def _multi_execute(stmt):
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            result.scalar_one.return_value = 100
        elif call_count == 2:
            result.one.return_value = types.SimpleNamespace(
                total=100, agg_hits=60, accel_hits=75, unacceleratable=20,
            )
        elif call_count == 3:
            result.scalar_one.return_value = 42.5
        elif call_count == 4:
            result.first.return_value = None
        return result

    db.execute = _multi_execute
    db.get = AsyncMock(return_value=types.SimpleNamespace(id=MODEL_ID, project_id=PROJECT_ID))
    with patch("src.api.analytics.get_tenant_db", lambda tid: _yield(db)):
        resp = await client.get(
            f"/api/v1/projects/{PROJECT_ID}/models/{MODEL_ID}/analytics/summary?days=7"
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["acceleration_rate"] == 93.8   # 75/80*100, rounded to 1 dp
    assert data["aggregate_hit_rate"] == 75.0  # 60/80*100


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
            result.one.return_value = types.SimpleNamespace(total=0, agg_hits=0, accel_hits=0, unacceleratable=0)
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


# ---------------------------------------------------------------------------
# Bug-6425: member-discovery queries excluded from usage analytics
# ---------------------------------------------------------------------------


def test_exclude_probes_emits_discovery_and_introspect_filters():
    """Bug-6425 (+ F-030-09): every QueryLog aggregation must drop introspect
    probe rows AND member-discovery rows (protocol='discover_members'). Compile
    the statement and assert both exclusions are present, and that the discovery
    guard uses IS DISTINCT FROM so NULL-protocol rows are still counted."""
    from sqlalchemy import func, select

    from shared.db.models import QueryLog
    from src.api.analytics import _DISCOVERY_PROTOCOL, _exclude_probes

    assert _DISCOVERY_PROTOCOL == "discover_members"
    stmt = _exclude_probes(select(func.count()).select_from(QueryLog))
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "IS DISTINCT FROM 'discover_members'" in sql
    assert "introspect" in sql


def test_metrics_defines_discovery_protocol_constant():
    """Producer/consumer contract: the model-service consumer excludes exactly
    the protocol string the query-router producer tags discovery rows with."""
    from src.api import metrics

    assert metrics._DISCOVERY_PROTOCOL == "discover_members"


@pytest.mark.asyncio
async def test_summary_acceleration_rate_excludes_cache_reserves_bug6426(client):
    """Bug-6426: the headline acceleration-rate query must exclude cache
    re-serves (cache_status='cache_hit') from the accelerated counters, so the
    number a business user reads is real acceleration — not inflated by results
    served from the in-TTL result cache. Compile the hit_stmt and assert the
    cache guard is present."""
    captured: list[str] = []
    db = AsyncMock()
    call_count = 0

    async def _multi_execute(stmt):
        nonlocal call_count
        call_count += 1
        try:
            captured.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))
        except Exception:
            captured.append(str(stmt))
        result = MagicMock()
        if call_count == 1:
            result.scalar_one.return_value = 100
        elif call_count == 2:
            result.one.return_value = types.SimpleNamespace(
                total=100, agg_hits=60, accel_hits=75, unacceleratable=0,
            )
        elif call_count == 3:
            result.scalar_one.return_value = 42.5
        elif call_count == 4:
            result.first.return_value = None
        return result

    db.execute = _multi_execute
    db.get = AsyncMock(return_value=types.SimpleNamespace(id=MODEL_ID, project_id=PROJECT_ID))
    with patch("src.api.analytics.get_tenant_db", lambda tid: _yield(db)):
        resp = await client.get(
            f"/api/v1/projects/{PROJECT_ID}/models/{MODEL_ID}/analytics/summary?days=7"
        )
    assert resp.status_code == 200
    # The accelerated-hits statement (2nd execute) must carry the cache guard.
    hit_sql = captured[1]
    assert "cache_status" in hit_sql and "cache_hit" in hit_sql, (
        "acceleration-rate query must exclude cache_status='cache_hit' rows"
    )


@pytest.mark.asyncio
async def test_estimated_savings_excludes_cache_reserves_bug6426(client):
    """Bug-6426: estimated-savings is the direct CFO-facing time_saved number.
    A cache re-serve carries execution_ms=0 and did not execute a route —
    counting it drags accel_avg toward 0 and inflates accel_count, so
    time_saved = (source_avg - accel_avg) * accel_count is inflated on BOTH
    factors. Both the accelerated and source statements must exclude cache
    re-serves; assert the SQL guard so a value regression is impossible."""
    captured: list[str] = []
    db = AsyncMock()
    call_count = 0

    async def _multi_execute(stmt):
        nonlocal call_count
        call_count += 1
        try:
            captured.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))
        except Exception:
            captured.append(str(stmt))
        result = MagicMock()
        if call_count == 1:
            result.scalar_one.return_value = 100  # total
        elif call_count == 2:
            result.one.return_value = types.SimpleNamespace(cnt=10, avg_ms=20.0)
        elif call_count == 3:
            result.scalar_one.return_value = 500.0  # source avg
        return result

    db.execute = _multi_execute
    db.get = AsyncMock(return_value=types.SimpleNamespace(id=MODEL_ID, project_id=PROJECT_ID))
    with patch("src.api.analytics.get_tenant_db", lambda tid: _yield(db)):
        resp = await client.get(
            f"/api/v1/projects/{PROJECT_ID}/models/{MODEL_ID}/analytics/estimated-savings?days=30"
        )
    assert resp.status_code == 200
    # The accelerated statement (2nd) and source statement (3rd) must both
    # exclude cache re-serves.
    accel_sql = captured[1]
    source_sql = captured[2]
    assert "cache_status" in accel_sql and "cache_hit" in accel_sql, (
        "accelerated-savings query must exclude cache_status='cache_hit' rows"
    )
    assert "cache_status" in source_sql and "cache_hit" in source_sql, (
        "source-baseline query must exclude cache_status='cache_hit' rows"
    )
    data = resp.json()
    # Value sanity: (500 - 20) * 10 = 4800, with real executions only.
    assert data["time_saved_ms"] == 4800
