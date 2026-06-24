from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.db.models import Model, PocketDefinition, PocketRefreshPolicy

from .conftest import TEST_MODEL_ID, TEST_PROJECT_ID, async_gen_from, client, make_mock_db

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
    assert data["total_pockets"] == 2
    assert data["fresh_pockets"] == 1
    assert "top_pockets" in data
    # F-005-08: hit ratio is pocket queries / all queries (3/10 = 0.3), a
    # fraction in [0,1] — NOT total hits / pocket count (which was 9/2 = 4.5).
    assert data["pocket_hit_rate"] == pytest.approx(0.3)
    # time_saved is now the genuine saved total (sum of per-pocket
    # time_saved_ms_total), which the route-time accumulator fills with
    # baseline-minus-pocket figures.
    assert data["pocket_time_saved_ms"] == 1250


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
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID)

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        return None

    db.get = AsyncMock(side_effect=_get)
    route_result = {"rows": [{"__c": 42}]}

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets._route_query", AsyncMock(return_value=route_result)), \
         patch("src.api.pockets.get_setting", AsyncMock(return_value=300)):
        resp = await client.post(
            f"{PREFIX}/dry-run",
            json={"defining_sql": "SELECT id FROM public.sales"},
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["row_count"] == 42


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
    db = make_mock_db()
    scoped_model = types.SimpleNamespace(id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID)

    async def _get(cls, obj_id):
        if cls is Model:
            return scoped_model
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.pockets._route_query", AsyncMock(
             side_effect=Exception("401 Unauthorized: authentication required")
         )), \
         patch("src.api.pockets.get_setting", AsyncMock(return_value=300)):
        resp = await client.post(
            f"{PREFIX}/dry-run",
            json={"defining_sql": "SELECT 1"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "authentication" in data["error"].lower()
