"""Composite KPI indicator agreement — single /evaluate vs /evaluate-batch.

B9 round 2:

1. ALL indicators (status, trend, formatted value) on the same composite
   must agree everywhere it renders. Both endpoints converge on the shared
   post-score pipeline (_finalize_kpi_response), so a composite scoring
   43.75 against target 50 is "Near Target" amber on BOTH endpoints —
   previously the batch third pass skipped threshold/trend/format and the
   scorecard + $KPIs surface showed "Off Target" red while KpisPanel was
   right.
2. Nested composites (composite child of a composite) score recursively
   on both endpoints — never the silent placeholder 0.0 — with fail-loud
   cycle and depth guards.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from .result_fakes import FakeScalarResult

from shared.middleware.internal_bypass import internal_request_headers
from shared.schemas.pydantic_models import KPIEvaluateResponse

from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    make_mock_db,
)

# F-017-12: shim caller_has_role to the token-role decision for these
# mocked-db unit tests (see conftest.kpi_effective_role).
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("kpi_effective_role")]

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/kpis"


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return FakeScalarResult(self._items)

    def all(self):
        return list(self._items)

    def first(self):
        return self._items[0] if self._items else None

    def scalar_one(self):
        # Bug-7982 R6: evaluate_batch (service context) issues
        # ``SELECT clock_timestamp()`` for the kpi_latest ordering marker.
        return self._items[0] if self._items else None


def _kpi(
    *,
    name: str,
    kpi_type: str | None = None,
    expression: str | None = 'measure("Revenue")',
    parent_kpi_id: uuid.UUID | None = None,
    weight: float | None = 1.0,
    target_value: float | None = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        display_name=name,
        description=None,
        display_folder=None,
        value_measure_id=None,
        goal_measure_id=None,
        status_expression=None,
        trend_expression=None,
        status_graphic="Traffic Light",
        trend_graphic="Standard Arrow",
        weight=weight,
        parent_kpi_id=parent_kpi_id,
        certification_status="certified",
        owner_user_id=None,
        created_at=NOW,
        updated_at=NOW,
        expression=expression,
        kpi_type=kpi_type,
        calc_agg_mode="automatic",
        inner_agg=None,
        inner_grain=None,
        outer_agg=None,
        at_grain=None,
        non_additive_agg=None,
        carry_forward=False,
        target_type="static" if target_value is not None else None,
        target_value=target_value,
        target_measure_id=None,
        target_expression=None,
        target_period=None,
        direction="higher_is_better",
        presentation_type=None,
        presentation_meta=None,
        trend_period="month",
        trend_threshold=0.01,
        trend_sparkline_periods=12,
        format_token=None,
        format_custom=None,
        unit_label=None,
        null_display_value="N/A",
        indicator_type=None,
        time_dimension_id=None,
        snapshot_frequency=None,
        snapshot_retention=None,
        created_by=None,
        is_deployed=False,
        deployed_at=None,
        evaluation_order=None,
        replacement_id=None,
        business_definition=None,
    )


_VERSION_ID = uuid.uuid4()


def _kpi_snapshot_dict(kpi: types.SimpleNamespace) -> dict:
    """Serialise a test KPI namespace into a snapshot dict for the deployed
    version (UUIDs as strings, as the serialiser stores)."""
    return {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in vars(kpi).items()}


def _version_with_kpis(kpi_list: list) -> types.SimpleNamespace:
    """Build a mock ModelVersion namespace with a snapshot containing ``kpi_list``."""
    return types.SimpleNamespace(
        id=_VERSION_ID,
        model_id=TEST_MODEL_ID,
        snapshot_json={
            "schema_version": "1.0",
            "measures": [{"id": str(uuid.uuid4()), "name": "Revenue"}],
            "kpis": [_kpi_snapshot_dict(k) for k in kpi_list],
        },
    )


def _model(deployed: bool = False) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=TEST_MODEL_ID,
        slug="acme_sales",
        fiscal_year_start_month=None,
        deployed_version_id=_VERSION_ID if deployed else None,
        deploy_epoch=1 if deployed else 0,
        data_epoch=0,
    )


def _entity_db(
    model,
    kpi_query_results: list[list],
    get_kpis: list | None = None,
    version: types.SimpleNamespace | None = None,
):
    """Mock DB whose execute() routes by selected ORM entity.

    Measure selects return []; KPISnapshot selects return [] (no
    snapshots); KPI selects pop sequential responses (requested load,
    then one auto-load level per call). ``get_kpis`` lists KPIs that
    db.get() must resolve (the single-evaluate route loads the requested
    KPI via db.get, not via a select).

    ``version`` — optional ModelVersion namespace for deployed-snapshot
    resolution (``kpi_deploy_resolver`` calls ``db.get(ModelVersion, id)``).
    """
    from shared.db.models import Model as ORMModel, ModelVersion as ORMVersion

    db = make_mock_db()
    kpi_responses = [list(r) for r in kpi_query_results]
    gettable = list(get_kpis or [])
    for batch in kpi_query_results:
        gettable.extend(batch)
    all_model_kpis = []
    seen_kpi_ids = set()
    for candidate in gettable:
        if candidate.id not in seen_kpi_ids:
            all_model_kpis.append(candidate)
            seen_kpi_ids.add(candidate.id)

    async def side_get(cls, obj_id, *a, **kw):
        if cls is ORMVersion and version is not None and obj_id == version.id:
            return version
        if cls is ORMModel or obj_id == TEST_MODEL_ID:
            return model
        for k in gettable:
            if k.id == obj_id:
                return k
        return None

    async def side_execute(stmt, *a, **kw):
        # Bug-7982 R6: the service-context publish path issues
        # ``SELECT clock_timestamp()`` for the kpi_latest ordering marker.
        if "clock_timestamp" in str(stmt):
            return _ScalarResult([NOW])
        entity = None
        descriptions = getattr(stmt, "column_descriptions", None)
        if descriptions:
            entity = descriptions[0].get("entity")
        name = getattr(entity, "__name__", "")
        if name == "KPI":
            sql = str(stmt)
            # Single-evaluate cache admission reads the full current model to
            # fingerprint a target-aware dependency closure. Keep that query
            # separate from the ordered dependency-load responses below.
            where_clause = sql.partition("WHERE")[2]
            if (
                "kpis.name =" not in where_clause
                and "kpis.id IN" not in where_clause
                and "kpis.parent_kpi_id" not in where_clause
            ):
                return _ScalarResult(all_model_kpis)
            if kpi_responses:
                return _ScalarResult(kpi_responses.pop(0))
            return _ScalarResult([])
        return _ScalarResult([])

    db.get = AsyncMock(side_effect=side_get)
    db.execute = AsyncMock(side_effect=side_execute)
    return db


def _fake_single_for(values: dict):
    """_evaluate_single_kpi stub: returns (value, target) per KPI id.

    For composite parents it emulates the pass-1 placeholder evaluation
    including the WRONG placeholder-derived indicators, so the tests prove
    the third pass replaces every indicator, not just the value.
    """
    async def fake_single(kpi, *a, **kw):
        v, t = values[kpi.id]
        return KPIEvaluateResponse(
            kpi_id=kpi.id,
            value=v,
            target=t,
            status=-1 if kpi.kpi_type == "composite" else None,
            status_label="Off Target" if kpi.kpi_type == "composite" else None,
            status_color="#D32F2F" if kpi.kpi_type == "composite" else None,
        )
    return fake_single


_INDICATOR_FIELDS = (
    "value", "target", "status", "status_label", "status_color",
    "trend", "trend_label", "trend_pct",
    "formatted_value", "formatted_target", "formatted_variance",
)


@pytest.mark.asyncio
async def test_composite_indicators_agree_single_vs_batch(client):
    """Composite 43.75 vs target 50 -> 'Near Target' amber on BOTH endpoints.

    Children score 50 and 25 with weights 3 and 1 (normalised 0.75/0.25):
    0.75*50 + 0.25*25 = 43.75; 43.75/50 = 0.875 -> Near Target (#F57C00).
    """
    parent = _kpi(
        name="health_score", kpi_type="composite",
        expression="literal(0)", target_value=50.0,
    )
    child_a = _kpi(
        name="child_a", parent_kpi_id=parent.id, weight=3.0,
    )
    child_b = _kpi(
        name="child_b", parent_kpi_id=parent.id, weight=1.0,
    )
    values = {
        parent.id: (0.0, 50.0),       # placeholder value, real target
        child_a.id: (100.0, 200.0),   # -> 50
        child_b.id: (25.0, 100.0),    # -> 25
    }

    # Single /evaluate: undeployed model (builder surface) — live draft path
    model = _model(deployed=False)
    db_single = _entity_db(model, [[child_a, child_b]], get_kpis=[parent])
    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db_single)),
        patch("src.api.kpis.resolve_effective_persona",
              new_callable=AsyncMock, return_value=None),
        patch("src.api.kpis._evaluate_single_kpi",
              side_effect=_fake_single_for(values)),
        patch("src.api.kpis._build_measure_provider", return_value=MagicMock()),
    ):
        single_resp = await client.post(f"{PREFIX}/{parent.id}/evaluate")
    assert single_resp.status_code == 200
    single = single_resp.json()

    # /evaluate-batch with ONLY the parent requested (the Bug-1031 trap).
    # F-017-01: the publish path (kpi_latest) requires a DEPLOYED model, so
    # provide a deployed model + a version snapshot containing all test KPIs.
    # The extra leading KPI response pop (all_live for the resolver) is the
    # first entry.
    all_kpis = [parent, child_a, child_b]
    model_batch = _model(deployed=True)
    version = _version_with_kpis(all_kpis)
    db_batch = _entity_db(
        model_batch,
        [all_kpis, [parent], [child_a, child_b]],
        version=version,
    )
    upsert_mock = AsyncMock()
    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db_batch)),
        patch("src.api.kpis.resolve_effective_persona",
              new_callable=AsyncMock, return_value=None),
        patch("src.api.kpis._batch_get_measure_values",
              new_callable=AsyncMock, return_value={}),
        patch("src.api.kpis._evaluate_single_kpi",
              side_effect=_fake_single_for(values)),
        patch("src.api.kpis._upsert_kpi_latest_batch", upsert_mock),
    ):
        # F-017-11: kpi_latest ($KPIs) is published only from the governed
        # service-context sweep. Send the verified internal-service marker so
        # the publish path fires and the indicator-agreement assertion below
        # still exercises the upsert payload (a per-user render correctly
        # skips the publish — covered by test_evaluate_batch_user_render_does_not_publish).
        batch_resp = await client.post(
            f"{PREFIX}/evaluate-batch",
            json={"kpi_ids": [str(parent.id)]},
            headers=internal_request_headers(),
        )
    assert batch_resp.status_code == 200
    batch_results = batch_resp.json()["results"]
    assert len(batch_results) == 1
    batch = batch_results[0]

    # Exact business outcome on both endpoints
    for data, endpoint in ((single, "single"), (batch, "batch")):
        assert data["value"] == pytest.approx(43.75), endpoint
        assert data["status"] == 0, endpoint
        assert data["status_label"] == "Near Target", endpoint
        assert data["status_color"] == "#F57C00", endpoint

    # EVERY indicator field identical across the two endpoints
    for f in _INDICATOR_FIELDS:
        assert single[f] == batch[f], f"indicator '{f}' disagrees"

    # The kpi_latest upsert (feeds the $KPIs virtual table) receives the
    # corrected indicators, not the pass-1 placeholder ones.
    upsert_mock.assert_awaited_once()
    result_map = upsert_mock.await_args.args[3]
    assert result_map[parent.id].status == 0
    assert result_map[parent.id].status_label == "Near Target"
    assert result_map[parent.id].value == pytest.approx(43.75)


@pytest.mark.asyncio
async def test_nested_composite_scores_recursively_both_endpoints(client):
    """Composite-of-composite returns the recursive score, never 0.0.

    mid: children 50 @0.6 + 25 @0.4 -> 43.75 is wrong here; weights 0.6/0.4
    give 0.6*50 + 0.4*25 = 40. grandparent: mid raw 40 vs target 50 ->
    normalised 80 @ weight 1 -> 80.0.
    """
    model = _model()
    grandparent = _kpi(
        name="overall_health", kpi_type="composite",
        expression="literal(0)", target_value=100.0,
    )
    mid = _kpi(
        name="financial_health", kpi_type="composite",
        expression="literal(0)", parent_kpi_id=grandparent.id,
        weight=1.0, target_value=50.0,
    )
    child_a = _kpi(name="leaf_a", parent_kpi_id=mid.id, weight=0.6)
    child_b = _kpi(name="leaf_b", parent_kpi_id=mid.id, weight=0.4)
    values = {
        grandparent.id: (0.0, 100.0),
        mid.id: (0.0, 50.0),
        child_a.id: (100.0, 200.0),  # -> 50
        child_b.id: (25.0, 100.0),   # -> 25
    }

    # Single /evaluate on the grandparent
    db_single = _entity_db(model, [[mid], [child_a, child_b]], get_kpis=[grandparent])
    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db_single)),
        patch("src.api.kpis.resolve_effective_persona",
              new_callable=AsyncMock, return_value=None),
        patch("src.api.kpis._evaluate_single_kpi",
              side_effect=_fake_single_for(values)),
        patch("src.api.kpis._build_measure_provider", return_value=MagicMock()),
    ):
        single_resp = await client.post(f"{PREFIX}/{grandparent.id}/evaluate")
    assert single_resp.status_code == 200
    assert single_resp.json()["value"] == pytest.approx(80.0)

    # /evaluate-batch requesting only the grandparent: transitive auto-load
    db_batch = _entity_db(model, [[grandparent], [mid], [child_a, child_b]])
    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db_batch)),
        patch("src.api.kpis.resolve_effective_persona",
              new_callable=AsyncMock, return_value=None),
        patch("src.api.kpis._batch_get_measure_values",
              new_callable=AsyncMock, return_value={}),
        patch("src.api.kpis._evaluate_single_kpi",
              side_effect=_fake_single_for(values)),
        patch("src.api.kpis._upsert_kpi_latest_batch", AsyncMock()),
    ):
        batch_resp = await client.post(
            f"{PREFIX}/evaluate-batch",
            json={"kpi_ids": [str(grandparent.id)]},
        )
    assert batch_resp.status_code == 200
    results = batch_resp.json()["results"]
    assert len(results) == 1
    assert results[0]["value"] == pytest.approx(80.0)


@pytest.mark.asyncio
async def test_batch_composite_cycle_fails_loud(client):
    """Two composites parenting each other -> explicit error, no silent 0.0."""
    model = _model()
    comp_a = _kpi(name="comp_a", kpi_type="composite", expression="literal(0)")
    comp_b = _kpi(
        name="comp_b", kpi_type="composite", expression="literal(0)",
        parent_kpi_id=comp_a.id,
    )
    comp_a.parent_kpi_id = comp_b.id  # cycle (write guard bypassed)
    values = {comp_a.id: (0.0, None), comp_b.id: (0.0, None)}

    db = _entity_db(model, [[comp_a, comp_b], []])
    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.resolve_effective_persona",
              new_callable=AsyncMock, return_value=None),
        patch("src.api.kpis._batch_get_measure_values",
              new_callable=AsyncMock, return_value={}),
        patch("src.api.kpis._evaluate_single_kpi",
              side_effect=_fake_single_for(values)),
        patch("src.api.kpis._upsert_kpi_latest_batch", AsyncMock()),
    ):
        resp = await client.post(
            f"{PREFIX}/evaluate-batch",
            json={"kpi_ids": [str(comp_a.id), str(comp_b.id)]},
        )
    assert resp.status_code == 200
    for result in resp.json()["results"]:
        assert result["value"] is None
        assert "circular" in (result["status_label"] or "")


@pytest.mark.asyncio
async def test_batch_composite_depth_limit_fails_loud(client):
    """A 6-deep composite chain errors at the root, never silent zeros."""
    model = _model()
    chain = []
    parent_id = None
    for i in range(6):
        c = _kpi(
            name=f"chain_{i}", kpi_type="composite",
            expression="literal(0)", parent_kpi_id=parent_id,
        )
        chain.append(c)
        parent_id = c.id
    values = {c.id: (0.0, None) for c in chain}

    db = _entity_db(model, [list(chain), []])
    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.resolve_effective_persona",
              new_callable=AsyncMock, return_value=None),
        patch("src.api.kpis._batch_get_measure_values",
              new_callable=AsyncMock, return_value={}),
        patch("src.api.kpis._evaluate_single_kpi",
              side_effect=_fake_single_for(values)),
        patch("src.api.kpis._upsert_kpi_latest_batch", AsyncMock()),
    ):
        resp = await client.post(
            f"{PREFIX}/evaluate-batch",
            json={"kpi_ids": [str(c.id) for c in chain]},
        )
    assert resp.status_code == 200
    by_id = {r["kpi_id"]: r for r in resp.json()["results"]}
    root = by_id[str(chain[0].id)]
    assert root["value"] is None
    assert "deeper than" in (root["status_label"] or "")


# ---------------------------------------------------------------------------
# F-017-11 — $KPIs published value must be the governed/service-context value,
# never a per-user RLS/persona-narrowed render.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_batch_service_context_publishes_kpi_latest(client):
    """A governed service-context evaluate-batch (verified internal marker, no
    persona) DOES publish to kpi_latest — the row that feeds the JDBC $KPIs
    virtual table. This is the scheduler snapshot sweep's path.

    F-017-01: the publish gate now requires a DEPLOYED model, so this test
    provides one with a snapshot that pins the KPI definition."""
    kpi = _kpi(name="revenue_kpi", expression='measure("Revenue")', target_value=100.0)
    model = _model(deployed=True)
    version = _version_with_kpis([kpi])
    values = {kpi.id: (90.0, 100.0)}

    # The resolver's all_live select is the first KPI pop; the requested load
    # is the second.
    db = _entity_db(model, [[kpi], [kpi]], version=version)
    upsert_mock = AsyncMock()
    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.resolve_effective_persona",
              new_callable=AsyncMock, return_value=None),
        patch("src.api.kpis._batch_get_measure_values",
              new_callable=AsyncMock, return_value={}),
        patch("src.api.kpis._evaluate_single_kpi",
              side_effect=_fake_single_for(values)),
        patch("src.api.kpis._upsert_kpi_latest_batch", upsert_mock),
    ):
        resp = await client.post(
            f"{PREFIX}/evaluate-batch",
            json={"kpi_ids": [str(kpi.id)]},
            headers=internal_request_headers(),
        )
    assert resp.status_code == 200
    upsert_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_evaluate_batch_clamps_future_marker_and_preserves_earlier_marker(client):
    """Bug-7982 R6 (round-4 #2): drive the REAL evaluate_batch handler and prove
    the round-2/round-3 kpis.py changes:
      - a FUTURE body eval_started_at is CLAMPED to the server clock (else it
        would wedge $KPIs for the whole epoch);
      - an EARLIER (sweep-supplied) marker is PRESERVED (min(earlier, clock) =
        earlier), so the sweep's own write shares this handler's marker (#7).
    The mocked DB's clock_timestamp() returns NOW (2026-01-01)."""
    from datetime import datetime as _dt, timezone as _tz

    kpi = _kpi(name="revenue_kpi", expression='measure("Revenue")', target_value=100.0)
    version = _version_with_kpis([kpi])
    values = {kpi.id: (90.0, 100.0)}

    async def _run(marker_iso):
        db = _entity_db(_model(deployed=True), [[kpi], [kpi]], version=version)
        upsert_mock = AsyncMock()
        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.resolve_effective_persona",
                  new_callable=AsyncMock, return_value=None),
            patch("src.api.kpis._batch_get_measure_values",
                  new_callable=AsyncMock, return_value={}),
            patch("src.api.kpis._evaluate_single_kpi",
                  side_effect=_fake_single_for(values)),
            patch("src.api.kpis._upsert_kpi_latest_batch", upsert_mock),
        ):
            resp = await client.post(
                f"{PREFIX}/evaluate-batch",
                json={"kpi_ids": [str(kpi.id)], "eval_started_at": marker_iso},
                headers=internal_request_headers(),
            )
        assert resp.status_code == 200
        upsert_mock.assert_awaited_once()
        return upsert_mock.call_args.kwargs["eval_started_at"]

    # Future marker -> clamped to the server clock (NOW).
    future = _dt(2099, 1, 1, tzinfo=_tz.utc)
    assert await _run(future.isoformat()) == NOW, "future marker was not clamped to the server clock"

    # Earlier (sweep) marker -> preserved (min(earlier, clock) == earlier).
    earlier = _dt(2025, 6, 1, tzinfo=_tz.utc)
    assert await _run(earlier.isoformat()) == earlier, "an earlier sweep marker must be preserved"


@pytest.mark.asyncio
async def test_evaluate_batch_user_render_does_not_publish(client):
    """A per-user scorecard render (NO internal marker) MUST NOT publish to
    kpi_latest. The caller still gets a correct response, but the shared $KPIs
    row is never overwritten with this user's row-scoped value — closing the
    cross-user row-scope leak (F-017-11)."""
    model = _model()
    kpi = _kpi(name="revenue_kpi", expression='measure("Revenue")', target_value=100.0)
    values = {kpi.id: (90.0, 100.0)}

    db = _entity_db(model, [[kpi]])
    upsert_mock = AsyncMock()
    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.resolve_effective_persona",
              new_callable=AsyncMock, return_value=None),
        patch("src.api.kpis._batch_get_measure_values",
              new_callable=AsyncMock, return_value={}),
        patch("src.api.kpis._evaluate_single_kpi",
              side_effect=_fake_single_for(values)),
        patch("src.api.kpis._upsert_kpi_latest_batch", upsert_mock),
    ):
        resp = await client.post(
            f"{PREFIX}/evaluate-batch",
            json={"kpi_ids": [str(kpi.id)]},
        )
    assert resp.status_code == 200
    # The user still receives their correct value...
    assert resp.json()["results"][0]["value"] == pytest.approx(90.0)
    # ...but the governed $KPIs row is NOT touched by a per-user render.
    upsert_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_evaluate_batch_persona_scope_does_not_publish(client):
    """Even with the internal marker, a persona-narrowed evaluation MUST NOT
    publish to kpi_latest — a persona-scoped value is not the governed
    model-level number (F-017-11, fail-closed)."""
    model = _model()
    kpi = _kpi(name="revenue_kpi", expression='measure("Revenue")', target_value=100.0)
    values = {kpi.id: (90.0, 100.0)}

    persona = types.SimpleNamespace(id=uuid.uuid4(), included_measure_ids=None)
    db = _entity_db(model, [[kpi]])
    upsert_mock = AsyncMock()
    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.resolve_effective_persona",
              new_callable=AsyncMock, return_value=persona),
        patch("src.api.kpis._batch_get_measure_values",
              new_callable=AsyncMock, return_value={}),
        patch("src.api.kpis._evaluate_single_kpi",
              side_effect=_fake_single_for(values)),
        patch("src.api.kpis._upsert_kpi_latest_batch", upsert_mock),
    ):
        resp = await client.post(
            f"{PREFIX}/evaluate-batch",
            json={"kpi_ids": [str(kpi.id)]},
            headers=internal_request_headers(),
        )
    assert resp.status_code == 200
    upsert_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_evaluate_batch_captures_epoch_before_evaluation_not_after(client):
    """opus5 completion-round finding 2.4: the epoch/version binding passed to
    ``_upsert_kpi_latest_batch`` must be captured from ``Model`` at the TOP of
    the request — before ``resolve_served_kpis`` / any per-KPI evaluation —
    never re-derived after evaluation completes. Simulates a revert
    committing WHILE evaluation is running by mutating ``model.deploy_epoch``
    as a side effect wrapped around the REAL ``resolve_served_kpis`` (standing
    in for "evaluation has started"), and asserts ``_upsert_kpi_latest_batch``
    is still called with the PRE-mutation epoch.

    Mutation check: moving the ``_eval_version_id_at_start`` /
    ``_eval_epoch_at_start`` capture in ``evaluate_batch`` to AFTER
    ``resolve_served_kpis`` (or later) makes this test FAIL — it would then
    observe the mutated (post-revert) epoch, exactly reopening the
    wrong-number stamp-timing bug this round fixed.
    """
    from src.kpi_deploy_resolver import resolve_served_kpis as _real_resolve_served_kpis

    model = _model(deployed=True)
    assert model.deploy_epoch == 1  # sanity: this is the epoch to prove was used
    kpi = _kpi(name="revenue_kpi", expression='measure("Revenue")', target_value=100.0)
    version = _version_with_kpis([kpi])
    values = {kpi.id: (150.0, 100.0)}

    db = _entity_db(model, [[kpi]], version=version)

    async def _resolve_and_mutate(db_, model_, live_kpis):
        result = await _real_resolve_served_kpis(db_, model_, live_kpis)
        # Simulate a CONCURRENT revert committing WHILE evaluation is running.
        model_.deploy_epoch = 99
        return result

    upsert_mock = AsyncMock()
    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.resolve_effective_persona",
              new_callable=AsyncMock, return_value=None),
        patch("src.api.kpis.resolve_served_kpis", side_effect=_resolve_and_mutate),
        patch("src.api.kpis._batch_get_measure_values",
              new_callable=AsyncMock, return_value={}),
        patch("src.api.kpis._evaluate_single_kpi",
              side_effect=_fake_single_for(values)),
        patch("src.api.kpis._upsert_kpi_latest_batch", upsert_mock),
    ):
        resp = await client.post(
            f"{PREFIX}/evaluate-batch",
            json={"kpi_ids": [str(kpi.id)]},
            headers=internal_request_headers(),
        )
    assert resp.status_code == 200, resp.text

    # opus5 round-2 finding 4.3: prove the mid-flight mutation actually FIRED
    # (i.e. evaluate_batch really did call the patched resolve_served_kpis).
    # Without this, a future refactor that stops calling resolve_served_kpis
    # (rename, extraction, restructuring the _batch_deployed branch) would
    # silently skip the mutation entirely — model.deploy_epoch would stay 1,
    # the eval_epoch assertion below would still pass, and this test would
    # pass VACUOUSLY, no longer simulating the race it claims to guard.
    assert model.deploy_epoch == 99, (
        "the mid-evaluation mutation never fired (resolve_served_kpis was "
        "not called as expected) — this test is no longer simulating the "
        "concurrent-revert race and its eval_epoch assertion below would "
        "pass vacuously"
    )

    upsert_mock.assert_awaited_once()
    kwargs = upsert_mock.await_args.kwargs
    assert kwargs["eval_epoch"] == 1, (
        "the epoch passed to _upsert_kpi_latest_batch must be the epoch "
        "captured BEFORE evaluation ran (1), not the value after a "
        "concurrent revert mutated it mid-evaluation (99) — capturing late "
        "instead of at evaluation start reopens the wrong-number "
        "stamp-timing bug"
    )
    assert kwargs["eval_version_id"] == model.deployed_version_id
