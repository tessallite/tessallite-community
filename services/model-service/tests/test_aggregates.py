"""
Unit tests for aggregate CRUD routes.

Key behaviours tested:
  1. Create aggregate — success, 201 returned.
  2. Cap enforcement — when at max_aggregates, lowest-hit-rate active aggregate
     is retired before the new one is inserted.
  3. List / get / delete aggregates.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from .result_fakes import FakeScalarResult

from .conftest import (
    TEST_AGG_ID,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    NOW,
    client,
    make_mock_db,
    make_aggregate,
    make_model,
    async_gen_from,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/aggregates"
TARGET_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _ScalarResult:
    """Stand-in for a SQLAlchemy ``Result``.

    Supports ``.scalars().all()``, ``.scalar_one()`` and
    ``.scalar_one_or_none()`` so the same instance can serve both the
    list-style queries (aggregate listing, dimension/measure loading)
    and the scalar count queries that feed cap enforcement.
    """

    def __init__(self, items):
        self._items = items

    def scalars(self):
        return FakeScalarResult(self._items)

    def all(self):
        return self._items

    def scalar_one(self):
        return self._items[0] if self._items else 0

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None

    def one_or_none(self):
        # ``_scope._lookup_scoped`` calls ``.scalars().one_or_none()``.
        return self._items[0] if self._items else None


_EMPTY_RESULT = _ScalarResult([])


def _stub_layout():
    """Minimal ``ResolvedAggregateLayout`` for tests that patch the
    grain resolver out of the create path. Both lists empty so the
    handler's AggregateColumn insertion loop also no-ops."""
    from shared.semantic.grain_resolver import ResolvedAggregateLayout

    return ResolvedAggregateLayout(grain_cols=[], measure_cols=[])


def _make_execute_script(*results):
    """Return an ``AsyncMock`` whose ``side_effect`` yields each result
    in order and falls back to an empty result once exhausted. Lets
    individual tests declare only the queries whose output they care
    about without worrying that route-handler helpers (resolver,
    redundant-partner scan, ``_get_measure_names``) issue additional
    queries after the tested path."""
    iterator = iter(list(results))

    async def _side(*_a, **_kw):
        try:
            return next(iterator)
        except StopIteration:
            return _EMPTY_RESULT

    return AsyncMock(side_effect=_side)


def _owned_target_result():
    """The row the Bug-8026 ``target_id`` ownership guard resolves.

    ``create_aggregate`` now proves the body's ``target_id`` names a DataTarget
    inside the path project+model (``_scope.ensure_ref_in_model``) before it
    does anything else, so that SELECT is the FIRST statement every create test
    issues. Returning a row here is fixture setup, not a relaxed assertion: the
    denial behaviour is asserted by its own tests below and against real
    Postgres in ``tests/integration/test_body_fk_route_adoption_db.py``.
    """
    return _ScalarResult([
        types.SimpleNamespace(id=TARGET_ID, model_id=TEST_MODEL_ID)
    ])


def _make_create_script(
    *cap_results,
    dims=None,
    measures=None,
    tables=None,
    columns=None,
    udas=None,
    joins=None,
):
    """Statement-aware create-path fixture.

    Bug-8939 moved cap selection after all request validation.  Keying fixture
    rows to the queried domain instead of positional call order keeps the tests
    faithful to that contract and ensures a future unsafe reorder cannot be
    hidden by reshuffling mock results.
    """
    cap_iter = iter(cap_results)
    by_table = {
        "dimensions": _ScalarResult(dims or []),
        "measures": _ScalarResult(measures or []),
        "model_tables": _ScalarResult(tables or []),
        "model_columns": _ScalarResult(columns or []),
        "user_defined_attributes": _ScalarResult(udas or []),
        "joins": _ScalarResult(joins or []),
    }

    async def _side(stmt, *_args, **_kwargs):
        sql = str(stmt)
        if "data_targets" in sql:
            return _owned_target_result()
        for table, result in by_table.items():
            if f"FROM {table}" in sql:
                return result
        if "aggregate_definitions" in sql:
            try:
                return next(cap_iter)
            except StopIteration:
                return _EMPTY_RESULT
        return _EMPTY_RESULT

    return AsyncMock(side_effect=_side)


def _agg_body():
    return {
        "target_id": str(TARGET_ID),
        "physical_table_name": "agg_test_001",
        "grain": ["country"],
        "creation_reason": "manual",
        "include_quantiles": False,
    }


def test_aggregate_response_includes_predictive_validation_timestamp():
    """F-010-18: the API response must carry ``predictive_validated_at`` so the
    Model Health card can show a "Validated" badge on predictive aggregates that
    the feedback sweep has confirmed against real query traffic."""
    from shared.schemas.domains.aggregates_security import AggregateDefinitionResponse

    validated_at = NOW
    agg = make_aggregate(creation_reason="predictive")
    agg.predictive_validated_at = validated_at

    resp = AggregateDefinitionResponse.model_validate(agg)

    assert resp.creation_reason == "predictive"
    assert resp.predictive_validated_at == validated_at


# ---------------------------------------------------------------------------
# Create aggregate — happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_aggregate_success(client):
    model = make_model(max_aggregates=50)
    agg = make_aggregate()
    mock_db = make_mock_db()

    # Validation collections are empty; cap count is 0 (below the limit).
    mock_db.get = AsyncMock(return_value=model)
    # count query returns 0 (below cap); subsequent queries issued by the
    # resolver + redundant-partner + _get_measure_names helpers return
    # empty results so the handler completes its happy path.
    mock_db.execute = _make_create_script(_ScalarResult([0]))

    async def _refresh(obj):
        obj.id = agg.id
        obj.model_id = TEST_MODEL_ID
        obj.source_row_count = None
        obj.agg_row_count = None
        obj.estimated_hit_rate = None
        obj.created_at = NOW
        obj.updated_at = NOW
        obj.last_refreshed_at = None
        obj.retired_at = None
        obj.status = "active"
        obj.target_schema = "public"

    mock_db.refresh = _refresh

    with (
        patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "src.api.aggregates.resolve_aggregate_layout",
            return_value=_stub_layout(),
        ),
        patch(
            "src.api.aggregates.compute_redundant_partners",
            return_value={},
        ),
    ):
        resp = await client.post(PREFIX, json=_agg_body())

    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "active"
    assert data["grain"] == ["country"]


@pytest.mark.asyncio
async def test_create_aggregate_quantiles_registers_only_routable_median(client):
    """Bug-5891 (DEC-PERCENTILE): creating an aggregate with include_quantiles
    must register ONLY the routable p50 coverage row for each numeric measure —
    never the non-median percentiles (p90/p95/p99/...), which SQL routing cannot
    reach and would materialise into dead columns."""
    from shared.db.models import AggregateColumn
    from shared.semantic.grain_resolver import (
        ResolvedAggregateLayout,
        ResolvedMeasureCol,
    )

    model = make_model(max_aggregates=50)
    agg = make_aggregate()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)
    mock_db.execute = _make_create_script(_ScalarResult([0]))

    async def _refresh(obj):
        obj.id = agg.id
        obj.model_id = TEST_MODEL_ID
        obj.source_row_count = None
        obj.agg_row_count = None
        obj.estimated_hit_rate = None
        obj.created_at = NOW
        obj.updated_at = NOW
        obj.last_refreshed_at = None
        obj.retired_at = None
        obj.status = "active"
        obj.target_schema = "public"

    mock_db.refresh = _refresh

    measure_col = ResolvedMeasureCol(
        measure_id=uuid.uuid4(),
        measure_name="revenue",
        stat_type="sum",
        aggregation_function="sum",
        source_table_id=uuid.uuid4(),
        source_column_name="amount",
        physical_col_name="revenue__sum",
    )
    layout = ResolvedAggregateLayout(grain_cols=[], measure_cols=[measure_col])

    body = _agg_body()
    body["include_quantiles"] = True

    with (
        patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.aggregates.resolve_aggregate_layout", return_value=layout),
        patch("src.api.aggregates.compute_redundant_partners", return_value={}),
    ):
        resp = await client.post(PREFIX, json=body)

    assert resp.status_code == 201

    quantile_stats = {
        obj.stat_type
        for (call_args, _kw) in [(c.args, c.kwargs) for c in mock_db.add.call_args_list]
        for obj in call_args
        if isinstance(obj, AggregateColumn) and (obj.stat_type or "").startswith("p")
    }
    # The routable median is registered; nothing else.
    assert quantile_stats == {"p50"}
    for dead in ("p01", "p05", "p10", "p25", "p75", "p90", "p95", "p99"):
        assert dead not in quantile_stats


# ---------------------------------------------------------------------------
# Bug-1091 — non-materialisable variant measures rejected at creation
# ---------------------------------------------------------------------------

def _make_variant_measure(name: str, variant_kind: str):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        model_id=TEST_MODEL_ID,
        default_agg="sum",
        is_additive=True,
        variant_kind=variant_kind,
        variant_of_measure_id=str(uuid.uuid4()),
        variant_n=None,
        measure_type="standard",
        calc_agg_mode=None,
        source_column_id=None,
    )


@pytest.mark.asyncio
async def test_create_aggregate_rejects_period_aware_variant(client):
    """Bug-1091: a period-aware variant (needs a calendar JOIN) cannot be
    materialised in a pre-aggregate. The API must reject it with 400 rather
    than registering AggregateColumn rows whose first refresh fails loud."""
    model = make_model(max_aggregates=50)
    variant = _make_variant_measure("revenue_ytd", "ytd")
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)
    # Validation resolves the variant before cap selection is reached.
    mock_db.execute = _make_create_script(
        dims=[], measures=[variant]
    )

    body = _agg_body()
    body["grain"] = ["month"]
    body["measure_names"] = ["revenue_ytd"]

    with (
        patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.aggregates.compute_redundant_partners", return_value={}),
    ):
        resp = await client.post(PREFIX, json=body)

    assert resp.status_code == 400
    assert "period-aware" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_aggregate_rejects_variant_without_source_snapshot(client):
    """Bug-3591/Bug-1091: a window-based variant (CTAS-allowed, e.g. ``lag``)
    whose base source column was never snapshotted (``source_column_id`` NULL —
    the shape a bare API create without source_table_id/source_column_name
    produces) must be rejected at create with a clean 400 instead of registering
    an unbuildable AggregateColumn set whose first scheduled refresh fails loud.

    Reproduces the exact Bug-3591 path: the variant kind passes the
    CTAS_ALLOWED_VARIANTS gate, so the failure must come from
    ``resolve_variant_context`` raising VariantContextError (a ValueError) on the
    missing source-column snapshot, which the handler converts to a 400."""
    model = make_model(max_aggregates=50)
    # A CTAS-allowed window variant (no calendar JOIN needed) with NULL
    # source_column_id — passes the period-aware gate, fails the snapshot gate.
    variant = _make_variant_measure("revenue_lag1", "lag")
    # A time dimension named "month" so resolve_variant_context clears the
    # time-dimension-in-grain check and reaches the source-column snapshot check.
    time_dim = types.SimpleNamespace(
        name="month",
        model_id=TEST_MODEL_ID,
        is_time_dim=True,
        source_column_id=None,
    )
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)
    # Validation resolves the time dimension + variant before cap selection.
    mock_db.execute = _make_create_script(
        dims=[time_dim], measures=[variant]
    )

    body = _agg_body()
    body["grain"] = ["month"]
    body["measure_names"] = ["revenue_lag1"]

    with (
        patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.aggregates.compute_redundant_partners", return_value={}),
    ):
        resp = await client.post(PREFIX, json=body)

    assert resp.status_code == 400
    assert "source column snapshot" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# PATCH — extra fields rejected (Bug-089)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_patch_aggregate_with_grain_returns_422(client):
    """AggregateDefinitionUpdate has extra='forbid'. Sending 'grain'
    in a PATCH body must trigger Pydantic validation error → 422."""
    agg = make_aggregate()
    model = make_model()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)

    with patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.patch(
            f"{PREFIX}/{agg.id}",
            json={"grain": ["region"]},
        )

    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("bad_status", ["banana", None])
@pytest.mark.asyncio
async def test_patch_aggregate_invalid_status_returns_422(client, bad_status):
    """Bug-6549: AggregateDefinition.status is a controlled lifecycle enum.
    An unknown value ("banana") or an explicit null must fail closed with 422 —
    never persist a NOT NULL 500 or an unroutable free-string status that
    silently pulls the aggregate out of every status-driven routing path."""
    agg = make_aggregate()
    model = make_model()
    mock_db = make_mock_db()
    # db.get is called for the Model then the AggregateDefinition; an explicit
    # null passes schema parse and reaches the endpoint body, so both lookups
    # must resolve before the status guard rejects it.
    mock_db.get = AsyncMock(side_effect=[model, agg])

    with patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.patch(
            f"{PREFIX}/{agg.id}",
            json={"status": bad_status},
        )

    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("system_status", ["pending", "invalid"])
@pytest.mark.asyncio
async def test_patch_status_rejected_while_system_managed(client, system_status):
    """Bug-7903 (Fable HIGH #1): a status PATCH must be rejected (409) while the
    aggregate is in a system-managed lifecycle state (pending/invalid). The
    uniform refresh pending-guard commits status="pending" BEFORE it durably
    replaces the target rows; a user PATCH to "active" in that window would re-open
    the DG99-CRITICAL-01 window (serving new rows under the prior run's proof).
    Only the refresh engine may transition out of pending/invalid."""
    from shared.db.models import Model

    agg = make_aggregate(status=system_status)
    model = make_model()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=[model, agg])

    with patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.patch(
            f"{PREFIX}/{agg.id}",
            json={"status": "active"},
        )

    assert resp.status_code == 409, resp.text
    assert system_status in resp.text


# ---------------------------------------------------------------------------
# PATCH — disabled->active flips refresh policy (Bug-6170 enable-path)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_patch_disabled_to_active_enables_refresh_policy(client):
    """Bug-6170: when an aggregate transitions disabled->active via PATCH,
    its AggregateRefreshPolicy.is_enabled must be set to True and the
    aggregate's is_stale must be set to True so the scheduler picks it up
    for its first build.  Without this, an AI aggregate enabled via the UI
    stays permanently unscheduled."""
    from shared.db.models import AggregateDefinition, AggregateRefreshPolicy, Model

    model = make_model()
    agg = make_aggregate(status="disabled")
    agg.is_stale = False
    agg.include_stats = False
    agg.include_quantiles = False

    # Simulate an existing policy row with is_enabled=False (the optimizer
    # creates these for disabled AI aggregates).
    policy = types.SimpleNamespace(
        id=uuid.uuid4(),
        aggregate_definition_id=agg.id,
        refresh_mode="scheduled",
        cron_expression="0 * * * *",
        is_enabled=False,
    )

    mock_db = make_mock_db()

    async def _get(cls, _id):
        return model if cls is Model else agg
    mock_db.get = AsyncMock(side_effect=_get)

    # The disabled->active path queries for the AggregateRefreshPolicy row.
    # Subsequent queries (_get_measure_names, _get_ai_rationale) fall through
    # to the empty default.
    mock_db.execute = _make_execute_script(
        _ScalarResult([policy]),   # policy lookup
    )

    with patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.patch(
            f"{PREFIX}/{agg.id}",
            json={"status": "active"},
        )

    assert resp.status_code == 200, resp.text
    # Policy must have been flipped to enabled.
    assert policy.is_enabled is True
    # Aggregate must be marked stale so the scheduler rebuilds it.
    assert agg.is_stale is True


@pytest.mark.asyncio
async def test_patch_disabled_to_active_creates_policy_when_missing(client):
    """Bug-6170 edge case: if no AggregateRefreshPolicy row exists for the
    aggregate (e.g. a legacy aggregate created before the optimizer added
    policies), the disabled->active transition must create one following
    the same pattern as the create endpoint."""
    from shared.db.models import AggregateDefinition, AggregateRefreshPolicy, Model

    model = make_model()
    agg = make_aggregate(status="disabled")
    agg.is_stale = False
    agg.include_stats = False
    agg.include_quantiles = False

    mock_db = make_mock_db()

    async def _get(cls, _id):
        return model if cls is Model else agg
    mock_db.get = AsyncMock(side_effect=_get)

    # Policy lookup returns None (no row exists).
    mock_db.execute = _make_execute_script(
        _ScalarResult([]),   # scalar_one_or_none -> None
    )

    with (
        patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.aggregates.get_setting", new_callable=AsyncMock, return_value="0 * * * *"),
    ):
        resp = await client.patch(
            f"{PREFIX}/{agg.id}",
            json={"status": "active"},
        )

    assert resp.status_code == 200, resp.text
    # A new policy must have been created.
    added = [c.args[0] for c in mock_db.add.call_args_list]
    policies = [r for r in added if isinstance(r, AggregateRefreshPolicy)]
    assert len(policies) == 1
    assert policies[0].is_enabled is True
    assert policies[0].aggregate_definition_id == agg.id
    assert agg.is_stale is True


@pytest.mark.asyncio
async def test_patch_target_schema_change_marks_aggregate_for_rebuild(client):
    """A changed physical location withholds the old build from routing while
    leaving the existing refresh policy untouched."""
    from shared.db.models import AggregateRefreshPolicy, Model

    model = make_model()
    agg = make_aggregate(status="active")
    agg.is_stale = False
    agg.last_refreshed_at = NOW
    agg.include_stats = False
    agg.include_quantiles = False

    mock_db = make_mock_db()

    async def _get(cls, _id):
        return model if cls is Model else agg
    mock_db.get = AsyncMock(side_effect=_get)
    mock_db.execute = _make_execute_script()  # all queries fall through to empty

    with patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.patch(
            f"{PREFIX}/{agg.id}",
            json={"target_schema": "analytics"},
        )

    assert resp.status_code == 200, resp.text
    # No policy-related objects should have been added.
    added = [c.args[0] for c in mock_db.add.call_args_list]
    policies = [r for r in added if isinstance(r, AggregateRefreshPolicy)]
    assert len(policies) == 0
    assert agg.is_stale is True
    assert agg.last_refreshed_at is None


@pytest.mark.asyncio
async def test_patch_same_target_schema_is_a_freshness_noop(client):
    """Re-saving the same physical location must not trigger a rebuild."""
    from shared.db.models import Model

    model = make_model()
    agg = make_aggregate(status="active")
    agg.is_stale = False
    agg.last_refreshed_at = NOW
    agg.include_stats = False
    agg.include_quantiles = False

    mock_db = make_mock_db()

    async def _get(cls, _id):
        return model if cls is Model else agg
    mock_db.get = AsyncMock(side_effect=_get)
    mock_db.execute = _make_execute_script()

    with patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.patch(
            f"{PREFIX}/{agg.id}",
            json={"target_schema": "public"},
        )

    assert resp.status_code == 200, resp.text
    assert agg.is_stale is False
    assert agg.last_refreshed_at == NOW


# ---------------------------------------------------------------------------
# PATCH — enabling include_stats inserts coverage rows + forces rebuild (HIGH-2)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_patch_enable_include_stats_adds_coverage_rows_and_marks_rebuild(client):
    """Flipping include_stats false->true on an existing aggregate must insert
    the canonical dispersion-stat AggregateColumn coverage rows for each
    eligible base measure, and withhold routing (NULL last_refreshed_at) until
    the scheduler rebuilds the physical columns. Without this the boolean is
    inert and exact-grain STDDEV queries silently fall back to source."""
    from shared.db.models import AggregateColumn, AggregateDefinition, Measure, Model
    from shared.aggregate_stats import STAT_TYPES

    model = make_model()
    measure_id = uuid.uuid4()
    agg = make_aggregate()
    agg.include_stats = False
    agg.include_quantiles = False
    agg.is_stale = False

    base_col = types.SimpleNamespace(
        measure_id=measure_id, stat_type="sum",
        aggregation_function="sum", physical_col_name="revenue__sum",
    )
    measure = types.SimpleNamespace(id=measure_id, name="revenue")

    mock_db = make_mock_db()

    async def _get(cls, _id):
        return model if cls is Model else agg
    mock_db.get = AsyncMock(side_effect=_get)
    # 1st execute -> existing columns; 2nd execute -> eligible measures.
    mock_db.execute = _make_execute_script(
        _ScalarResult([base_col]),
        _ScalarResult([measure]),
    )

    with patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.patch(
            f"{PREFIX}/{agg.id}",
            json={"include_stats": True},
        )

    assert resp.status_code == 200, resp.text
    added = [c.args[0] for c in mock_db.add.call_args_list]
    stat_rows = [r for r in added if isinstance(r, AggregateColumn)]
    assert {r.stat_type for r in stat_rows} == set(STAT_TYPES)
    assert {r.physical_col_name for r in stat_rows} == {f"revenue__{s}" for s in STAT_TYPES}
    assert all(r.measure_id == measure_id for r in stat_rows)
    # Routing withheld until rebuild.
    assert agg.last_refreshed_at is None
    assert agg.is_stale is True


@pytest.mark.asyncio
async def test_patch_disable_include_stats_removes_coverage_rows(client):
    """Mirror case: flipping include_stats true->false must retire the stat
    coverage rows so the matcher does not match exact-grain stat queries that
    the next (stats-off) refresh will not materialise."""
    from shared.db.models import AggregateDefinition, Model
    from shared.aggregate_stats import STAT_TYPES

    model = make_model()
    measure_id = uuid.uuid4()
    agg = make_aggregate()
    agg.include_stats = True
    agg.include_quantiles = False
    agg.is_stale = False

    base_col = types.SimpleNamespace(
        measure_id=measure_id, stat_type="sum",
        aggregation_function="sum", physical_col_name="revenue__sum",
    )
    stat_cols = [
        types.SimpleNamespace(
            measure_id=measure_id, stat_type=s,
            aggregation_function=None, physical_col_name=f"revenue__{s}",
        )
        for s in STAT_TYPES
    ]

    mock_db = make_mock_db()

    async def _get(cls, _id):
        return model if cls is Model else agg
    mock_db.get = AsyncMock(side_effect=_get)
    mock_db.execute = _make_execute_script(_ScalarResult([base_col, *stat_cols]))

    with patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.patch(
            f"{PREFIX}/{agg.id}",
            json={"include_stats": False},
        )

    assert resp.status_code == 200, resp.text
    deleted = [c.args[0] for c in mock_db.delete.call_args_list]
    assert {d.stat_type for d in deleted} == set(STAT_TYPES)
    assert agg.last_refreshed_at is None
    assert agg.is_stale is True


# ---------------------------------------------------------------------------
# Bug-8026 (API half) — the body ``target_id`` must belong to the path model
# ---------------------------------------------------------------------------
#
# Test escape: ``create_aggregate`` built ``AggregateDefinition(model_id=...,
# **body.model_dump())`` and ``target_id`` is a NOT NULL foreign key to
# ``data_targets``. Every existing create test supplied a target id that no
# assertion ever tied to the model, so nothing noticed that the id was never
# checked. The optimizer's lifecycle twin has validated it since Bug-8026;
# only this API path was left open, and a foreign target_id points the
# materialisation CTAS and every scheduled refresh at another project's
# warehouse connection.
#
# Guard: these tests plus the real-Postgres route tests in
# ``tests/integration/test_body_fk_route_adoption_db.py``. Tier: T3.
#
# Each denial test asserts the rejection REASON, not merely the status: 422 is
# also FastAPI's own request-validation status, and its detail is a LIST, so a
# bare status assertion could pass for an entirely unrelated reason.

@pytest.mark.asyncio
async def test_create_aggregate_rejects_a_target_outside_the_model(client):
    """A target id that does not resolve inside the path project+model is
    refused with the body-FK 422, and nothing is persisted."""
    model = make_model(max_aggregates=50)
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)
    # The ownership SELECT is the first statement and finds nothing — the one
    # outcome the primitive produces for "no such target" and "another
    # project's target" alike (its anti-oracle property).
    mock_db.execute = _make_execute_script(_EMPTY_RESULT)

    with (
        patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "src.api.aggregates.resolve_aggregate_layout",
            return_value=_stub_layout(),
        ),
        patch("src.api.aggregates.compute_redundant_partners", return_value={}),
    ):
        resp = await client.post(PREFIX, json=_agg_body())

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, dict), (
        "must be the body-FK error shape, not FastAPI's own 422 validation list"
    )
    assert detail["error_code"] == "REF_NOT_IN_MODEL"
    assert detail["field"] == "target_id"
    assert detail["ids"] == [str(TARGET_ID)]
    assert "a data target" in detail["message"]
    mock_db.add.assert_not_called()
    mock_db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_aggregate_refuses_a_foreign_target_before_evicting(client):
    """The guard runs BEFORE ``_enforce_max_aggregates``.

    At the cap, cap enforcement retires the lowest-scored aggregate and DROPS
    its physical table. Validating the target afterwards would let a request
    that is about to be refused destroy a live aggregate on its way out — a
    denial-of-service reachable by any modeler in the caller's own project.
    """
    model = make_model(max_aggregates=2)
    lowest = make_aggregate(agg_id=uuid.uuid4(), status="active", estimated_hit_rate=0.05)

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)
    mock_db.execute = _make_execute_script(
        _EMPTY_RESULT,            # target ownership lookup: no such target here
        _ScalarResult([2]),       # count query: at cap (must never be reached)
        _ScalarResult([lowest]),  # lowest-scored query
    )
    drop_table = AsyncMock(return_value=False)

    with (
        patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "src.api.aggregates.resolve_aggregate_layout",
            return_value=_stub_layout(),
        ),
        patch("src.api.aggregates.compute_redundant_partners", return_value={}),
        patch(
            "src.api.aggregates.drop_aggregate_physical_table", new=drop_table
        ),
    ):
        resp = await client.post(PREFIX, json=_agg_body())

    assert resp.status_code == 422
    assert lowest.status == "active", "a refused create must not retire anything"
    assert lowest.retired_at is None
    drop_table.assert_not_awaited()
    mock_db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_bug_8939_invalid_create_does_not_drop_live_cap_victim(client):
    """A rejectable create must finish validation before cap eviction.

    The physical DROP is outside the metadata transaction.  Before Bug-8939,
    an unknown measure at the cap selected and dropped the incumbent and only
    then returned 400; rolling back restored ``active`` metadata over a missing
    table.  This guard makes the incumbent observable and asserts both halves:
    no DROP and no in-memory retirement on the rejected request.
    """
    model = make_model(max_aggregates=1)
    incumbent = make_aggregate(
        agg_id=uuid.uuid4(), status="active", estimated_hit_rate=0.05
    )
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)

    async def _execute(stmt, *_args, **_kwargs):
        sql = str(stmt)
        if "data_targets" in sql:
            return _owned_target_result()
        if "count(*)" in sql and "aggregate_definitions" in sql:
            return _ScalarResult([1])
        if "aggregate_definitions" in sql:
            return _ScalarResult([incumbent])
        # The model intentionally has no measures, so the submitted name is
        # invalid.  Every other validation collection is empty.
        return _EMPTY_RESULT

    mock_db.execute = AsyncMock(side_effect=_execute)
    drop_table = AsyncMock(return_value=True)
    body = _agg_body()
    body["measure_names"] = ["does_not_exist"]

    with (
        patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "src.api.aggregates.drop_aggregate_physical_table", new=drop_table
        ),
    ):
        resp = await client.post(PREFIX, json=body)

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Unknown measure: 'does_not_exist'"
    assert incumbent.status == "active"
    assert incumbent.retired_at is None
    drop_table.assert_not_awaited()
    mock_db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_aggregate_accepts_a_target_owned_by_the_model(client):
    """The guard is an ownership check, not a blanket denial.

    Bug-8864 shipped a scope guard that rejected essentially everything; only a
    positive test catches that direction. This one also pins the producer side:
    the accepted ``target_id`` is what actually lands on the persisted
    definition, so the aggregate materialises where the modeller asked.
    """
    from shared.db.models import AggregateDefinition

    model = make_model(max_aggregates=50)
    agg = make_aggregate()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)
    mock_db.execute = _make_create_script(_ScalarResult([0]))

    async def _refresh(obj):
        obj.id = agg.id
        obj.model_id = TEST_MODEL_ID
        obj.source_row_count = None
        obj.agg_row_count = None
        obj.estimated_hit_rate = None
        obj.created_at = NOW
        obj.updated_at = NOW
        obj.last_refreshed_at = None
        obj.retired_at = None
        obj.status = "active"
        obj.target_schema = "public"

    mock_db.refresh = _refresh

    with (
        patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "src.api.aggregates.resolve_aggregate_layout",
            return_value=_stub_layout(),
        ),
        patch("src.api.aggregates.compute_redundant_partners", return_value={}),
    ):
        resp = await client.post(PREFIX, json=_agg_body())

    assert resp.status_code == 201
    definitions = [
        c.args[0]
        for c in mock_db.add.call_args_list
        if isinstance(c.args[0], AggregateDefinition)
    ]
    assert len(definitions) == 1
    assert definitions[0].target_id == TARGET_ID
    assert definitions[0].model_id == TEST_MODEL_ID


# ---------------------------------------------------------------------------
# Cap enforcement — lowest hit-rate is retired
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bug_8939_cap_retirement_commits_before_physical_drop(client):
    """Cap replacement follows stop-routing -> commit -> DROP -> evidence."""
    from shared.db.models import AggregateLifecycleEvent

    model = make_model(max_aggregates=2)
    lowest = make_aggregate(agg_id=uuid.uuid4(), status="active", estimated_hit_rate=0.05)
    new_agg = make_aggregate(agg_id=uuid.uuid4())

    mock_db = make_mock_db()
    order: list[str] = []

    async def _commit():
        order.append("commit")

    async def _drop(_agg, _db, *, reason):
        assert _agg.status == "retired"
        assert order == ["commit"], (
            "the victim must be durably non-routable before physical removal"
        )
        order.append("drop")
        return True

    mock_db.get = AsyncMock(return_value=model)
    mock_db.commit = AsyncMock(side_effect=_commit)
    mock_db.execute = _make_create_script(
        _ScalarResult([2]),       # count query: at cap
        _ScalarResult([lowest]),  # lowest-scored query
    )

    async def _refresh(obj):
        obj.id = new_agg.id
        obj.model_id = TEST_MODEL_ID
        obj.source_row_count = None
        obj.agg_row_count = None
        obj.estimated_hit_rate = None
        obj.created_at = NOW
        obj.updated_at = NOW
        obj.last_refreshed_at = None
        obj.retired_at = None
        obj.status = "active"
        obj.target_schema = "public"

    mock_db.refresh = _refresh

    with (
        patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "src.api.aggregates.resolve_aggregate_layout",
            return_value=_stub_layout(),
        ),
        patch(
            "src.api.aggregates.compute_redundant_partners",
            return_value={},
        ),
        # Exercise the post-commit purge boundary without resolving a real
        # target connection. The side effect above asserts lifecycle order.
        patch(
            "src.api.aggregates.drop_aggregate_physical_table",
            new=AsyncMock(side_effect=_drop),
        ),
    ):
        resp = await client.post(PREFIX, json=_agg_body())

    assert resp.status_code == 201
    # lowest must have been marked retired
    assert lowest.status == "retired"
    assert lowest.retired_at is not None
    lifecycle_events = [
        call.args[0]
        for call in mock_db.add.call_args_list
        if isinstance(call.args[0], AggregateLifecycleEvent)
    ]
    assert len(lifecycle_events) == 1
    assert lifecycle_events[0].aggregate_id == lowest.id
    assert lifecycle_events[0].event_type == "retired"
    assert lifecycle_events[0].reason.startswith("cap_enforcement:")
    assert order == ["commit", "drop", "commit"], (
        "the second commit durably records purge terminal evidence"
    )
    # flush is now called multiple times along the handler: once after
    # the retirement, once after the new aggregate insert, and once
    # per helper hop. Any non-zero count is fine — the invariant is
    # that the retirement committed.
    assert mock_db.flush.called


@pytest.mark.asyncio
async def test_cap_enforcement_not_triggered_below_cap(client):
    """When count < max_aggregates, no retirement happens."""
    from shared.db.models import AggregateLifecycleEvent

    model = make_model(max_aggregates=50)
    agg = make_aggregate()

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)
    mock_db.execute = _make_create_script(_ScalarResult([5]))  # 5 < 50, others fall through

    async def _refresh(obj):
        obj.id = agg.id
        obj.model_id = TEST_MODEL_ID
        obj.source_row_count = None
        obj.agg_row_count = None
        obj.estimated_hit_rate = None
        obj.created_at = NOW
        obj.updated_at = NOW
        obj.last_refreshed_at = None
        obj.retired_at = None
        obj.status = "active"
        obj.target_schema = "public"

    mock_db.refresh = _refresh

    with (
        patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "src.api.aggregates.resolve_aggregate_layout",
            return_value=_stub_layout(),
        ),
        patch(
            "src.api.aggregates.compute_redundant_partners",
            return_value={},
        ),
    ):
        resp = await client.post(PREFIX, json=_agg_body())

    assert resp.status_code == 201
    # The below-cap path mustn't execute the "retire lowest" query
    # (second execute call in _enforce_max_aggregates). We prove that
    # by checking the execute-script only consumed its first row — any
    # call after the count query falls back to the empty result.
    assert not [
        call.args[0]
        for call in mock_db.add.call_args_list
        if isinstance(call.args[0], AggregateLifecycleEvent)
    ]


# ---------------------------------------------------------------------------
# List aggregates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_aggregates(client):
    model = make_model()
    agg = make_aggregate()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)
    mock_db.execute = _make_execute_script(_ScalarResult([agg]))

    with patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.get(PREFIX)

    assert resp.status_code == 200
    assert len(resp.json()) == 1


# ---------------------------------------------------------------------------
# Get single aggregate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_aggregate_found(client):
    model = make_model()
    agg = make_aggregate()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=lambda cls, id_: model if id_ == TEST_MODEL_ID else agg)
    mock_db.execute = _make_execute_script()

    with patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.get(f"{PREFIX}/{TEST_AGG_ID}")

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_aggregate_ai_carries_rationale(client):
    """F-011-03: an AI-created aggregate (creation_reason="ai") serves the LLM
    rationale joined from its AIAggregateRecommendation, so the drawer can show
    the AI rationale box."""
    model = make_model()
    agg = make_aggregate(creation_reason="ai")
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=lambda cls, id_: model if id_ == TEST_MODEL_ID else agg)
    # Sequence: _get_measure_names (scalars().all()) → _get_ai_rationale
    # (scalar_one_or_none()).
    mock_db.execute = _make_execute_script(
        _ScalarResult([]),
        _ScalarResult(["High miss rate on (region, month)."]),
    )

    with patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.get(f"{PREFIX}/{TEST_AGG_ID}")

    assert resp.status_code == 200
    assert resp.json()["rationale"] == "High miss rate on (region, month)."


@pytest.mark.asyncio
async def test_get_aggregate_non_ai_has_null_rationale(client):
    """A non-AI aggregate never queries for a rationale and serves null."""
    model = make_model()
    agg = make_aggregate(creation_reason="manual")
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=lambda cls, id_: model if id_ == TEST_MODEL_ID else agg)
    mock_db.execute = _make_execute_script(_ScalarResult([]))

    with patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.get(f"{PREFIX}/{TEST_AGG_ID}")

    assert resp.status_code == 200
    assert resp.json()["rationale"] is None


@pytest.mark.asyncio
async def test_get_aggregate_wrong_model(client):
    model = make_model()
    agg = make_aggregate(model_id=uuid.uuid4())
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=lambda cls, id_: model if id_ == TEST_MODEL_ID else agg)

    with patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.get(f"{PREFIX}/{TEST_AGG_ID}")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Delete aggregate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_aggregate(client):
    model = make_model()
    agg = make_aggregate()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=lambda cls, id_: model if id_ == TEST_MODEL_ID else agg)

    with (
        patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "shared.physical_cleanup.schedule_model_physical_cleanup",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "shared.physical_cleanup.attempt_scheduled_physical_cleanup",
            new=AsyncMock(return_value=0),
        ),
    ):
        resp = await client.delete(f"{PREFIX}/{TEST_AGG_ID}")

    assert resp.status_code == 204
    mock_db.delete.assert_called_once_with(agg)


@pytest.mark.asyncio
async def test_bug_9051_delete_schedules_the_physical_table_drop(client):
    """Bug-9051: deleting an aggregate definition must not orphan its table.

    The definition row is the ONLY record of the materialised table's name,
    target and schema, and the retirement sweep enumerates RETIRED definitions
    only — so a plain delete leaked the storage permanently with nothing left to
    find it.

    Asserts the whole ordering, not just that a drop happens: the cleanup
    identity is persisted INSIDE the delete transaction (before the commit that
    removes the owning row), and the physical DROP is attempted only AFTER that
    commit — the Bug-8126/Bug-9148 stop-routing-before-removal order that every
    other drop site on this codebase already follows.
    """
    model = make_model()
    agg = make_aggregate()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(
        side_effect=lambda cls, id_: model if id_ == TEST_MODEL_ID else agg
    )

    order: list[str] = []
    scheduled: dict = {}

    async def _schedule(db, *, model_id, aggregate_definitions, pocket_definitions,
                        requested_by, **_kw):
        order.append("schedule")
        scheduled["aggregates"] = list(aggregate_definitions)
        scheduled["model_id"] = model_id
        scheduled["requested_by"] = requested_by
        return [uuid.uuid4()]

    async def _delete(obj):
        order.append("delete")

    async def _commit():
        order.append("commit")

    async def _drain(_db):
        order.append("drain")
        return 1

    mock_db.delete = AsyncMock(side_effect=_delete)
    mock_db.commit = AsyncMock(side_effect=_commit)

    with (
        patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "shared.physical_cleanup.schedule_model_physical_cleanup",
            new=AsyncMock(side_effect=_schedule),
        ),
        patch(
            "shared.physical_cleanup.attempt_scheduled_physical_cleanup",
            new=AsyncMock(side_effect=_drain),
        ),
    ):
        resp = await client.delete(f"{PREFIX}/{TEST_AGG_ID}")

    assert resp.status_code == 204
    assert scheduled["aggregates"] == [agg], (
        "the deleted aggregate itself must be the cleanup subject"
    )
    assert scheduled["model_id"] == TEST_MODEL_ID
    assert scheduled["requested_by"] == "aggregate_delete"
    assert order == ["schedule", "delete", "commit", "drain"], (
        "Bug-9051: the drop identity must be durable before the row is removed, "
        f"and the DROP must follow the metadata commit; got {order}"
    )


@pytest.mark.asyncio
async def test_bug_9051_delete_refuses_when_the_table_cannot_be_resolved(client):
    """Fail closed rather than leak.

    If the physical table's target identity cannot be resolved (missing target,
    cross-project connection), committing the delete would discard the only
    retry ownership record — the exact Bug-8140 failure mode. The delete is
    refused with a 409 that says what to repair, and the definition survives.
    """
    from shared.physical_cleanup import PhysicalCleanupIdentityError

    model = make_model()
    agg = make_aggregate()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(
        side_effect=lambda cls, id_: model if id_ == TEST_MODEL_ID else agg
    )

    with (
        patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "shared.physical_cleanup.schedule_model_physical_cleanup",
            new=AsyncMock(
                side_effect=PhysicalCleanupIdentityError("no safe target identity")
            ),
        ),
        patch(
            "shared.physical_cleanup.attempt_scheduled_physical_cleanup",
            new=AsyncMock(return_value=0),
        ),
    ):
        resp = await client.delete(f"{PREFIX}/{TEST_AGG_ID}")

    assert resp.status_code == 409
    mock_db.delete.assert_not_called()
    mock_db.commit.assert_not_awaited()
    mock_db.rollback.assert_awaited()


# ---------------------------------------------------------------------------
# Project ownership validation — Finding 10
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_aggregates_wrong_project_returns_404(client):
    """Finding 10: model from another project yields 404."""
    wrong_project_model = make_model(project_id=uuid.uuid4())
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=wrong_project_model)

    with patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.get(PREFIX)

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Row count column — always present, even without measures (Finding 4)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_aggregate_always_adds_row_count_column(client):
    """Finding 4: __row_count__count must be added even when
    layout.measure_cols is empty (measure-less aggregate)."""
    model = make_model(max_aggregates=50)
    agg = make_aggregate()
    mock_db = make_mock_db()

    mock_db.get = AsyncMock(return_value=model)
    mock_db.execute = _make_create_script(_ScalarResult([0]))

    async def _refresh(obj):
        obj.id = agg.id
        obj.model_id = TEST_MODEL_ID
        obj.source_row_count = None
        obj.agg_row_count = None
        obj.estimated_hit_rate = None
        obj.created_at = NOW
        obj.updated_at = NOW
        obj.last_refreshed_at = None
        obj.retired_at = None
        obj.status = "active"
        obj.target_schema = "public"

    mock_db.refresh = _refresh

    empty_layout = _stub_layout()
    assert empty_layout.measure_cols == []

    with (
        patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "src.api.aggregates.resolve_aggregate_layout",
            return_value=empty_layout,
        ),
        patch(
            "src.api.aggregates.compute_redundant_partners",
            return_value={},
        ),
    ):
        resp = await client.post(PREFIX, json=_agg_body())

    assert resp.status_code == 201
    added_objects = [c.args[0] for c in mock_db.add.call_args_list]
    row_count_cols = [
        obj for obj in added_objects
        if getattr(obj, "physical_col_name", None) == "__row_count__count"
    ]
    assert len(row_count_cols) == 1, (
        "__row_count__count column must be added even with empty measure_cols"
    )
