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
        return self

    def all(self):
        return self._items

    def scalar_one(self):
        return self._items[0] if self._items else 0

    def scalar_one_or_none(self):
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

    # _enforce_max_aggregates: get(Model) + execute(count)
    mock_db.get = AsyncMock(return_value=model)
    # count query returns 0 (below cap); subsequent queries issued by the
    # resolver + redundant-partner + _get_measure_names helpers return
    # empty results so the handler completes its happy path.
    mock_db.execute = _make_execute_script(_ScalarResult([0]))

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
    # _enforce_max_aggregates count(0) → dims(empty) → measures([variant])
    mock_db.execute = _make_execute_script(
        _ScalarResult([0]),
        _EMPTY_RESULT,
        _ScalarResult([variant]),
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
    # _enforce_max_aggregates count(0) → dims([time_dim]) → measures([variant])
    mock_db.execute = _make_execute_script(
        _ScalarResult([0]),
        _ScalarResult([time_dim]),
        _ScalarResult([variant]),
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
# Cap enforcement — lowest hit-rate is retired
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cap_enforcement_retires_lowest_hit_rate(client):
    """When count == max_aggregates, lowest hit-rate aggregate is retired."""
    from shared.db.models import AggregateLifecycleEvent

    model = make_model(max_aggregates=2)
    lowest = make_aggregate(agg_id=uuid.uuid4(), status="active", estimated_hit_rate=0.05)
    new_agg = make_aggregate(agg_id=uuid.uuid4())

    mock_db = make_mock_db()

    mock_db.get = AsyncMock(return_value=model)
    mock_db.execute = _make_execute_script(
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
        # The retire path now drops the physical table via the shared helper;
        # stub it out (this mock_db.get returns the model for every lookup, so
        # the real helper can't resolve a target). This test asserts the
        # retire + lifecycle-event behaviour, not the DROP.
        patch(
            "src.api.aggregates.drop_aggregate_physical_table",
            new=AsyncMock(return_value=False),
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
    mock_db.execute = _make_execute_script(_ScalarResult([5]))  # 5 < 50, others fall through

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

    with patch("src.api.aggregates.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.delete(f"{PREFIX}/{TEST_AGG_ID}")

    assert resp.status_code == 204
    mock_db.delete.assert_called_once_with(agg)


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
    mock_db.execute = _make_execute_script(_ScalarResult([0]))

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
