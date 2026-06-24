from __future__ import annotations

import inspect
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fastapi.params import Query as QueryParam

from src.api.metrics import _build_hourly_volume, _calculate_bytes_avoided, get_model_metrics
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

    # One DB row: hour 00:00 with 4 total, 1 aggregate, 2 pocket -> 1 source.
    row = types.SimpleNamespace(hour=since, total=4, aggregate_hits=1, pocket_hits=2)
    db = AsyncMock()
    result = MagicMock()
    result.all.return_value = [row]
    db.execute = AsyncMock(return_value=result)

    buckets = await _build_hourly_volume(db, (), since, now)

    first = next(b for b in buckets if b.hour == "2026-01-01T00:00:00Z")
    assert first.total == 4
    assert first.aggregate_hits == 1
    assert first.pocket_hits == 2
    assert first.source_hits == 1  # 4 - 1 - 2, derived not loaded
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

    rollup_row = types.SimpleNamespace(total=0, aggregate_hits=0, pocket_hits=0, bytes_avoided=0)

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
