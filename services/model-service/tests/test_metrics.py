from __future__ import annotations

import inspect
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fastapi.params import Query as QueryParam

from src.api.metrics import (
    _build_hourly_volume,
    _calculate_bytes_avoided,
    _calculate_pocket_time_saved,
    get_model_metrics,
)
from tests.conftest import (
    TEST_PROJECT_ID,
    TEST_MODEL_ID,
    async_gen_from,
    make_mock_db,
)

pytestmark = pytest.mark.unit


def test_window_hours_is_bounded():
    """F-030-12 guard: window_hours must be bounded so the hourly-bucket loop
    cannot be driven to an unbounded allocation by an authenticated caller."""
    sig = inspect.signature(get_model_metrics)
    default = sig.parameters["window_hours"].default
    assert isinstance(default, QueryParam)
    assert default.default == 24
    bounds = {type(m).__name__: getattr(m, type(m).__name__.lower(), None) for m in default.metadata}
    assert bounds.get("Ge") == 1
    assert bounds.get("Le") == 720


@pytest.mark.anyio
async def test_hourly_volume_aggregates_in_sql_and_counts_pocket_hits():
    """F-030-12: _build_hourly_volume aggregates via a SQL GROUP BY (date_trunc)
    rather than materialising the window. The DB returns one row per hour with
    pre-summed totals; the helper backfills empty hours and derives source_hits.
    """
    since = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    now = since + timedelta(hours=2)

    # One DB row: hour 00:00 with 5 total, 1 aggregate, 2 pocket, 1 cache
    # re-serve -> 1 source. Bug-6426: cache re-serves are a distinct series and
    # must not be attributed to source.
    row = types.SimpleNamespace(
        hour=since, total=5, aggregate_hits=1, pocket_hits=2, cache_hits=1
    )
    db = AsyncMock()
    result = MagicMock()
    result.all.return_value = [row]
    db.execute = AsyncMock(return_value=result)

    buckets = await _build_hourly_volume(db, (), since, now)

    first = next(b for b in buckets if b.hour == "2026-01-01T00:00:00Z")
    assert first.total == 5
    assert first.aggregate_hits == 1
    assert first.pocket_hits == 2
    assert first.cache_hits == 1
    assert first.source_hits == 1  # 5 - 1 - 2 - 1, derived not loaded
    # Empty hours in the window are backfilled to zero.
    empty = next(b for b in buckets if b.hour == "2026-01-01T01:00:00Z")
    assert empty.total == 0


@pytest.mark.anyio
async def test_bytes_avoided_uses_source_baseline_not_accelerated_scan_bytes():
    """Bug-5321: source 2 GiB vs aggregate 20 MiB reports about 1.98 GiB
    avoided, not the aggregate route's 20 MiB actual scan.
    """
    source_bytes = 2 * 1024 * 1024 * 1024
    aggregate_bytes = 20 * 1024 * 1024
    fingerprint = "fp-source-aggregate"

    source_result = MagicMock()
    source_result.all.return_value = [
        types.SimpleNamespace(query_fingerprint=fingerprint, source_bytes=source_bytes)
    ]
    accelerated_result = MagicMock()
    accelerated_result.all.return_value = [
        types.SimpleNamespace(
            query_fingerprint=fingerprint,
            accelerated_count=1,
            accelerated_bytes=aggregate_bytes,
        )
    ]

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[source_result, accelerated_result])

    avoided = await _calculate_bytes_avoided(
        db,
        TEST_MODEL_ID,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert avoided == source_bytes - aggregate_bytes
    assert avoided != aggregate_bytes


@pytest.mark.anyio
async def test_bytes_avoided_excludes_cache_reserves_bug6426():
    """Bug-6426: a cache re-serve (cache_status='cache_hit', bytes_processed=0)
    must be excluded from the avoided-bytes computation. Both the source-baseline
    query and the accelerated query must carry the ``cache_status IS DISTINCT
    FROM 'cache_hit'`` guard, or each cache re-serve fabricates a full baseline
    of avoided bytes (baseline - 0) and inflates the CFO-facing savings figure.
    """
    captured: list[str] = []
    source_result = MagicMock()
    source_result.all.return_value = []
    accelerated_result = MagicMock()
    accelerated_result.all.return_value = []

    results = iter([source_result, accelerated_result])

    async def _execute(stmt):
        captured.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))
        return next(results)

    db = AsyncMock()
    db.execute = _execute

    await _calculate_bytes_avoided(
        db, TEST_MODEL_ID, datetime(2026, 1, 1, tzinfo=timezone.utc)
    )

    assert captured, "no SQL captured"
    # Every statement that reads bytes must exclude cache re-serves.
    for sql in captured:
        assert "cache_status" in sql and "cache_hit" in sql, (
            "bytes-avoided SQL must exclude cache_status='cache_hit' rows"
        )


@pytest.mark.anyio
async def test_bytes_avoided_not_inflated_by_cache_hit_zero_bytes():
    """Value-level guard: a fingerprint with a source baseline of 2 GiB and ONE
    real aggregate scan of 20 MiB reports ~1.98 GiB avoided. The accelerated
    query (which already excludes cache_status='cache_hit') therefore never sees
    the cache re-serves, so ``accelerated_count`` reflects real executions only
    and the fabricated ``baseline * cache_count`` inflation cannot occur.
    """
    source_bytes = 2 * 1024 * 1024 * 1024
    aggregate_bytes = 20 * 1024 * 1024
    fingerprint = "fp-cache-guard"

    source_result = MagicMock()
    source_result.all.return_value = [
        types.SimpleNamespace(query_fingerprint=fingerprint, source_bytes=source_bytes)
    ]
    # The accelerated query filters cache hits in SQL, so it returns ONLY the
    # single real execution — count 1, not 1 + N cache re-serves.
    accelerated_result = MagicMock()
    accelerated_result.all.return_value = [
        types.SimpleNamespace(
            query_fingerprint=fingerprint,
            accelerated_count=1,
            accelerated_bytes=aggregate_bytes,
        )
    ]
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[source_result, accelerated_result])

    avoided = await _calculate_bytes_avoided(
        db, TEST_MODEL_ID, datetime(2026, 1, 1, tzinfo=timezone.utc)
    )

    # Exactly one real execution's worth of savings — not multiplied by cache hits.
    assert avoided == source_bytes - aggregate_bytes


@pytest.mark.anyio
async def test_bug7460_pocket_savings_are_estimated_from_windowed_query_logs():
    """Three 50ms pocket runs against a 500ms source baseline save 1350ms.

    Both statements must be bounded on both sides of the requested window and
    exclude cache re-serves; lifetime PocketDefinition counters are irrelevant.
    """
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = since + timedelta(hours=24)
    pocket_result = MagicMock()
    pocket_result.one.return_value = types.SimpleNamespace(cnt=3, avg_ms=50)
    source_result = MagicMock()
    source_result.scalar_one.return_value = 500
    captured: list[str] = []
    results = iter([pocket_result, source_result])

    async def _execute(stmt):
        captured.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))
        return next(results)

    db = AsyncMock()
    db.execute = _execute

    saved = await _calculate_pocket_time_saved(
        db, TEST_MODEL_ID, since, now
    )

    assert saved == 1350
    assert len(captured) == 2
    assert all("created_at >=" in sql and "created_at <=" in sql for sql in captured)
    assert all("cache_status" in sql and "cache_hit" in sql for sql in captured)
    assert "'pocket'" in captured[0]
    assert "'source'" in captured[1]


@pytest.mark.anyio
async def test_rollup_excludes_cache_hits_from_acceleration_bug6426(client):
    """Bug-6426: the headline rollup must count a cache re-serve as a DISTINCT
    ``cache_hits`` figure and NOT as an aggregate/pocket acceleration hit. The
    compiled rollup SQL must reference ``cache_status`` so cache_hit rows are
    partitioned out of the acceleration counters.
    """
    captured: list[str] = []
    model = types.SimpleNamespace(project_id=TEST_PROJECT_ID)

    db = make_mock_db()
    db.get = AsyncMock(return_value=model)

    rollup_row = types.SimpleNamespace(
        total=0, aggregate_hits=0, pocket_hits=0, cache_hits=0, unacceleratable_queries=0
    )

    async def _execute(stmt):
        try:
            captured.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))
        except Exception:
            captured.append(str(stmt))
        result = MagicMock()
        result.one.return_value = rollup_row
        result.all.return_value = []
        result.scalars.return_value.all.return_value = []
        result.scalar_one_or_none.return_value = None
        return result

    db.execute = _execute
    with patch("src.api.metrics.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/metrics"
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "cache_hits" in body, "rollup must report cache_hits distinctly"
    assert any("cache_status" in c and "cache_hit" in c for c in captured), (
        "acceleration rollup must partition cache_status='cache_hit' rows out"
    )


@pytest.mark.anyio
async def test_eligible_hit_rate_excludes_unacceleratable_raw_routes_bug8180(client):
    """Bug-8180: route_type="raw" rows (explicit ungrouped flat-row detail
    pulls) are structurally impossible for any aggregate/pocket to serve, so
    they must not count against acceleration coverage. Known values: 10 total
    queries, 2 aggregate hits, 3 pocket hits, 1 cache re-serve, 4
    unacceleratable (raw) rows.

    hit_rate            = (2 + 3) / 10   = 0.5     (unchanged, existing metric)
    eligible_queries    = 10 - 4         = 6
    eligible_hit_rate   = (2 + 3) / 6    = 0.8333
    unacceleratable     = 4 / 10         = 0.4
    unacceleratable_queries = 4
    """
    model = types.SimpleNamespace(project_id=TEST_PROJECT_ID)

    db = make_mock_db()
    db.get = AsyncMock(return_value=model)

    rollup_row = types.SimpleNamespace(
        total=10,
        aggregate_hits=2,
        pocket_hits=3,
        cache_hits=1,
        unacceleratable_queries=4,
    )

    async def _execute(stmt):
        result = MagicMock()
        result.one.return_value = rollup_row
        result.all.return_value = []
        result.scalars.return_value.all.return_value = []
        result.scalar_one_or_none.return_value = None
        return result

    db.execute = _execute
    with patch("src.api.metrics.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/metrics"
        )
    assert resp.status_code == 200
    body = resp.json()

    assert body["total_queries"] == 10
    assert body["aggregate_hits"] == 2
    assert body["pocket_hits"] == 3
    assert body["hit_rate"] == 0.5
    assert body["unacceleratable_queries"] == 4
    assert body["eligible_queries"] == 6
    assert body["eligible_hit_rate"] == pytest.approx(0.8333, abs=1e-4)
    assert body["unacceleratable"] == pytest.approx(0.4, abs=1e-4)


@pytest.mark.anyio
async def test_eligible_hit_rate_zero_eligible_queries_is_zero_not_divide_error(client):
    """Bug-8180 boundary: when every non-cache query in the window is
    unacceleratable (raw), eligible_queries is 0 and eligible_hit_rate must
    report 0.0 rather than raising a ZeroDivisionError."""
    model = types.SimpleNamespace(project_id=TEST_PROJECT_ID)

    db = make_mock_db()
    db.get = AsyncMock(return_value=model)

    rollup_row = types.SimpleNamespace(
        total=3, aggregate_hits=0, pocket_hits=0, cache_hits=0, unacceleratable_queries=3
    )

    async def _execute(stmt):
        result = MagicMock()
        result.one.return_value = rollup_row
        result.all.return_value = []
        result.scalars.return_value.all.return_value = []
        result.scalar_one_or_none.return_value = None
        return result

    db.execute = _execute
    with patch("src.api.metrics.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/metrics"
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["eligible_queries"] == 0
    assert body["eligible_hit_rate"] == 0.0
    assert body["unacceleratable"] == 1.0


@pytest.mark.anyio
async def test_unknown_model_returns_404(client):
    """F-030-18: a non-existent / foreign model is a 404, not an all-zero payload."""
    db = make_mock_db()
    db.get = AsyncMock(return_value=None)
    with patch("src.api.metrics.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/metrics"
        )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_introspect_rows_excluded_from_rollup(client):
    """F-030-09: the route-type rollup query must filter out introspect rows.

    We assert the compiled WHERE clause carries a NOT IN ('introspect') guard
    on route_type so probe rows never reach the counts.
    """
    captured: list[str] = []
    model = types.SimpleNamespace(project_id=TEST_PROJECT_ID)

    db = make_mock_db()
    db.get = AsyncMock(return_value=model)

    rollup_row = types.SimpleNamespace(
        total=0,
        aggregate_hits=0,
        pocket_hits=0,
        cache_hits=0,
        unacceleratable_queries=0,
        bytes_avoided=0,
    )

    async def _execute(stmt):
        try:
            captured.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))
        except Exception:
            captured.append(str(stmt))
        result = MagicMock()
        result.one.return_value = rollup_row
        result.all.return_value = []
        result.scalars.return_value.all.return_value = []
        result.scalar_one_or_none.return_value = None
        return result

    db.execute = _execute
    with patch("src.api.metrics.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/metrics"
        )
    assert resp.status_code == 200
    assert any("introspect" in c for c in captured), "rollup must exclude introspect rows"


@pytest.mark.anyio
async def test_metrics_403_for_user_with_no_project_binding(client):
    """F-030-02: model metrics expose per-model usage; a tenant user with no
    binding to the project (project has other bindings, so the bootstrap-admin
    path does not apply) must be denied 403, not read another project's data."""
    rbac_db = AsyncMock()
    rbac_result = MagicMock()
    rbac_result.scalar_one_or_none.return_value = None  # no binding
    rbac_result.first.return_value = (object(),)  # bindings exist probe
    rbac_db.execute = AsyncMock(return_value=rbac_result)

    model = types.SimpleNamespace(id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID)
    data_db = make_mock_db()
    data_db.get = AsyncMock(return_value=model)
    with patch("src.auth.rbac.get_tenant_db", async_gen_from(rbac_db)):
        with patch("src.api.metrics.get_tenant_db", async_gen_from(data_db)):
            resp = await client.get(
                f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/metrics"
            )
    assert resp.status_code == 403
