from __future__ import annotations

import types
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError, InvalidRequestError

from shared.db.models import Model, PocketDefinition, PocketPredicate, PocketRefreshPolicy

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    TEST_TENANT,
    async_gen_from,
    client,
    make_mock_db,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/pockets"
AUTH_HEADERS = {"Authorization": "Bearer test-token"}


def _result_with(items):
    return types.SimpleNamespace(
        scalars=lambda: types.SimpleNamespace(all=lambda: items),
    )


def _router_response(**overrides) -> dict:
    base = {
        "ok": True,
        "errors": [],
        "select_star": True,
        "from_tables": ["modely"],
        "has_complex_sql": False,
        "has_unresolvable_where": False,
        "grain": [],
        "query_fingerprint": "abc123",
        "filters": [],
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_list_pockets_rejects_wrong_project_scope(client):
    db = make_mock_db()
    wrong_model = types.SimpleNamespace(id=TEST_MODEL_ID, project_id="different-project")

    async def _get(cls, obj_id):
        if cls is Model and str(obj_id) == str(TEST_MODEL_ID):
            return wrong_model
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)):
        resp = await client.get(PREFIX)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Model not found"


@pytest.mark.asyncio
async def test_pocket_metrics_returns_expected_shape(client):
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID)
    now = datetime.now(timezone.utc)
    pockets = [
        types.SimpleNamespace(
            id="p1",
            physical_table_name="pocket_a",
            status="fresh",
            hit_count=8,
            ttl_days=14,
            time_saved_ms_total=1200,
            storage_bytes=100,
            retired_at=None,
            last_access_at=now,
            # F-005-22: fresh pocket that HAS matched since its last refresh, so
            # it does not count toward zero_match_fresh and the skip-reason query
            # is not fired (keeps the two-execute mock below intact).
            last_refresh_at=now,
            last_match_at=now,
        ),
        types.SimpleNamespace(
            id="p2",
            physical_table_name="pocket_b",
            status="stale",
            hit_count=1,
            ttl_days=14,
            time_saved_ms_total=50,
            storage_bytes=0,
            retired_at=now,
            last_access_at=now,
            last_refresh_at=now,
            last_match_at=now,
        ),
    ]

    async def _get(cls, obj_id):
        if cls is Model and str(obj_id) == str(TEST_MODEL_ID):
            return scoped_model
        return None

    db.get = AsyncMock(side_effect=_get)

    # F-005-08: the metrics endpoint now runs two queries — the pocket select,
    # then a route_type count over QueryLog to compute the genuine hit RATIO
    # (pocket-routed queries / all queries). Mock both in order.
    route_counts = types.SimpleNamespace(
        all=lambda: [("pocket", 3), ("source", 7)],
    )
    calls = {"n": 0}

    async def _execute(_stmt):
        calls["n"] += 1
        if calls["n"] == 1:
            return _result_with(pockets)
        return route_counts

    db.execute = _execute

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)):
        resp = await client.get(f"{PREFIX}/metrics")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_pockets"] == 1
    assert data["fresh_pockets"] == 1
    assert data["retired_pockets"] == 1
    assert "top_pockets" in data
    assert [p["pocket_id"] for p in data["top_pockets"]] == ["p1"]
    # F-005-08: hit ratio is pocket queries / all queries (3/10 = 0.3), a
    # fraction in [0,1] — NOT total hits / pocket count (which was 9/2 = 4.5).
    assert data["pocket_hit_rate"] == pytest.approx(0.3)
    # time_saved is now the genuine saved total (sum of per-pocket
    # time_saved_ms_total), which the route-time accumulator fills with
    # baseline-minus-pocket figures.
    assert data["pocket_time_saved_ms"] == 1200


@pytest.mark.asyncio
async def test_pocket_metrics_zero_match_signal(client):
    """F-005-22: a fresh pocket that has not matched since its last refresh is
    counted in zero_match_fresh_pockets and triggers the top-skip-reason query."""
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID)
    older = datetime(2020, 1, 1, tzinfo=timezone.utc)
    newer = datetime(2020, 2, 1, tzinfo=timezone.utc)
    pockets = [
        types.SimpleNamespace(
            id="p1", physical_table_name="pocket_a", status="fresh",
            hit_count=0, ttl_days=14, time_saved_ms_total=0, storage_bytes=100,
            retired_at=None, last_access_at=None,
            # refreshed AFTER its last match (or never matched) => zero-match.
            last_refresh_at=newer, last_match_at=older,
        ),
    ]

    async def _get(cls, obj_id):
        if cls is Model and str(obj_id) == str(TEST_MODEL_ID):
            return scoped_model
        return None

    db.get = AsyncMock(side_effect=_get)

    route_counts = types.SimpleNamespace(all=lambda: [("source", 5)])
    skip_top = types.SimpleNamespace(first=lambda: ("no_tenant_filter", 4))
    calls = {"n": 0}

    async def _execute(_stmt):
        calls["n"] += 1
        if calls["n"] == 1:
            return _result_with(pockets)   # pocket select
        if calls["n"] == 2:
            return route_counts            # route_type counts
        return skip_top                    # top skip reason

    db.execute = _execute

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)):
        resp = await client.get(f"{PREFIX}/metrics")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["zero_match_fresh_pockets"] == 1
    assert data["top_skip_reason"] == "no_tenant_filter"
    assert data["top_skip_count"] == 4
    # the single top pocket is flagged as not matched since refresh
    assert data["top_pockets"][0]["matched_since_refresh"] is False


@pytest.mark.asyncio
async def test_g4_sol_r1_b04_metrics_account_for_ineligible_pockets(client):
    """Eligibility is a first-class metric and all active states reconcile."""
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID)
    now = datetime.now(timezone.utc)
    pockets = [
        types.SimpleNamespace(
            id="fresh", physical_table_name="p_fresh", status="fresh",
            population_eligibility="eligible", hit_count=0, ttl_days=14,
            time_saved_ms_total=0, storage_bytes=0, retired_at=None,
            last_access_at=now, last_refresh_at=now, last_match_at=now,
        ),
        types.SimpleNamespace(
            id="parked", physical_table_name="p_parked", status="fresh",
            population_eligibility="ineligible", hit_count=0, ttl_days=14,
            time_saved_ms_total=0, storage_bytes=0, retired_at=None,
            last_access_at=now, last_refresh_at=now, last_match_at=None,
        ),
        types.SimpleNamespace(
            id="stale", physical_table_name="p_stale", status="stale",
            population_eligibility="unknown", hit_count=0, ttl_days=14,
            time_saved_ms_total=0, storage_bytes=0, retired_at=None,
            last_access_at=now, last_refresh_at=now, last_match_at=None,
        ),
    ]

    async def _get(cls, obj_id):
        if cls is Model and str(obj_id) == str(TEST_MODEL_ID):
            return scoped_model
        return None

    db.get = AsyncMock(side_effect=_get)
    calls = {"n": 0}

    async def _execute(_stmt):
        calls["n"] += 1
        if calls["n"] == 1:
            return _result_with(pockets)
        return types.SimpleNamespace(all=lambda: [("source", 1)])

    db.execute = _execute
    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)):
        resp = await client.get(f"{PREFIX}/metrics")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_pockets"] == 3
    assert data["fresh_pockets"] == 1
    assert data["stale_pockets"] == 1
    assert data["ineligible_pockets"] == 1
    assert data["fresh_pockets"] + data["stale_pockets"] + data["invalidating_pockets"] + data["failed_pockets"] + data["ineligible_pockets"] == data["total_pockets"]


@pytest.mark.asyncio
async def test_refresh_pocket_returns_202_queued(client):
    """F-005-23: POST /refresh queues the work and returns 202 with a queued
    run, instead of blocking the request on the full CTAS."""
    db = make_mock_db()
    pocket_id = uuid.uuid4()
    scoped_model = types.SimpleNamespace(id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID)
    pocket = types.SimpleNamespace(id=pocket_id, model_id=TEST_MODEL_ID)
    now = datetime.now(timezone.utc)

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        if cls is PocketDefinition:
            return pocket
        return None

    db.get = AsyncMock(side_effect=_get)

    def _fake_add(obj):
        obj.id = uuid.uuid4()
        obj.started_at = now
        obj.completed_at = None
        obj.rows_written = None
        obj.bytes_processed = None
        obj.error_message = None

    db.add = MagicMock(side_effect=_fake_add)
    db.refresh = AsyncMock(return_value=None)

    # Do not actually run the background refresh in the unit test.
    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), patch(
        "src.api.pockets._run_pocket_refresh_in_background", new_callable=AsyncMock
    ):
        resp = await client.post(f"{PREFIX}/{pocket_id}/refresh")

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert body["refresh_mode"] == "manual"
    assert body["triggered_by"] == "api"


# ---------------------------------------------------------------------------
# Refresh policy round-trip (GET -> 404, PUT create, GET, PUT update)
# ---------------------------------------------------------------------------


def _scalar_result(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


@pytest.mark.asyncio
async def test_get_refresh_policy_returns_404_when_not_configured(client):
    db = make_mock_db()
    pocket_id = uuid.uuid4()
    scoped_model = types.SimpleNamespace(id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID)
    pocket = types.SimpleNamespace(id=pocket_id, model_id=TEST_MODEL_ID)

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        if cls is PocketDefinition:
            return pocket
        return None

    db.get = AsyncMock(side_effect=_get)
    db.execute = AsyncMock(return_value=_scalar_result(None))

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)):
        resp = await client.get(f"{PREFIX}/{pocket_id}/refresh/policy")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Refresh policy not configured"


@pytest.mark.asyncio
async def test_upsert_refresh_policy_creates_new_policy_when_absent(client):
    db = make_mock_db()
    pocket_id = uuid.uuid4()
    scoped_model = types.SimpleNamespace(id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID)
    pocket = types.SimpleNamespace(id=pocket_id, model_id=TEST_MODEL_ID)
    now = datetime.now(timezone.utc)

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        if cls is PocketDefinition:
            return pocket
        return None

    db.get = AsyncMock(side_effect=_get)
    db.execute = AsyncMock(return_value=_scalar_result(None))

    def _fake_add(obj):
        obj.id = uuid.uuid4()
        obj.pocket_definition_id = pocket_id
        obj.created_at = now
        obj.updated_at = now

    db.add = MagicMock(side_effect=_fake_add)

    async def _refresh(obj):
        return None

    db.refresh = AsyncMock(side_effect=_refresh)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)):
        resp = await client.put(
            f"{PREFIX}/{pocket_id}/refresh/policy",
            json={"cron_expression": "0 2 * * *", "is_enabled": True},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cron_expression"] == "0 2 * * *"
    assert body["is_enabled"] is True


# ---------------------------------------------------------------------------
# Validate SQL — routes through query-router via _validate_via_router
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_pocket_sql_ok(client):
    """Valid pocket SQL passes router validation, structural checks, and probe."""
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID,
        project_id=TEST_PROJECT_ID,
        slug="modely",
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets._validate_via_router", AsyncMock(return_value=_router_response())), \
         patch("src.api.pockets._route_query", AsyncMock(return_value={"columns": ["c1"]})):
        resp = await client.post(
            f"{PREFIX}/validate",
            json={"defining_sql": "SELECT * FROM modely"},
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["stage"] == "probe"


@pytest.mark.asyncio
async def test_validate_pocket_sql_rejects_empty(client):
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID)

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            f"{PREFIX}/validate",
            json={"defining_sql": "   "},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["stage"] == "parse"
    assert "empty" in data["error"].lower()


@pytest.mark.asyncio
async def test_validate_pocket_sql_fails_on_garbage(client):
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID)

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets._validate_via_router", AsyncMock(side_effect=ValueError("SQL parse error"))):
        resp = await client.post(
            f"{PREFIX}/validate",
            json={"defining_sql": "SELECT FROM WHERE ) )"},
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["stage"] == "parse"


# ---------------------------------------------------------------------------
# Dry run — now routed via _route_query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_happy_path_returns_row_count(client):
    # Bug-5898: dry-run now reuses _validate_via_router + subset validation
    # (same as validate/create) before the count probe, so this must mock
    # _validate_via_router like every other pockets test does — otherwise
    # the unmocked call attempts a real HTTP request to the query-router.
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID, slug="modely",
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        return None

    db.get = AsyncMock(side_effect=_get)
    route_result = {"rows": [{"__c": 42}]}

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets._validate_via_router", AsyncMock(return_value=_router_response())), \
         patch("src.api.pockets._route_query", AsyncMock(return_value=route_result)), \
         patch("src.api.pockets.get_setting", AsyncMock(return_value=300)):
        resp = await client.post(
            f"{PREFIX}/dry-run",
            json={"defining_sql": "SELECT id FROM modely"},
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["row_count"] == 42


@pytest.mark.asyncio
async def test_dry_run_rejects_subset_violating_sql(client):
    """Bug-5898: dry-run must reject SQL that fails _check_pocket_structure
    (e.g. FROM a physical table instead of the model slug) instead of
    wrapping it in a COUNT(*) probe and reporting a misleading success."""
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID, slug="modely",
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets._validate_via_router", AsyncMock(
             return_value=_router_response(from_tables=["demo_data.sales_data"])
         )), \
         patch("src.api.pockets._route_query", AsyncMock(
             side_effect=AssertionError("count probe must not run")
         )):
        resp = await client.post(
            f"{PREFIX}/dry-run",
            json={"defining_sql": "SELECT * FROM demo_data.sales_data"},
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is False
    assert "row_count" not in data or data["row_count"] is None


@pytest.mark.asyncio
async def test_dry_run_rejects_invalid_target_id(client):
    """Bug-5898: same target_id existence check as validate/create."""
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID, slug="modely",
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        return None  # DataTarget lookup resolves to nothing

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets._route_query", AsyncMock(
             side_effect=AssertionError("count probe must not run")
         )):
        resp = await client.post(
            f"{PREFIX}/dry-run",
            json={
                "defining_sql": "SELECT * FROM modely",
                "target_id": str(uuid.uuid4()),
            },
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "invalid target_id" in data["error"].lower()


@pytest.mark.asyncio
async def test_validate_pocket_sql_rejects_physical_table_from(client):
    """A pocket whose FROM is a physical table (instead of the model
    slug) must be rejected via _check_pocket_structure."""
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID,
        project_id=TEST_PROJECT_ID,
        slug="modely",
        display_name="Model Y",
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets._validate_via_router", AsyncMock(
             return_value=_router_response(from_tables=["demo_data.sales_data"])
         )):
        resp = await client.post(
            f"{PREFIX}/validate",
            json={"defining_sql": "SELECT * FROM demo_data.sales_data"},
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["stage"] == "subset"
    codes = {v["code"] for v in (data.get("violations") or [])}
    assert "FROM_NOT_MODEL" in codes


@pytest.mark.asyncio
async def test_validate_pocket_sql_rejects_invalid_target_id(client):
    """Bug-5898: validate must load and check a supplied target_id the same
    way create does — an unresolvable/foreign target_id must not be
    reported as a validated pocket."""
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID, slug="modely",
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        return None  # DataTarget lookup resolves to nothing

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            f"{PREFIX}/validate",
            json={
                "defining_sql": "SELECT * FROM modely",
                "target_id": str(uuid.uuid4()),
            },
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "invalid target_id" in data["error"].lower()


@pytest.mark.asyncio
async def test_validate_pocket_sql_rejects_cross_connector_target(client):
    """Bug-5898: validate must run the same source/target connector
    compatibility check create does (via _pocket_combo_error), not just
    confirm the target row exists."""
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID, slug="modely",
    )
    target_id = uuid.uuid4()
    target = types.SimpleNamespace(
        id=target_id, model_id=TEST_MODEL_ID, project_connection_id="conn-pg",
    )
    pg_conn = types.SimpleNamespace(
        id="conn-pg", connection_type="postgresql", project_id=TEST_PROJECT_ID,
    )
    bq_source = types.SimpleNamespace(id="conn-bq", connection_type="bigquery")

    async def _get(cls, obj_id):
        from shared.db.models import DataTarget, ProjectConnection
        if cls is Model:
            return scoped_model
        if cls is DataTarget:
            return target
        if cls is ProjectConnection:
            return pg_conn
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets.resolve_source_connection", AsyncMock(return_value=bq_source)), \
         patch("src.api.pockets.is_same_database", return_value=False):
        resp = await client.post(
            f"{PREFIX}/validate",
            json={
                "defining_sql": "SELECT * FROM modely",
                "target_id": str(target_id),
            },
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "cross-connector" in data["error"].lower()


@pytest.mark.asyncio
async def test_validate_pocket_sql_accepts_select_from_model_slug(client):
    """Structural check passes when FROM references the model slug."""
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID,
        project_id=TEST_PROJECT_ID,
        slug="modely",
        display_name="Model Y",
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets._validate_via_router", AsyncMock(
             return_value=_router_response(
                 filters=[{"dimension_name": "base_amount", "operator": "gt", "value": 4950}],
             )
         )), \
         patch("src.api.pockets._route_query", AsyncMock(return_value={"columns": ["c1"]})):
        resp = await client.post(
            f"{PREFIX}/validate",
            json={"defining_sql": "SELECT * FROM modely WHERE base_amount > 4950"},
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["stage"] == "probe"


# ---------------------------------------------------------------------------
# Bug-8162 — an unreachable validator is not a verdict on the user's SQL.
#
# The sibling of ``scratchpad_measures._validate_expression_against_model``.
# ``_validate_via_router`` already failed CLOSED, but it collapsed "the router
# is down" into the same ValueError as "the router rejected your SQL", so an
# outage surfaced to a modeller as "Pocket SQL failed validation" — telling
# them their correct SQL was wrong. A network error escaped uncaught as a 500.
# ---------------------------------------------------------------------------


def _httpx_client_raising(exc: Exception):
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise exc

    return _Client


def _httpx_client_returning(status: int, payload: dict | None = None):
    class _Resp:
        status_code = status
        text = "stub"

        def json(self):
            return payload if payload is not None else {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp()

    return _Client


@pytest.mark.asyncio
async def test_bug_8162_router_5xx_is_unavailability_not_a_rejection(monkeypatch):
    """Bug-8162: a 5xx must raise RouterUnavailableError, not ValueError."""
    import httpx as _httpx

    from src.api import pockets as pk

    monkeypatch.setattr(pk.httpx, "AsyncClient", _httpx_client_returning(503))
    with pytest.raises(pk.RouterUnavailableError):
        await pk._validate_via_router(TEST_MODEL_ID, "SELECT * FROM modely", "tok")

    # A network error is the same class of "no answer".
    monkeypatch.setattr(
        pk.httpx, "AsyncClient", _httpx_client_raising(_httpx.ConnectError("down"))
    )
    with pytest.raises(pk.RouterUnavailableError):
        await pk._validate_via_router(TEST_MODEL_ID, "SELECT * FROM modely", "tok")

    # ...and a 4xx is still a verdict, still a ValueError. RouterUnavailableError
    # deliberately does not subclass ValueError, so the two never blur.
    monkeypatch.setattr(
        pk.httpx,
        "AsyncClient",
        _httpx_client_returning(400, {"detail": "unknown column"}),
    )
    with pytest.raises(ValueError) as exc:
        await pk._validate_via_router(TEST_MODEL_ID, "SELECT * FROM modely", "tok")
    assert not isinstance(exc.value, pk.RouterUnavailableError)
    assert "unknown column" in str(exc.value)


@pytest.mark.asyncio
async def test_bug_8162_create_pocket_503s_when_router_unavailable(client):
    """Bug-8162 END-TO-END: an outage answers 503 and persists nothing.

    The SQL below is valid. Before the fix the caller got a 400 saying their
    SQL failed validation — a false statement about correct SQL, and the exact
    "careless implementation" the decision warned against.
    """
    from src.api.pockets import RouterUnavailableError

    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID,
        project_id=TEST_PROJECT_ID,
        slug="modely",
        display_name="Model Y",
        seed="deadbeef",
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets._validate_via_router", AsyncMock(
             side_effect=RouterUnavailableError("router returned HTTP 503")
         )):
        resp = await client.post(
            PREFIX,
            json={
                "target_id": str(uuid.uuid4()),
                "defining_sql": "SELECT * FROM modely",
                "refresh_policy": "manual",
                "ttl_days": 14,
            },
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert "validator_unavailable" in detail
    assert "not been rejected" in detail
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_bug_8162_validate_endpoint_503s_instead_of_claiming_ok_false(client):
    """Bug-8162: ``ok=false, stage="parse"`` is a verdict; an outage has none."""
    from src.api.pockets import RouterUnavailableError

    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID, slug="modely",
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets._validate_via_router", AsyncMock(
             side_effect=RouterUnavailableError("ConnectError: router down")
         )):
        resp = await client.post(
            f"{PREFIX}/validate",
            json={"defining_sql": "SELECT * FROM modely"},
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 503, resp.text
    assert "validator_unavailable" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_bug_8162_real_rejection_still_reads_as_a_verdict(client):
    """Bug-8162 (other half): a genuine 4xx rejection must NOT say "retry".

    Fail-closed is only half the decision — the response must still tell a user
    with genuinely bad SQL that their SQL is bad. This pins the contrast, so an
    implementation that answers 503 to everything cannot pass.
    """
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID,
        project_id=TEST_PROJECT_ID,
        slug="modely",
        display_name="Model Y",
        seed="deadbeef",
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets._validate_via_router", AsyncMock(
             side_effect=ValueError("column no_such_col does not exist")
         )):
        resp = await client.post(
            PREFIX,
            json={
                "target_id": str(uuid.uuid4()),
                "defining_sql": "SELECT no_such_col FROM modely",
                "refresh_policy": "manual",
                "ttl_days": 14,
            },
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "no_such_col" in detail
    assert "validator_unavailable" not in detail
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_create_pocket_rejects_sql_outside_model(client):
    """POST .../pockets must reject a defining_sql that reads from a
    physical table instead of the model slug (HTTP 400)."""
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID,
        project_id=TEST_PROJECT_ID,
        slug="modely",
        display_name="Model Y",
        seed="deadbeef",
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets._validate_via_router", AsyncMock(
             return_value=_router_response(from_tables=["demo_data.sales_data"])
         )):
        resp = await client.post(
            PREFIX,
            json={
                "target_id": str(uuid.uuid4()),
                "defining_sql": "SELECT * FROM demo_data.sales_data",
                "refresh_policy": "manual",
                "ttl_days": 14,
            },
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    codes = {v["code"] for v in detail["violations"]}
    assert "FROM_NOT_MODEL" in codes


@pytest.mark.asyncio
async def test_validate_pocket_sql_rejects_unresolvable_where_fail_closed(client):
    """F-005-01: has_unresolvable_where must FAIL CLOSED at validate — the
    stored predicates would under-describe the cached rows, so the pocket is
    non-routable and must be refused, not merely warned about."""
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID,
        project_id=TEST_PROJECT_ID,
        slug="modely",
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets._validate_via_router", AsyncMock(
             return_value=_router_response(has_unresolvable_where=True)
         )), \
         patch("src.api.pockets._route_query", AsyncMock(return_value={"columns": ["c1"]})):
        resp = await client.post(
            f"{PREFIX}/validate",
            json={"defining_sql": 'SELECT * FROM modely WHERE channel_code = "WEB"'},
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is False
    assert data["stage"] == "subset"
    codes = {v["code"] for v in (data.get("violations") or [])}
    assert "UNRESOLVABLE_WHERE" in codes


@pytest.mark.asyncio
async def test_create_pocket_rejects_unresolvable_where_fail_closed(client):
    """F-005-01: POST .../pockets must reject (HTTP 400) a defining_sql whose
    WHERE the engine cannot fully extract — no under-describing predicate set
    is ever persisted."""
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID,
        project_id=TEST_PROJECT_ID,
        slug="modely",
        display_name="Model Y",
        seed="deadbeef",
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets._validate_via_router", AsyncMock(
             return_value=_router_response(has_unresolvable_where=True)
         )):
        resp = await client.post(
            PREFIX,
            json={
                "target_id": str(uuid.uuid4()),
                "defining_sql": 'SELECT * FROM modely WHERE channel_code = "WEB"',
                "refresh_policy": "manual",
                "ttl_days": 14,
            },
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    codes = {v["code"] for v in detail["violations"]}
    assert "UNRESOLVABLE_WHERE" in codes
    # The pocket row must never be flushed/committed.
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_patch_pocket_rejects_unresolvable_where_fail_closed(client):
    """F-005-01: PATCH with a new defining_sql whose WHERE cannot be fully
    extracted must fail closed (HTTP 422) rather than weaken the stored
    predicate set the matcher trusts."""
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID,
        project_id=TEST_PROJECT_ID,
        slug="modely",
        display_name="Model Y",
    )
    pocket_id = uuid.uuid4()
    existing_pocket = PocketDefinition(
        model_id=TEST_MODEL_ID,
        target_id=uuid.uuid4(),
        physical_table_name="pocket_x",
        defining_sql="SELECT * FROM modely WHERE country = 'GB'",
        query_fingerprint="fp",
        predicate_set_hash="h",
        refresh_policy="manual",
        ttl_days=14,
        status="failed",
    )
    existing_pocket.id = pocket_id

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        if cls is PocketDefinition:
            return existing_pocket
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets._validate_via_router", AsyncMock(
             return_value=_router_response(has_unresolvable_where=True)
         )):
        resp = await client.patch(
            f"{PREFIX}/{pocket_id}",
            json={"defining_sql": 'SELECT * FROM modely WHERE channel_code = "WEB"'},
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    codes = {v["code"] for v in detail["violations"]}
    assert "UNRESOLVABLE_WHERE" in codes
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_patch_pocket_rejects_unknown_refresh_policy(client):
    """Bug-6108 / Bug-6593: PATCH must reject an unknown refresh_policy, not
    write it. A token outside the policy universe ({schedule, manual, event})
    is refused at the schema boundary (Pydantic 422) — the same status create
    returns for the same malformed input, so the two endpoints stay consistent.
    The handler's per-tenant allow-list (400) is a distinct rule for a
    universe-valid token a tenant has disabled; it is not exercised here."""
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID, slug="modely",
        display_name="Model Y",
    )
    pocket_id = uuid.uuid4()
    existing = PocketDefinition(
        model_id=TEST_MODEL_ID, target_id=uuid.uuid4(),
        physical_table_name="pocket_x", defining_sql="SELECT * FROM modely",
        query_fingerprint="fp", predicate_set_hash="h",
        refresh_policy="manual", ttl_days=14, status="stale",
    )
    existing.id = pocket_id

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        if cls is PocketDefinition:
            return existing
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets.get_setting", AsyncMock(
             side_effect=lambda key, **kw: ["manual", "schedule"] if "allowed" in key else 14
         )):
        resp = await client.patch(
            f"{PREFIX}/{pocket_id}",
            json={"refresh_policy": "every_hour"},
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 422, resp.text
    assert "refresh_policy must be one of" in resp.text
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_patch_pocket_rejects_invalid_cron(client):
    """Bug-6108 / Bug-6593: PATCH must reject an unparseable refresh_cron instead
    of silently writing a cron the scheduler can never fire. The cron is
    validated at the schema boundary (Pydantic 422), matching create for the
    same malformed input; the handler cron check is defense-in-depth behind it."""
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID, slug="modely",
        display_name="Model Y",
    )
    pocket_id = uuid.uuid4()
    existing = PocketDefinition(
        model_id=TEST_MODEL_ID, target_id=uuid.uuid4(),
        physical_table_name="pocket_x", defining_sql="SELECT * FROM modely",
        query_fingerprint="fp", predicate_set_hash="h",
        refresh_policy="schedule", ttl_days=14, status="stale",
    )
    existing.id = pocket_id

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        if cls is PocketDefinition:
            return existing
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets.get_setting", AsyncMock(
             side_effect=lambda key, **kw: ["manual", "schedule"] if "allowed" in key else 14
         )):
        resp = await client.patch(
            f"{PREFIX}/{pocket_id}",
            json={"refresh_cron": "not a cron"},
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 422, resp.text
    assert "Invalid cron expression" in resp.text
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_patch_pocket_schedule_change_syncs_policy_row(client):
    """Bug-6108: a PATCH that sets a scheduled refresh must upsert the
    authoritative PocketRefreshPolicy child row (the scheduler reads it), not
    just the deprecated pocket.refresh_cron column."""
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID, slug="modely",
        display_name="Model Y",
    )
    pocket_id = uuid.uuid4()
    existing = PocketDefinition(
        model_id=TEST_MODEL_ID, target_id=uuid.uuid4(),
        physical_table_name="pocket_x", defining_sql="SELECT * FROM modely",
        query_fingerprint="fp", predicate_set_hash="h",
        refresh_policy="manual", ttl_days=14, status="stale",
    )
    existing.id = pocket_id

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        if cls is PocketDefinition:
            return existing
        return None

    db.get = AsyncMock(side_effect=_get)

    def _execute(*args, **kwargs):
        result = MagicMock()
        # No pre-existing policy row -> the sync inserts one.
        result.scalar_one_or_none.return_value = None
        # Final select echoes a valid response object.
        result.scalar_one.return_value = _pocket_response_stub([])
        return result

    db.execute = AsyncMock(side_effect=_execute)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets.get_setting", AsyncMock(
             side_effect=lambda key, **kw: ["manual", "schedule"] if "allowed" in key else 14
         )):
        resp = await client.patch(
            f"{PREFIX}/{pocket_id}",
            json={"refresh_policy": "schedule", "refresh_cron": "0 3 * * *"},
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 200, resp.text
    policies = [
        call.args[0]
        for call in db.add.call_args_list
        if call.args and isinstance(call.args[0], PocketRefreshPolicy)
    ]
    assert len(policies) == 1, "scheduled PATCH must upsert the refresh-policy row"
    assert policies[0].cron_expression == "0 3 * * *"
    assert policies[0].is_enabled is True


# ---------------------------------------------------------------------------
# Bug-1093 / Bug-1096 (F-005): predicate rows are derived authoritatively from
# the validated SQL at every defining_sql write. The matcher trusts these rows
# to decide routability, so they must describe the cached slice exactly — never
# a client-supplied claim that under-describes (over-claims coverage of) it.
# ---------------------------------------------------------------------------


def _captured_predicates(db) -> list:
    """Return every PocketPredicate instance passed to db.add (in order)."""
    from shared.db.models import PocketPredicate

    out = []
    for call in db.add.call_args_list:
        obj = call.args[0] if call.args else None
        if isinstance(obj, PocketPredicate):
            out.append(obj)
    return out


def _pocket_response_stub(predicate_rows):
    """A minimally-valid PocketDefinitionResponse source object that echoes the
    predicate rows the producer actually persisted, so the route's final select
    validates while the test asserts the real persisted predicate set."""
    now = datetime.now(timezone.utc)
    preds = [
        types.SimpleNamespace(
            id=uuid.uuid4(),
            pocket_definition_id=uuid.uuid4(),
            column_name=p.column_name,
            operator=p.operator,
            value_json=p.value_json,
            created_at=now,
        )
        for p in predicate_rows
    ]
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        target_id=uuid.uuid4(),
        target_schema="public",
        physical_table_name="pocket_x",
        row_count=0,
        defining_sql="SELECT * FROM modely WHERE x = 1",
        query_fingerprint="fp",
        predicate_set_hash="h",
        refresh_policy="manual",
        refresh_cron=None,
        incremental_column=None,
        incremental_lookback_hours=None,
        ttl_days=14,
        storage_bytes=0,
        status="stale",
        failure_reason=None,
        last_refresh_at=None,
        last_access_at=None,
        last_match_at=None,
        hit_count=0,
        time_saved_ms_total=0,
        created_at=now,
        updated_at=now,
        retired_at=None,
        predicates=preds,
        refresh_policy_row=None,
    )


@pytest.mark.asyncio
async def test_compound_edit_is_one_locked_atomic_route_write(client):
    """L13-F6: production compound route owns one lock and one commit.

    The response query is kept at the route boundary, so this proves the
    definition and policy are accepted through the shipped endpoint together,
    rather than only asserting a mocked hook call.
    """
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID)
    pocket = PocketDefinition(
        model_id=TEST_MODEL_ID,
        target_id=uuid.uuid4(),
        physical_table_name="pocket_x",
        defining_sql="SELECT 1",
        query_fingerprint="old",
        predicate_set_hash="old-hash",
        refresh_policy="schedule",
        ttl_days=14,
        status="stale",
    )
    pocket.id = uuid.uuid4()
    policy = PocketRefreshPolicy(
        pocket_definition_id=pocket.id,
        cron_expression="0 2 * * *",
        is_enabled=False,
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        if cls is PocketDefinition:
            return pocket
        return None

    db.get = AsyncMock(side_effect=_get)
    response_result = MagicMock()
    response_result.scalar_one.return_value = _pocket_response_stub([])
    policy_result = MagicMock()
    policy_result.scalar_one_or_none.return_value = policy
    db.execute = AsyncMock(side_effect=[MagicMock(), policy_result, response_result])
    lock = AsyncMock()
    router_response = _router_response(query_fingerprint="new-fingerprint")

    with (
        patch("src.api.pockets.get_tenant_db", async_gen_from(db)),
        patch("src.api.pockets.acquire_model_definition_lock", lock),
        patch("src.api.pockets._validate_via_router", AsyncMock(return_value=router_response)),
    ):
        resp = await client.post(
            f"{PREFIX}/{pocket.id}/compound-edit",
            json={
                "definition": {"defining_sql": "SELECT 2"},
                "policy": {"cron_expression": "0 3 * * *", "is_enabled": True},
            },
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 200, resp.text
    lock.assert_awaited_once_with(db, TEST_MODEL_ID)
    db.commit.assert_awaited_once()
    assert pocket.defining_sql == "SELECT 2"
    assert policy.cron_expression == "0 3 * * *"
    assert policy.is_enabled is True


@pytest.mark.asyncio
async def test_l13_r2_f7_compound_edit_enforces_canonical_invariants(client):
    """Compound history writes must share PATCH structure/policy/conflict gates."""

    async def run_case(*, router_response=None, policy_values=None, commit_error=None, body):
        db = make_mock_db()
        scoped_model = types.SimpleNamespace(
            id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID, slug="modely",
        )
        pocket = PocketDefinition(
            model_id=TEST_MODEL_ID, target_id=uuid.uuid4(),
            physical_table_name="pocket_x", defining_sql="SELECT * FROM modely",
            query_fingerprint="old", predicate_set_hash="old-hash",
            refresh_policy="manual", ttl_days=14, status="stale",
        )
        pocket.id = uuid.uuid4()
        policy = PocketRefreshPolicy(
            pocket_definition_id=pocket.id,
            cron_expression="0 2 * * *", is_enabled=False,
        )

        async def _get(cls, obj_id):
            if cls is Model:
                return scoped_model
            if cls is PocketDefinition:
                return pocket
            return None

        db.get = AsyncMock(side_effect=_get)
        policy_result = MagicMock()
        policy_result.scalar_one_or_none.return_value = policy
        db.execute = AsyncMock(side_effect=[MagicMock(), policy_result])
        db.commit = AsyncMock(side_effect=commit_error)
        db.rollback = AsyncMock()
        with (
            patch("src.api.pockets.get_tenant_db", async_gen_from(db)),
            patch("src.api.pockets.acquire_model_definition_lock", new=AsyncMock()),
            patch("src.api.pockets._validate_via_router", AsyncMock(return_value=router_response)),
            patch("src.api.pockets.get_setting", AsyncMock(return_value=policy_values or ["manual", "schedule", "event"])),
        ):
            response = await client.post(
                f"{PREFIX}/{pocket.id}/compound-edit",
                json=body,
                headers=AUTH_HEADERS,
            )
        return response, db

    invalid_structure, invalid_db = await run_case(
        router_response=_router_response(from_tables=["demo_data.sales_data"]),
        body={
            "definition": {"defining_sql": "SELECT * FROM demo_data.sales_data"},
            "policy": {"cron_expression": None, "is_enabled": False},
        },
    )
    assert invalid_structure.status_code == 422
    assert "FROM_NOT_MODEL" in invalid_structure.text
    invalid_db.commit.assert_not_awaited()
    assert not any(
        call.args and isinstance(call.args[0], PocketPredicate)
        for call in invalid_db.add.call_args_list
    )

    tenant_policy, tenant_db = await run_case(
        policy_values=["manual", "schedule"],
        body={
            "definition": {"refresh_policy": "event"},
            "policy": {"cron_expression": None, "is_enabled": False},
        },
    )
    assert tenant_policy.status_code == 400
    assert "refresh_policy must be one of" in tenant_policy.text
    tenant_db.commit.assert_not_awaited()

    duplicate, duplicate_db = await run_case(
        router_response=_router_response(query_fingerprint="duplicate-shape"),
        commit_error=IntegrityError("duplicate", {}, Exception("duplicate")),
        body={
            "definition": {"defining_sql": "SELECT * FROM modely"},
            "policy": {"cron_expression": None, "is_enabled": False},
        },
    )
    assert duplicate.status_code == 409
    assert "same query shape" in duplicate.text
    duplicate_db.commit.assert_awaited_once()
    duplicate_db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_pocket_rejects_cross_connector_target(client):
    """Bug-5475: a pocket whose source connector differs from its target
    connector (e.g. BigQuery source -> PostgreSQL target) is rejected at
    creation (HTTP 400) with a clear message — never persisted to fail or hang
    on refresh."""
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID, slug="modely",
        display_name="Model Y", seed="deadbeef",
    )
    target_id = uuid.uuid4()
    target = types.SimpleNamespace(
        id=target_id, model_id=TEST_MODEL_ID, project_connection_id="conn-pg",
    )
    pg_conn = types.SimpleNamespace(
        id="conn-pg", connection_type="postgresql", project_id=TEST_PROJECT_ID,
    )
    bq_source = types.SimpleNamespace(id="conn-bq", connection_type="bigquery")

    async def _get(cls, obj_id):
        from shared.db.models import DataTarget, ProjectConnection
        if cls is Model:
            return scoped_model
        if cls is DataTarget:
            return target
        if cls is ProjectConnection:
            return pg_conn
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets._validate_via_router", AsyncMock(return_value=_router_response())), \
         patch("src.api.pockets.resolve_source_connection", AsyncMock(return_value=bq_source)), \
         patch("src.api.pockets.is_same_database", return_value=False), \
         patch("src.api.pockets.get_setting", AsyncMock(side_effect=lambda key, **kw: ["manual"] if "allowed" in key else 14)):
        resp = await client.post(
            PREFIX,
            json={
                "target_id": str(target_id),
                "defining_sql": "SELECT * FROM modely",
                "refresh_policy": "manual",
                "ttl_days": 14,
            },
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 400, resp.text
    assert "Cross-connector pocket materialisation is not supported" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_pocket_allows_bigquery_same_connector_target(client):
    """Bug-5475: a BigQuery source + BigQuery target (same connection) is now a
    supported pocket combination and must persist (HTTP 201)."""
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID, slug="modely",
        display_name="Model Y", seed="deadbeef",
    )
    target_id = uuid.uuid4()
    target = types.SimpleNamespace(
        id=target_id, model_id=TEST_MODEL_ID, project_connection_id="conn-bq",
    )
    bq_conn = types.SimpleNamespace(
        id="conn-bq", connection_type="bigquery", project_id=TEST_PROJECT_ID,
    )

    async def _get(cls, obj_id):
        from shared.db.models import DataTarget, ProjectConnection
        if cls is Model:
            return scoped_model
        if cls is DataTarget:
            return target
        if cls is ProjectConnection:
            return bq_conn
        return None

    db.get = AsyncMock(side_effect=_get)

    def _execute(*args, **kwargs):
        result = MagicMock()
        result.scalar_one.return_value = _pocket_response_stub([])
        return result

    db.execute = AsyncMock(side_effect=_execute)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets._validate_via_router", AsyncMock(return_value=_router_response())), \
         patch("src.api.pockets.resolve_source_connection", AsyncMock(return_value=bq_conn)), \
         patch("src.api.pockets.is_same_database", return_value=True), \
         patch("src.api.pockets.get_setting", AsyncMock(side_effect=lambda key, **kw: ["manual"] if "allowed" in key else 14)):
        resp = await client.post(
            PREFIX,
            json={
                "target_id": str(target_id),
                "defining_sql": "SELECT * FROM modely",
                "refresh_policy": "manual",
                "ttl_days": 14,
            },
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 201, resp.text
    db.commit.assert_called()


@pytest.mark.asyncio
async def test_create_scheduled_pocket_creates_refresh_policy_row(client):
    """Bug-5575: refresh_policy='schedule' must create the enabled
    PocketRefreshPolicy row the scheduler sweep joins on."""
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID,
        project_id=TEST_PROJECT_ID,
        slug="modely",
        display_name="Model Y",
        seed="deadbeef",
    )
    target_id = uuid.uuid4()
    target = types.SimpleNamespace(id=target_id, model_id=TEST_MODEL_ID)

    async def _get(cls, obj_id):
        from shared.db.models import DataTarget

        if cls is Model:
            return scoped_model
        if cls is DataTarget and str(obj_id) == str(target_id):
            return target
        return None

    db.get = AsyncMock(side_effect=_get)

    def _execute(*args, **kwargs):
        result = MagicMock()
        result.scalar_one.return_value = _pocket_response_stub(_captured_predicates(db))
        return result

    db.execute = AsyncMock(side_effect=_execute)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets._validate_via_router", AsyncMock(return_value=_router_response())), \
         patch("src.api.pockets.get_setting", AsyncMock(side_effect=lambda key, **kw: ["manual", "schedule"] if "allowed" in key else 14)):
        resp = await client.post(
            PREFIX,
            json={
                "target_id": str(target_id),
                "defining_sql": "SELECT * FROM modely",
                "refresh_policy": "schedule",
                "refresh_cron": "0 2 * * *",
                "ttl_days": 14,
            },
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 201, resp.text
    policies = [
        call.args[0]
        for call in db.add.call_args_list
        if call.args and isinstance(call.args[0], PocketRefreshPolicy)
    ]
    assert len(policies) == 1
    assert policies[0].cron_expression == "0 2 * * *"
    assert policies[0].is_enabled is True


@pytest.mark.asyncio
async def test_create_pocket_ignores_weaker_client_predicates(client):
    """Bug-1096 (F-005): a create whose client `predicates` payload is WEAKER
    than the actual SQL filters must persist the SQL-derived predicates, never
    the client claim. The router extracts two filters (country_code='AE' AND
    customer_segment='RETAIL'); the client sends only [country_code=AE]. The
    persisted predicate rows must reflect BOTH SQL filters."""
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID,
        project_id=TEST_PROJECT_ID,
        slug="modely",
        display_name="Model Y",
        seed="deadbeef",
    )
    target_id = uuid.uuid4()
    target = types.SimpleNamespace(id=target_id, model_id=TEST_MODEL_ID)

    async def _get(cls, obj_id):
        from shared.db.models import DataTarget

        if cls is Model:
            return scoped_model
        if cls is DataTarget and str(obj_id) == str(target_id):
            return target
        return None

    db.get = AsyncMock(side_effect=_get)

    # Final select after commit echoes the persisted predicate rows.
    def _execute(*args, **kwargs):
        result = MagicMock()
        result.scalar_one.return_value = _pocket_response_stub(_captured_predicates(db))
        return result

    db.execute = AsyncMock(side_effect=_execute)

    router_resp = _router_response(
        from_tables=["modely"],
        query_fingerprint="fp-ae-retail",
        filters=[
            {"dimension_name": "country_code", "operator": "eq", "value": "AE"},
            {"dimension_name": "customer_segment", "operator": "eq", "value": "RETAIL"},
        ],
    )

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets._validate_via_router", AsyncMock(return_value=router_resp)), \
         patch("src.api.pockets.get_setting", AsyncMock(side_effect=lambda key, **kw: ["manual"] if "allowed" in key else 14)):
        resp = await client.post(
            PREFIX,
            json={
                "target_id": str(target_id),
                "defining_sql": "SELECT * FROM modely WHERE country_code = 'AE' AND customer_segment = 'RETAIL'",
                "refresh_policy": "manual",
                "ttl_days": 14,
                # Deliberately WEAKER than the SQL: omits customer_segment.
                "predicates": [{"column_name": "country_code", "operator": "eq", "value": "AE"}],
            },
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 201, resp.text
    db.commit.assert_called()
    persisted = _captured_predicates(db)
    cols = {p.column_name for p in persisted}
    # The SQL had TWO filters; the client claimed ONE. Persisted set must be
    # the SQL set — never the weaker client claim.
    assert cols == {"country_code", "customer_segment"}, cols
    assert len(persisted) == 2
    seg = next(p for p in persisted if p.column_name == "customer_segment")
    assert seg.value_json == {"value": "RETAIL"}


@pytest.mark.asyncio
async def test_patch_pocket_narrowing_sql_rebuilds_predicate_rows(client):
    """Bug-1093 (F-005): a PATCH that narrows defining_sql must delete the old
    predicate rows and reinsert rows derived from the NEW validated SQL. The
    existing pocket holds [country_code=AE]; the PATCH narrows it to
    country_code='AE' AND customer_segment='RETAIL'. The producer must (a) issue
    a DELETE of the old predicate rows and (b) add a PocketPredicate for BOTH
    new filters."""
    from sqlalchemy.sql.dml import Delete

    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID,
        project_id=TEST_PROJECT_ID,
        slug="modely",
        display_name="Model Y",
    )
    pocket_id = uuid.uuid4()
    existing_pocket = PocketDefinition(
        model_id=TEST_MODEL_ID,
        target_id=uuid.uuid4(),
        physical_table_name="pocket_x",
        defining_sql="SELECT * FROM modely WHERE country_code = 'AE'",
        query_fingerprint="fp-old",
        predicate_set_hash="h-old",
        refresh_policy="manual",
        ttl_days=14,
        status="fresh",
    )
    existing_pocket.id = pocket_id

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        if cls is PocketDefinition:
            return existing_pocket
        return None

    db.get = AsyncMock(side_effect=_get)

    delete_statements = []

    def _execute(*args, **kwargs):
        stmt = args[0] if args else None
        if isinstance(stmt, Delete):
            delete_statements.append(stmt)
        result = MagicMock()
        result.scalar_one.return_value = _pocket_response_stub(_captured_predicates(db))
        return result

    db.execute = AsyncMock(side_effect=_execute)

    router_resp = _router_response(
        from_tables=["modely"],
        query_fingerprint="fp-ae-retail",
        filters=[
            {"dimension_name": "country_code", "operator": "eq", "value": "AE"},
            {"dimension_name": "customer_segment", "operator": "eq", "value": "RETAIL"},
        ],
    )

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets._validate_via_router", AsyncMock(return_value=router_resp)):
        resp = await client.patch(
            f"{PREFIX}/{pocket_id}",
            json={"defining_sql": "SELECT * FROM modely WHERE country_code = 'AE' AND customer_segment = 'RETAIL'"},
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 200, resp.text
    db.commit.assert_called()
    # Old predicate rows must be deleted.
    assert len(delete_statements) == 1, "narrowing PATCH must DELETE old predicate rows"
    # New predicate rows must reflect the NEW SQL's two filters.
    persisted = _captured_predicates(db)
    cols = {p.column_name for p in persisted}
    assert cols == {"country_code", "customer_segment"}, cols
    assert len(persisted) == 2
    assert existing_pocket.status == "stale"


@pytest.mark.asyncio
async def test_patch_pocket_identity_collision_returns_409(client):
    """F-005-02 (residual): a PATCH whose new defining_sql recomputes an identity
    that collides with another pocket on this model's partial unique index must
    surface a 409 (like create), not a raw 500."""
    from sqlalchemy.exc import IntegrityError

    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID,
        project_id=TEST_PROJECT_ID,
        slug="modely",
        display_name="Model Y",
    )
    pocket_id = uuid.uuid4()
    existing_pocket = PocketDefinition(
        model_id=TEST_MODEL_ID,
        target_id=uuid.uuid4(),
        physical_table_name="pocket_x",
        defining_sql="SELECT * FROM modely WHERE country_code = 'AE'",
        query_fingerprint="fp-old",
        predicate_set_hash="h-old",
        refresh_policy="manual",
        ttl_days=14,
        status="fresh",
    )
    existing_pocket.id = pocket_id

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        if cls is PocketDefinition:
            return existing_pocket
        return None

    db.get = AsyncMock(side_effect=_get)
    db.commit = AsyncMock(
        side_effect=IntegrityError("stmt", {}, Exception("uq collision"))
    )
    db.rollback = AsyncMock()

    router_resp = _router_response(
        query_fingerprint="fp-collide",
        filters=[{"dimension_name": "country_code", "operator": "eq", "value": "SA"}],
    )

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets._validate_via_router", AsyncMock(return_value=router_resp)):
        resp = await client.patch(
            f"{PREFIX}/{pocket_id}",
            json={"defining_sql": "SELECT * FROM modely WHERE country_code = 'SA'"},
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 409, resp.text
    db.rollback.assert_awaited()


@pytest.mark.asyncio
async def test_dry_run_requires_authentication(client):
    # Bug-5898: dry-run now validates via _validate_via_router before the
    # count probe (_route_query) — mock that to succeed so the auth
    # failure this test targets is exercised on the probe itself, same as
    # the other pockets tests mock _validate_via_router.
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID, slug="modely",
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets._validate_via_router", AsyncMock(return_value=_router_response())), \
         patch("src.api.pockets._route_query", AsyncMock(
             side_effect=Exception("401 Unauthorized: authentication required")
         )), \
         patch("src.api.pockets.get_setting", AsyncMock(return_value=300)):
        resp = await client.post(
            f"{PREFIX}/dry-run",
            json={"defining_sql": "SELECT 1 FROM modely"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "authentication" in data["error"].lower()


@pytest.mark.asyncio
async def test_create_scheduled_pocket_honours_policy_enabled_flag(client):
    """Bug-7007: the COMPLETE initial refresh policy is created atomically in the
    single create request. A create carrying refresh_policy_enabled=False must
    write a DISABLED PocketRefreshPolicy row — proving no second setPolicy
    request is needed to finish configuring the pocket."""
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(
        id=TEST_MODEL_ID,
        project_id=TEST_PROJECT_ID,
        slug="modely",
        display_name="Model Y",
        seed="deadbeef",
    )
    target_id = uuid.uuid4()
    target = types.SimpleNamespace(id=target_id, model_id=TEST_MODEL_ID)

    async def _get(cls, obj_id):
        from shared.db.models import DataTarget

        if cls is Model:
            return scoped_model
        if cls is DataTarget and str(obj_id) == str(target_id):
            return target
        return None

    db.get = AsyncMock(side_effect=_get)

    def _execute(*args, **kwargs):
        result = MagicMock()
        result.scalar_one.return_value = _pocket_response_stub(_captured_predicates(db))
        return result

    db.execute = AsyncMock(side_effect=_execute)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets._validate_via_router", AsyncMock(return_value=_router_response())), \
         patch("src.api.pockets.get_setting", AsyncMock(side_effect=lambda key, **kw: ["manual", "schedule"] if "allowed" in key else 14)):
        resp = await client.post(
            PREFIX,
            json={
                "target_id": str(target_id),
                "defining_sql": "SELECT * FROM modely",
                "refresh_policy": "schedule",
                "refresh_cron": "0 2 * * *",
                "refresh_policy_enabled": False,
                "ttl_days": 14,
            },
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 201, resp.text
    policies = [
        call.args[0]
        for call in db.add.call_args_list
        if call.args and isinstance(call.args[0], PocketRefreshPolicy)
    ]
    # Exactly one policy row, created in the same transaction, honouring the
    # caller's disabled state — no separate PUT /refresh/policy required.
    assert len(policies) == 1
    assert policies[0].cron_expression == "0 2 * * *"
    assert policies[0].is_enabled is False


# ---------------------------------------------------------------------------
# Bug-8581 — deleting a pocket must not leave the query-router serving it.
# ---------------------------------------------------------------------------


def _pocket_row():
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        physical_table_name="pocket_abc",
        target_schema="public",
        target_id=uuid.uuid4(),
        status="fresh",
        retired_at=None,
    )


class _RollbackExpiredPocket:
    """Raise like an expired async ORM attribute after rollback."""

    def __init__(self):
        self._id = uuid.uuid4()
        self.model_id = TEST_MODEL_ID
        self.physical_table_name = "pocket_abc"
        self.target_schema = "public"
        self.target_id = uuid.uuid4()
        self.status = "fresh"
        self.retired_at = None
        self.expired = False

    @property
    def id(self):
        if self.expired:
            raise InvalidRequestError(
                "greenlet_spawn has not been called; expired pocket.id"
            )
        return self._id


@asynccontextmanager
async def _noop_pocket_delete_lock(*_args, **_kwargs):
    """Keep API unit tests independent of PostgreSQL advisory-lock plumbing."""
    yield


@pytest.mark.asyncio
async def test_bug_8581_delete_pocket_evicts_the_query_router_cache(client):
    """Observed live (LIVE-POCKET-RLS-001): after a pocket DELETE returned 204
    and its physical table was verified gone, the same query kept returning
    route_type=pocket with the deleted pocket's id and a routed SQL naming the
    dropped table, for 15+ seconds — replayed from the router's result cache.

    Mechanism 2 of the cache-invalidation contract (mechanism 1, the serve-time
    servability check, lives in the query-router): clear the receiving replica
    immediately, so the operator who just deleted a pocket does not keep being
    told it is serving. Deleting the wired call fails this test."""
    from src.api import pockets as _pockets

    pocket = _pocket_row()
    model = types.SimpleNamespace(id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID)
    db = make_mock_db()
    db.get = AsyncMock(return_value=pocket)

    evict = AsyncMock()
    with (
        patch("src.api.pockets.get_tenant_db", async_gen_from(db)),
        patch.object(_pockets, "_get_scoped_model", new=AsyncMock(return_value=model)),
        patch.object(_pockets, "drop_pocket_storage", new=AsyncMock()),
        patch.object(_pockets, "pocket_refresh_lock", new=_noop_pocket_delete_lock),
        patch.object(_pockets, "_evict_query_router_cache", evict),
    ):
        resp = await client.delete(f"{PREFIX}/{pocket.id}", headers=AUTH_HEADERS)

    assert resp.status_code == 204, resp.text
    evict.assert_awaited_once_with(TEST_MODEL_ID, TEST_TENANT)


@pytest.mark.asyncio
async def test_bug_8581_delete_still_succeeds_when_the_router_is_unreachable(client):
    """Best-effort by contract, exercised through the REAL helper: the delete has
    already committed, so an unreachable query-router must not turn a completed
    delete into an error. Uses the real ``_evict_query_router_cache`` with a
    broken transport rather than a raising mock — a mock that raises would be
    asserting a shape the helper cannot produce. The serve-time servability check
    in the query-router is what makes correctness independent of this call
    landing at all."""
    import httpx as _httpx
    from src.api import pockets as _pockets

    pocket = _pocket_row()
    model = types.SimpleNamespace(id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID)
    db = make_mock_db()
    db.get = AsyncMock(return_value=pocket)

    class _BrokenClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def delete(self, *a, **k):
            raise _httpx.ConnectError("query-router unreachable")

    with (
        patch("src.api.pockets.get_tenant_db", async_gen_from(db)),
        patch.object(_pockets, "_get_scoped_model", new=AsyncMock(return_value=model)),
        patch.object(_pockets, "drop_pocket_storage", new=AsyncMock()),
        patch.object(_pockets, "pocket_refresh_lock", new=_noop_pocket_delete_lock),
        patch("httpx.AsyncClient", _BrokenClient),
    ):
        resp = await client.delete(f"{PREFIX}/{pocket.id}", headers=AUTH_HEADERS)

    assert resp.status_code == 204, resp.text
    assert db.delete.await_count == 1


@pytest.mark.asyncio
async def test_bug9430_delete_returns_retryable_conflict_when_refresh_is_in_flight(client):
    """Delete must not race a live pocket CTAS/streaming refresh."""
    from shared.pocket_refresh_lock import PocketRefreshInFlightError
    from src.api import pockets as _pockets

    pocket = _pocket_row()
    model = types.SimpleNamespace(id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID)
    db = make_mock_db()
    db.get = AsyncMock(return_value=pocket)

    @asynccontextmanager
    async def _conflict(*_args, **_kwargs):
        raise PocketRefreshInFlightError(pocket.id)
        yield  # pragma: no cover

    with (
        patch("src.api.pockets.get_tenant_db", async_gen_from(db)),
        patch.object(_pockets, "_get_scoped_model", new=AsyncMock(return_value=model)),
        patch.object(_pockets, "pocket_refresh_lock", new=_conflict),
        patch.object(_pockets, "drop_pocket_storage", new=AsyncMock()),
    ):
        resp = await client.delete(f"{PREFIX}/{pocket.id}", headers=AUTH_HEADERS)

    assert resp.status_code == 409, resp.text
    assert "in flight" in resp.json()["detail"]
    db.delete.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_bug8579_delete_surfaces_storage_failure_and_retains_metadata(client):
    """A failed physical drop is evidence of incomplete cleanup, not 204."""
    from src.api import pockets as _pockets

    pocket = _pocket_row()
    model = types.SimpleNamespace(id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID)
    db = make_mock_db()
    db.get = AsyncMock(return_value=pocket)

    with (
        patch("src.api.pockets.get_tenant_db", async_gen_from(db)),
        patch.object(_pockets, "_get_scoped_model", new=AsyncMock(return_value=model)),
        patch.object(_pockets, "pocket_refresh_lock", new=_noop_pocket_delete_lock),
        patch.object(
            _pockets,
            "drop_pocket_storage",
            new=AsyncMock(side_effect=RuntimeError("target unavailable")),
        ),
    ):
        resp = await client.delete(f"{PREFIX}/{pocket.id}", headers=AUTH_HEADERS)

    assert resp.status_code == 503, resp.text
    assert "not deleted" in resp.json()["detail"]
    db.delete.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_bug8579_delete_does_not_read_expired_pocket_after_rollback(client):
    """Rollback expiration cannot turn the retained-metadata 503 into a 500."""
    from src.api import pockets as _pockets

    pocket = _RollbackExpiredPocket()
    model = types.SimpleNamespace(id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID)
    db = make_mock_db()
    db.get = AsyncMock(return_value=pocket)
    original_rollback = db.rollback

    async def _rollback_and_expire():
        pocket.expired = True
        await original_rollback()

    db.rollback = AsyncMock(side_effect=_rollback_and_expire)

    with (
        patch("src.api.pockets.get_tenant_db", async_gen_from(db)),
        patch.object(_pockets, "_get_scoped_model", new=AsyncMock(return_value=model)),
        patch.object(_pockets, "pocket_refresh_lock", new=_noop_pocket_delete_lock),
        patch.object(
            _pockets,
            "drop_pocket_storage",
            new=AsyncMock(side_effect=RuntimeError("target unavailable")),
        ),
    ):
        resp = await client.delete(
            f"{PREFIX}/{pocket._id}", headers=AUTH_HEADERS,
        )

    assert resp.status_code == 503, resp.text
    assert "not deleted" in resp.json()["detail"]
    db.delete.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()
