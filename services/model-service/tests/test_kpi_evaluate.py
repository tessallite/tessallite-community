"""Tests for KPI CRUD and evaluate endpoint."""
from __future__ import annotations

import types
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from .result_fakes import FakeScalarResult

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

MEASURE_VALUE_ID = uuid.uuid4()
MEASURE_GOAL_ID = uuid.uuid4()


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return FakeScalarResult(self._items)

    def all(self):
        return list(self._items)


def _snapshot_rows(values: list[float]) -> list[types.SimpleNamespace]:
    return [
        types.SimpleNamespace(snapshot_at=NOW + timedelta(days=i), value=value)
        for i, value in enumerate(values)
    ]


def _kpi(
    *,
    kpi_id: uuid.UUID | None = None,
    name: str = "Revenue KPI",
    expression: str | None = 'measure("Revenue")',
    certification_status: str = "certified",
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=kpi_id or uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        display_name=name,
        description=None,
        display_folder=None,
        # Legacy v1 — retained for ORM compatibility, not used by application code
        value_measure_id=None,
        goal_measure_id=None,
        status_expression=None,
        trend_expression=None,
        status_graphic="Traffic Light",
        trend_graphic="Standard Arrow",
        weight=1.0,
        parent_kpi_id=None,
        certification_status=certification_status,
        owner_user_id=None,
        created_at=NOW,
        updated_at=NOW,
        # v2 fields
        expression=expression,
        kpi_type=None,
        calc_agg_mode="automatic",
        inner_agg=None,
        inner_grain=None,
        outer_agg=None,
        at_grain=None,
        non_additive_agg=None,
        carry_forward=False,
        target_type=None,
        target_value=None,
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
    )


def _measure(
    measure_id: uuid.UUID = MEASURE_VALUE_ID,
    name: str = "Revenue",
    default_agg: str = "sum",
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=measure_id,
        model_id=TEST_MODEL_ID,
        name=name,
        default_agg=default_agg,
    )


def _model() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=TEST_MODEL_ID,
        slug="acme_sales",
    )


def _deployed_model(version_id: uuid.UUID, epoch: int = 1) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=TEST_MODEL_ID,
        slug="acme_sales",
        deployed_version_id=version_id,
        deploy_epoch=epoch,
        data_epoch=0,
        fiscal_year_start_month=None,
    )


def _snapshot_kpi_dict(kpi: types.SimpleNamespace, **overrides) -> dict:
    """Serialise a test KPI namespace into a snapshot dict (UUIDs as strings)."""
    d = {
        "id": str(kpi.id),
        "model_id": str(kpi.model_id),
        "name": kpi.name,
        "expression": kpi.expression,
        "kpi_type": kpi.kpi_type,
        "calc_agg_mode": kpi.calc_agg_mode,
        "direction": kpi.direction,
        "target_type": kpi.target_type,
        "target_value": kpi.target_value,
        "presentation_meta": kpi.presentation_meta,
        "trend_period": kpi.trend_period,
        "trend_threshold": kpi.trend_threshold,
        "trend_sparkline_periods": kpi.trend_sparkline_periods,
        "null_display_value": kpi.null_display_value,
        "status_graphic": kpi.status_graphic,
        "trend_graphic": kpi.trend_graphic,
    }
    d.update(overrides)
    return d


def _version(version_id: uuid.UUID, kpi_dicts: list[dict]) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=version_id,
        model_id=TEST_MODEL_ID,
        snapshot_json={
            "schema_version": "1.0",
            "measures": [{"id": str(uuid.uuid4()), "name": "Revenue"}],
            "kpis": kpi_dicts,
        },
    )


# ---- CRUD Tests ----

@pytest.mark.asyncio
async def test_list_kpis(client):
    kpi = _kpi()
    db = make_mock_db()
    db.execute = AsyncMock(return_value=_ScalarResult([kpi]))
    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
    ):
        resp = await client.get(PREFIX)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "Revenue KPI"
    assert data[0]["certification_status"] == "certified"


@pytest.mark.asyncio
async def test_create_kpi(client):
    db = make_mock_db()
    kpi = _kpi()

    async def mock_refresh(obj):
        for attr, val in vars(kpi).items():
            setattr(obj, attr, val)

    db.refresh = mock_refresh
    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)):
        resp = await client.post(PREFIX, json={
            "name": "Revenue KPI",
            "status_graphic": "Traffic Light",
            "trend_graphic": "Standard Arrow",
        })
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_kpi_with_governance_fields(client):
    """Bug-6893: certification_status is now accepted on KPICreate. The
    governance guard requires ADMIN authority for privileged statuses like
    'shared'. Bug-9443 (Option A): authority is the caller's effective admin
    binding or the human tenant/system-admin bypass — NOT the coarse token
    role — so this test uses a human tenant_admin (a genuinely authorized
    born-certifier). A token merely stamped role='admin' without an effective
    admin binding is now rejected; that elevation is guarded in
    test_kpi_cert_governance_bypass.py."""
    from src.auth.middleware import CurrentUser, get_current_user
    from src.main import app as _app

    admin_user = CurrentUser(
        user_id="admin@test.com", tenant_id="test-tenant",
        email="admin@test.com", role="tenant_admin",
    )
    _app.dependency_overrides[get_current_user] = lambda: admin_user

    db = make_mock_db()
    kpi = _kpi(certification_status="shared")

    async def mock_refresh(obj):
        for attr, val in vars(kpi).items():
            setattr(obj, attr, val)

    db.refresh = mock_refresh
    try:
        with patch("src.api.kpis.get_tenant_db", async_gen_from(db)):
            resp = await client.post(PREFIX, json={
                "name": "Revenue KPI",
                "certification_status": "shared",
                "owner_user_id": "admin@test.com",
            })
        assert resp.status_code == 201
    finally:
        _app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_update_kpi(client):
    kpi = _kpi()
    db = make_mock_db()
    db.get = AsyncMock(return_value=kpi)

    async def mock_refresh(obj):
        pass

    db.refresh = mock_refresh
    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(f"{PREFIX}/{kpi.id}", json={
            "display_name": "Updated KPI",
        })
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_delete_kpi(client):
    kpi = _kpi()
    db = make_mock_db()
    db.get = AsyncMock(return_value=kpi)
    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)):
        resp = await client.delete(f"{PREFIX}/{kpi.id}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_get_kpi_not_found(client):
    db = make_mock_db()
    db.get = AsyncMock(return_value=None)
    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)):
        resp = await client.get(f"{PREFIX}/{uuid.uuid4()}")
    assert resp.status_code == 404


# ---- Evaluate Tests ----

@pytest.mark.asyncio
async def test_evaluate_kpi_in_target(client):
    kpi = _kpi(expression='measure("Revenue")')
    kpi.target_type = "static"
    kpi.target_value = 900.0
    model = _model()
    revenue_measure = _measure(MEASURE_VALUE_ID, "Revenue")
    db = make_mock_db()

    async def side_get(cls, obj_id):
        if obj_id == kpi.id:
            return kpi
        if obj_id == TEST_MODEL_ID:
            return model
        return None

    db.get = AsyncMock(side_effect=side_get)

    async def mock_execute(model_id, query, bearer, timeout_s=30.0, **kwargs):
        return {"rows": [{"value": 1000.0}], "cells": [{"value": 1000.0}]}

    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)), \
         patch("src.api.kpis._execute_via_router", side_effect=mock_execute):
        resp = await client.post(f"{PREFIX}/{kpi.id}/evaluate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["value"] == 1000.0
    assert data["formatted_value"] is not None


@pytest.mark.asyncio
async def test_evaluate_kpi_off_target(client):
    kpi = _kpi(expression='measure("Revenue")')
    kpi.target_type = "static"
    kpi.target_value = 1000.0
    model = _model()
    db = make_mock_db()

    async def side_get(cls, obj_id):
        if obj_id == kpi.id:
            return kpi
        if obj_id == TEST_MODEL_ID:
            return model
        return None

    db.get = AsyncMock(side_effect=side_get)

    async def mock_execute(model_id, query, bearer, timeout_s=30.0, **kwargs):
        return {"rows": [{"value": 500.0}], "cells": [{"value": 500.0}]}

    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)), \
         patch("src.api.kpis._execute_via_router", side_effect=mock_execute):
        resp = await client.post(f"{PREFIX}/{kpi.id}/evaluate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == -1


@pytest.mark.asyncio
async def test_evaluate_variance_kpi_exposes_deviation_position_and_bands(client):
    """Bug-1226: a percentage_variance KPI without custom bands must score on
    its deviation preset AND expose status_position/status_bands that AGREE with
    the badge — the position lands in the band the status reports. This is the
    round-1 reproduction: cost KPI beating target reads GREEN, and the gauge
    needle (the deviation, not the percentage-of-target ratio) must land green.
    """
    kpi = _kpi(expression='measure("Revenue")')
    kpi.direction = "lower_is_better"
    kpi.target_type = "static"
    kpi.target_value = 2_800_000.0
    kpi.presentation_meta = {"evaluation_type": "percentage_variance"}
    model = _model()
    db = make_mock_db()

    async def side_get(cls, obj_id):
        if obj_id == kpi.id:
            return kpi
        if obj_id == TEST_MODEL_ID:
            return model
        return None

    db.get = AsyncMock(side_effect=side_get)

    async def mock_execute(model_id, query, bearer, timeout_s=30.0, **kwargs):
        # Cost 1.65% under budget → deviation 0.0165 → green.
        return {"rows": [{"value": 2_753_735.0}], "cells": [{"value": 2_753_735.0}]}

    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)), \
         patch("src.api.kpis._execute_via_router", side_effect=mock_execute):
        resp = await client.post(f"{PREFIX}/{kpi.id}/evaluate")
    assert resp.status_code == 200
    data = resp.json()
    # Badge GREEN.
    assert data["status"] == 1
    # Position is the DEVIATION (~0.0165), not the percentage-of-target 1.0165.
    assert data["status_position"] == pytest.approx(0.0165, abs=0.001)
    bands = data["status_bands"]
    assert bands is not None and len(bands) > 0
    # The exposed position lands in the band whose colour equals status_color —
    # needle, band colour and badge all agree.
    pos = data["status_position"]
    matched = None
    for b in bands:
        lo = b["min"]
        hi = b["max"]
        if (lo is None or pos >= lo) and (hi is None or pos < hi):
            matched = b
            break
    assert matched is not None
    assert matched["color"] == data["status_color"]


@pytest.mark.asyncio
async def test_evaluate_variance_kpi_missing_target_is_red_and_agrees(client):
    """Bug-1226: the missing-target counterpart — a cost KPI 30% over budget
    reads RED and the deviation needle lands in the red band.
    """
    kpi = _kpi(expression='measure("Revenue")')
    kpi.direction = "lower_is_better"
    kpi.target_type = "static"
    kpi.target_value = 2_800_000.0
    kpi.presentation_meta = {"evaluation_type": "percentage_variance"}
    model = _model()
    db = make_mock_db()

    async def side_get(cls, obj_id):
        if obj_id == kpi.id:
            return kpi
        if obj_id == TEST_MODEL_ID:
            return model
        return None

    db.get = AsyncMock(side_effect=side_get)

    async def mock_execute(model_id, query, bearer, timeout_s=30.0, **kwargs):
        return {"rows": [{"value": 3_640_000.0}], "cells": [{"value": 3_640_000.0}]}

    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)), \
         patch("src.api.kpis._execute_via_router", side_effect=mock_execute):
        resp = await client.post(f"{PREFIX}/{kpi.id}/evaluate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == -1
    pos = data["status_position"]
    bands = data["status_bands"]
    matched = None
    for b in bands:
        lo = b["min"]
        hi = b["max"]
        if (lo is None or pos >= lo) and (hi is None or pos < hi):
            matched = b
            break
    assert matched is not None
    assert matched["color"] == data["status_color"]


@pytest.mark.asyncio
async def test_evaluate_z_score_without_custom_bands_uses_statistical_basis(client):
    """Bug-6249: z_score must carry evaluation_type without custom bands.

    Known value: 120 against historical [95, 100, 105, 98, 102] is more than
    one standard deviation above the mean, so the default z_score preset marks
    it On Track and exposes a z-score position, not a percentage-of-target
    ratio.
    """
    kpi = _kpi(expression='measure("Revenue")')
    kpi.presentation_meta = {"evaluation_type": "z_score"}
    model = _model()
    revenue_measure = _measure(MEASURE_VALUE_ID, "Revenue")
    snapshots = _snapshot_rows([95, 100, 105, 98, 102])
    db = make_mock_db()

    async def side_get(cls, obj_id):
        if obj_id == kpi.id:
            return kpi
        if obj_id == TEST_MODEL_ID:
            return model
        return None

    execute_calls = 0

    async def side_execute(stmt):
        nonlocal execute_calls
        execute_calls += 1
        if execute_calls == 1:
            return _ScalarResult([revenue_measure])
        return _ScalarResult(snapshots)

    db.get = AsyncMock(side_effect=side_get)
    db.execute = AsyncMock(side_effect=side_execute)

    async def mock_execute(model_id, query, bearer, timeout_s=30.0, **kwargs):
        return {"rows": [{"value": 120.0}], "cells": [{"value": 120.0}]}

    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)), \
         patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=None), \
         patch("src.api.kpis._kpi_outer_cache_allowed", new_callable=AsyncMock, return_value=True), \
         patch("src.api.kpis._execute_via_router", side_effect=mock_execute):
        resp = await client.post(f"{PREFIX}/{kpi.id}/evaluate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == 1
    assert data["status_label"] == "On Track"
    assert data["status_position"] > 1.0
    assert data["status_bands"][0]["max"] == -1.0
    assert data["status_bands"][1]["max"] == 1.0


@pytest.mark.asyncio
async def test_evaluate_percentile_without_custom_bands_uses_percentile_basis(client):
    """Bug-6249: percentile_rank must carry evaluation_type without bands."""
    kpi = _kpi(expression='measure("Revenue")')
    kpi.presentation_meta = {
        "evaluation_type": "percentile_rank",
        "peer_dimension": "Region",
    }
    model = _model()
    revenue_measure = _measure(MEASURE_VALUE_ID, "Revenue")
    db = make_mock_db()

    async def side_get(cls, obj_id):
        if obj_id == kpi.id:
            return kpi
        if obj_id == TEST_MODEL_ID:
            return model
        return None

    execute_calls = 0

    async def side_execute(stmt):
        nonlocal execute_calls
        execute_calls += 1
        if execute_calls == 1:
            return _ScalarResult([revenue_measure])
        return _ScalarResult([])

    db.get = AsyncMock(side_effect=side_get)
    db.execute = AsyncMock(side_effect=side_execute)

    async def mock_execute(model_id, query, bearer, timeout_s=30.0, **kwargs):
        return {"rows": [{"value": 85.0}], "cells": [{"value": 85.0}]}

    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)), \
         patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=None), \
         patch("src.api.kpis._execute_via_router", side_effect=mock_execute), \
         patch(
             "src.api.kpis._resolve_peer_values",
             new_callable=AsyncMock,
             return_value=[50, 60, 70, 80, 90, 100],
         ):
        resp = await client.post(
            f"{PREFIX}/{kpi.id}/evaluate",
            headers={"Authorization": "Bearer test-token"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == 1
    assert data["status_label"] == "On Track"
    assert data["status_position"] == pytest.approx((4 / 6) * 100)
    assert data["status_bands"][0]["max"] == 25.0
    assert data["status_bands"][1]["max"] == 50.0


@pytest.mark.asyncio
async def test_python_fallback_custom_variance_bands_position_and_status_agree():
    """Bug-6250: fallback and primary path agree for custom non-default bands."""
    from src.api.kpis import _finalize_kpi_response
    from src.kpi_evaluator import (
        EvaluationContext,
        MeasureValueProvider,
        evaluate_kpi as run_evaluation_pipeline,
    )

    ctx = EvaluationContext(
        kpi_id=uuid.uuid4(),
        kpi_name="Cost KPI",
        expression='measure("Cost")',
        target_type="static",
        target_value=100.0,
        direction="lower_is_better",
        presentation_meta={
            "evaluation_type": "percentage_variance",
            "bands": [
                {"label": "On Track", "color": "#388E3C", "min": None, "max": 0.20},
                {"label": "Off Target", "color": "#D32F2F", "min": 0.20, "max": None},
            ],
        },
    )
    provider = MeasureValueProvider(
        get_measure_value=AsyncMock(return_value=85.0),
    )

    result = await run_evaluation_pipeline(ctx, provider)

    assert result.status == 1
    assert result.status_label == "On Track"
    assert result.status_position == pytest.approx(0.15)
    assert result.status_bands == [
        {"label": "On Track", "color": "#388E3C", "min": None, "max": 0.20},
        {"label": "Off Target", "color": "#D32F2F", "min": 0.20, "max": None},
    ]

    kpi = _kpi(kpi_id=ctx.kpi_id, expression=ctx.expression)
    kpi.target_type = ctx.target_type
    kpi.target_value = ctx.target_value
    kpi.direction = ctx.direction
    kpi.presentation_meta = ctx.presentation_meta
    db = make_mock_db()
    db.execute = AsyncMock(return_value=_ScalarResult([]))

    primary = await _finalize_kpi_response(
        kpi,
        ctx,
        db,
        value=85.0,
        target=100.0,
        model_id=TEST_MODEL_ID,
        model_slug="acme_sales",
        bearer=None,
        measure_map={},
    )

    assert (
        result.status,
        result.status_position,
        result.status_bands,
    ) == (
        primary.status,
        primary.status_position,
        primary.status_bands,
    )


@pytest.mark.asyncio
async def test_evaluate_kpi_not_found(client):
    db = make_mock_db()
    db.get = AsyncMock(return_value=None)
    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)):
        resp = await client.post(f"{PREFIX}/{uuid.uuid4()}/evaluate")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_evaluate_kpi_no_expression(client):
    kpi = _kpi(expression=None)
    model = _model()
    db = make_mock_db()

    async def side_get(cls, obj_id):
        if obj_id == kpi.id:
            return kpi
        if obj_id == TEST_MODEL_ID:
            return model
        return None

    db.get = AsyncMock(side_effect=side_get)

    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)):
        resp = await client.post(f"{PREFIX}/{kpi.id}/evaluate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["value"] is None
    assert data["status_label"] == "No expression configured"


class TestResolvePeerValuesAggregation:
    """Bug-1225: peer values must be computed with the measure's own aggregate
    (default_agg), not a hardcoded SUM. Ranking an AVG / COUNT_DISTINCT KPI
    against summed peers ranks against a different quantity — business-wrong.
    """

    @staticmethod
    async def _capture_sql(default_agg: str) -> str:
        from src.api import kpis as kpis_mod

        kpi = _kpi(expression='measure("AvgOrderValue")')
        meta = {"peer_dimension": "Region"}
        measure = _measure(name="AvgOrderValue", default_agg=default_agg)
        measure_map = {"AvgOrderValue": measure}
        captured: dict[str, str] = {}

        async def mock_execute(model_id, query, bearer, **kwargs):
            captured["sql"] = query
            return {"rows": [{"Region": "EU", "v": 10.0}]}

        with patch.object(kpis_mod, "_execute_via_router", side_effect=mock_execute):
            await kpis_mod._resolve_peer_values(
                kpi, meta, TEST_MODEL_ID, "acme_sales", "bearer", None,
                measure_map=measure_map,
            )
        return captured["sql"]

    @pytest.mark.asyncio
    async def test_avg_measure_uses_avg(self):
        sql = await self._capture_sql("avg")
        assert "AVG(" in sql.upper()
        assert "SUM(" not in sql.upper()

    @pytest.mark.asyncio
    async def test_count_distinct_measure_uses_count_distinct(self):
        sql = await self._capture_sql("count_distinct")
        assert "COUNT(DISTINCT" in sql.upper()
        assert "SUM(" not in sql.upper()

    @pytest.mark.asyncio
    async def test_sum_measure_still_uses_sum(self):
        sql = await self._capture_sql("sum")
        assert "SUM(" in sql.upper()

    @pytest.mark.asyncio
    async def test_missing_measure_map_defaults_to_sum(self):
        from src.api import kpis as kpis_mod

        kpi = _kpi(expression='measure("AvgOrderValue")')
        meta = {"peer_dimension": "Region"}
        captured: dict[str, str] = {}

        async def mock_execute(model_id, query, bearer, **kwargs):
            captured["sql"] = query
            return {"rows": [{"Region": "EU", "v": 10.0}]}

        with patch.object(kpis_mod, "_execute_via_router", side_effect=mock_execute):
            await kpis_mod._resolve_peer_values(
                kpi, meta, TEST_MODEL_ID, "acme_sales", "bearer", None,
                measure_map=None,
            )
        assert "SUM(" in captured["sql"].upper()


@pytest.mark.asyncio
async def test_evaluate_kpi_router_failure_returns_nulls(client):
    kpi = _kpi()
    model = _model()
    value_measure = _measure(MEASURE_VALUE_ID, "Revenue")
    goal_measure = _measure(MEASURE_GOAL_ID, "Revenue Target")
    db = make_mock_db()

    async def side_get(cls, obj_id):
        if obj_id == kpi.id:
            return kpi
        if obj_id == TEST_MODEL_ID:
            return model
        if obj_id == MEASURE_VALUE_ID:
            return value_measure
        if obj_id == MEASURE_GOAL_ID:
            return goal_measure
        return None

    db.get = AsyncMock(side_effect=side_get)

    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)), \
         patch("src.api.kpis._execute_via_router", AsyncMock(side_effect=ValueError("Router down"))):
        resp = await client.post(f"{PREFIX}/{kpi.id}/evaluate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["value"] is None
    assert data["goal"] is None


@pytest.mark.asyncio
async def test_evaluate_kpi_with_target(client):
    kpi = _kpi(expression='measure("Revenue")')
    kpi.target_type = "static"
    kpi.target_value = 1000.0
    model = _model()
    db = make_mock_db()

    async def side_get(cls, obj_id):
        if obj_id == kpi.id:
            return kpi
        if obj_id == TEST_MODEL_ID:
            return model
        return None

    db.get = AsyncMock(side_effect=side_get)

    async def mock_execute(model_id, query, bearer, timeout_s=30.0, **kwargs):
        return {"cells": [{"value": 850.0}]}

    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)), \
         patch("src.api.kpis._execute_via_router", side_effect=mock_execute):
        resp = await client.post(f"{PREFIX}/{kpi.id}/evaluate")
    data = resp.json()
    assert data["value"] == 850.0


@pytest.mark.asyncio
async def test_evaluate_kpi_uses_raw_token(override_auth):
    """Regression: cookie-auth users have no Authorization header; bearer must come from raw_token."""
    from src.auth.middleware import CurrentUser, get_current_user
    from src.main import app as test_app

    user = CurrentUser(
        user_id="cookie@example.com",
        tenant_id="test-tenant",
        email="cookie@example.com",
        raw_token="cookie-token-xyz",
    )
    test_app.dependency_overrides[get_current_user] = lambda: user

    kpi = _kpi()
    model = _model()
    value_measure = _measure(MEASURE_VALUE_ID, "Revenue")
    goal_measure = _measure(MEASURE_GOAL_ID, "Revenue Target")
    db = make_mock_db()

    async def side_get(cls, obj_id):
        if obj_id == kpi.id:
            return kpi
        if obj_id == TEST_MODEL_ID:
            return model
        if obj_id == MEASURE_VALUE_ID:
            return value_measure
        if obj_id == MEASURE_GOAL_ID:
            return goal_measure
        return None

    db.get = AsyncMock(side_effect=side_get)

    captured_bearers = []

    async def mock_execute(model_id, query, bearer, timeout_s=30.0, **kwargs):
        captured_bearers.append(bearer)
        return {"rows": [{"value": 100.0}], "cells": [{"value": 100.0}]}

    import httpx as httpx_mod
    async with httpx_mod.AsyncClient(
        transport=httpx_mod.ASGITransport(app=test_app), base_url="http://testserver"
    ) as ac:
        with patch("src.api.kpis.get_tenant_db", async_gen_from(db)), \
             patch("src.api.kpis._execute_via_router", side_effect=mock_execute):
            resp = await ac.post(f"{PREFIX}/{kpi.id}/evaluate")

    assert resp.status_code == 200
    assert len(captured_bearers) >= 1
    assert all(b == "cookie-token-xyz" for b in captured_bearers)

@pytest.mark.asyncio
async def test_evaluate_batch_prefetch_uses_effective_persona(client):
    kpi = _kpi(expression='measure("Revenue")')
    model = _model()
    revenue_measure = _measure(MEASURE_VALUE_ID, "Revenue")
    persona_id = uuid.uuid4()
    persona = types.SimpleNamespace(id=persona_id, included_measure_ids=[])
    db = make_mock_db()

    async def side_get(cls, obj_id):
        if obj_id == TEST_MODEL_ID:
            return model
        return None

    db.get = AsyncMock(side_effect=side_get)
    db.execute = AsyncMock(side_effect=[
        _ScalarResult([revenue_measure]),
        _ScalarResult([kpi]),
    ])

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=persona),
        patch("src.api.kpis._kpi_outer_cache_allowed", new_callable=AsyncMock, return_value=True),
        patch("src.api.kpis._batch_get_measure_values", new_callable=AsyncMock, return_value={}) as mock_batch_measures,
        patch(
            "src.api.kpis._evaluate_single_kpi",
            new_callable=AsyncMock,
            return_value=KPIEvaluateResponse(kpi_id=kpi.id, value=123.0),
        ),
        patch("src.api.kpis._upsert_kpi_latest_batch", new_callable=AsyncMock),
    ):
        resp = await client.post(
            f"{PREFIX}/evaluate-batch?persona_id={persona_id}",
            json={"kpi_ids": [str(kpi.id)]},
        )

    assert resp.status_code == 200
    mock_batch_measures.assert_awaited_once()
    assert mock_batch_measures.await_args.kwargs["persona_id"] == str(persona_id)



# ---------------------------------------------------------------------------
# Bug-6251: prior_period target resolution (F-017-04)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_target_prior_period_uses_ti_hook():
    """A prior_period target must resolve through the provider's time-intelligence
    hook (the decomposed query-router machinery) rather than returning None.

    Previously _resolve_target had ``target_type == "prior_period" -> return None``
    ("Deferred to Phase 3"), so a wizard prior_period target was accepted end to
    end but silently inert on the Python evaluation path."""
    from src.kpi_evaluator import (
        EvaluationContext,
        MeasureValueProvider,
        _resolve_target,
    )

    captured = {}

    async def _ti(node):
        # The prior_period(...) call node is dispatched here; return a known
        # prior-period value distinct from any current-period value.
        captured["name"] = getattr(node, "name", None)
        return 1234.0

    provider = MeasureValueProvider(evaluate_time_intelligence=_ti)
    ctx = EvaluationContext(
        kpi_id=uuid.uuid4(),
        kpi_name="Revenue KPI",
        target_type="prior_period",
        target_expression='prior_period(measure("Revenue"), "month")',
    )

    result = await _resolve_target(ctx, provider)
    assert result == 1234.0
    assert captured["name"] == "prior_period"


@pytest.mark.asyncio
async def test_resolve_target_prior_period_none_without_hook():
    """Without a TI hook the prior_period target evaluates to None — never a
    silently-wrong current-period value."""
    from src.kpi_evaluator import (
        EvaluationContext,
        MeasureValueProvider,
        _resolve_target,
    )

    provider = MeasureValueProvider()  # no evaluate_time_intelligence
    ctx = EvaluationContext(
        kpi_id=uuid.uuid4(),
        kpi_name="Revenue KPI",
        target_type="prior_period",
        target_expression='prior_period(measure("Revenue"), "month")',
    )
    assert await _resolve_target(ctx, provider) is None


def test_prior_period_target_expression_synthesizes_and_falls_back():
    """Bug-6251: for an API-created KPI with target_type='prior_period' but no
    stored target_expression, the synthesizer wraps the KPI's own expression and
    resolves the grain (target_period -> trend_period -> 'month'). Returns None
    when not a prior_period target or when there is no expression to wrap."""
    from src.api.kpis import _prior_period_target_expression

    k = types.SimpleNamespace(
        target_type="prior_period",
        expression='measure("Revenue")',
        target_period="quarter",
        trend_period="month",
    )
    assert _prior_period_target_expression(k) == 'prior_period(measure("Revenue"), "quarter")'

    # Grain falls back to trend_period when target_period is unset.
    k.target_period = None
    assert _prior_period_target_expression(k) == 'prior_period(measure("Revenue"), "month")'

    # An unknown/free-form grain is not interpolated verbatim — it falls back to
    # the safe default rather than emitting an unparseable DSL string.
    k.target_period = 'month") OR 1=1 --'
    assert _prior_period_target_expression(k) == 'prior_period(measure("Revenue"), "month")'

    # Not a prior_period target -> None (so the normal target paths apply).
    k.target_type = "static"
    assert _prior_period_target_expression(k) is None

    # No expression to wrap -> None.
    k.target_type = "prior_period"
    k.expression = None
    assert _prior_period_target_expression(k) is None


# ---------------------------------------------------------------------------
# Bug-7774: service token access to evaluate-batch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_service_token_with_kpi_scope_accepted_on_evaluate_batch(client):
    """Bug-7774: a service token carrying SCOPE_KPI_EVALUATE should be accepted
    by the evaluate-batch endpoint (used by the scheduler KPI-snapshot sweep).
    The Auth-RR-01 fix categorically rejects service tokens from human RBAC
    paths, so the endpoint needs a composite dependency that allows service
    tokens with the right scope through.
    """
    import types as _types
    from src.auth.middleware import CurrentServiceUser, get_current_user
    from shared.auth.service_principal import SCOPE_KPI_EVALUATE
    from src.main import app

    svc_user = CurrentServiceUser(
        principal="kpi-evaluator",
        tenant_id="test-tenant",
        role="kpi_evaluator",
        scopes=[SCOPE_KPI_EVALUATE],
    )
    # Mock the model DB lookup that _ensure_kpi_model_scope performs for
    # service tokens (it does its own get_tenant_db + db.get(Model, ...) call
    # instead of going through load_authorized_model).
    mock_model = _types.SimpleNamespace(
        id=TEST_MODEL_ID,
        project_id=TEST_PROJECT_ID,
        slug="mock-model",
    )
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=mock_model)
    mock_db.execute = AsyncMock(return_value=MagicMock(
        scalars=lambda: MagicMock(all=lambda: []),
        scalar_one_or_none=lambda: None,
        first=lambda: None,
    ))
    app.dependency_overrides[get_current_user] = lambda: svc_user
    with patch("src.api.kpis.get_tenant_db", async_gen_from(mock_db)):
        try:
            resp = await client.post(
                f"{PREFIX}/evaluate-batch",
                json={"kpi_ids": []},
            )
            # 200 (empty batch) or 404 (model not found) are acceptable — we're
            # testing that the service token is NOT rejected with 403 by the
            # RBAC guard.
            assert resp.status_code != 403, (
                f"Service token with KPI scope was rejected: {resp.status_code} {resp.text}"
            )
        finally:
            app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_service_token_without_kpi_scope_rejected_on_evaluate_batch(client):
    """Bug-7774: a service token WITHOUT SCOPE_KPI_EVALUATE must be rejected."""
    from src.auth.middleware import CurrentServiceUser, get_current_user
    from src.main import app

    svc_user = CurrentServiceUser(
        principal="some-other-service",
        tenant_id="test-tenant",
        role="some_role",
        scopes=["some.other.scope"],
    )
    app.dependency_overrides[get_current_user] = lambda: svc_user
    try:
        resp = await client.post(
            f"{PREFIX}/evaluate-batch",
            json={"kpi_ids": []},
        )
        assert resp.status_code == 403, (
            f"Service token without KPI scope should be rejected: {resp.status_code}"
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---- F-017-01 / F-013-04: deployed-snapshot serving authority ----

@pytest.mark.asyncio
async def test_evaluate_deployed_uses_snapshot_definition_not_live_edit(client):
    """A live target edit after deploy must NOT change the served status until a
    model deploy. The served target comes from the deployed snapshot (900), so a
    value of 1000 reads On Track even though the LIVE row was edited to 1200."""
    from shared.db.models import Model as ORMModel, ModelVersion as ORMVersion

    version_id = uuid.uuid4()
    kpi = _kpi(expression='measure("Revenue")')
    kpi.kpi_type = "simple_measure"
    kpi.target_type = "static"
    # LIVE (edited-after-deploy) target — must be IGNORED for serving.
    kpi.target_value = 1200.0
    model = _deployed_model(version_id)
    # DEPLOYED snapshot pins the ORIGINAL target of 900.
    snap = _snapshot_kpi_dict(kpi, target_type="static", target_value=900.0)
    version = _version(version_id, [snap])
    db = make_mock_db()

    async def side_get(cls, obj_id):
        if cls is ORMVersion and obj_id == version_id:
            return version
        if obj_id == kpi.id:
            return kpi
        if cls is ORMModel or obj_id == TEST_MODEL_ID:
            return model
        return None

    db.get = AsyncMock(side_effect=side_get)

    async def mock_execute(model_id, query, bearer, timeout_s=30.0, **kwargs):
        return {"rows": [{"value": 1000.0}], "cells": [{"value": 1000.0}]}

    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)), \
         patch("src.api.kpis._execute_via_router", side_effect=mock_execute):
        resp = await client.post(f"{PREFIX}/{kpi.id}/evaluate")
    assert resp.status_code == 200
    data = resp.json()
    # Served target is the DEPLOYED 900, not the live 1200.
    assert data["target"] == 900.0
    # 1000 vs 900 = 111% -> On Track (status 1), not off-target as it would be vs 1200.
    assert data["status"] == 1


@pytest.mark.asyncio
async def test_evaluate_deployed_snapshot_invalid_fails_closed(client):
    """A deployed model whose version snapshot is empty/malformed must fail
    closed (409 DEPLOYED_SNAPSHOT_INVALID), never fall back to the live draft."""
    from shared.db.models import Model as ORMModel, ModelVersion as ORMVersion

    version_id = uuid.uuid4()
    kpi = _kpi(expression='measure("Revenue")')
    model = _deployed_model(version_id)
    empty_version = types.SimpleNamespace(
        id=version_id, model_id=TEST_MODEL_ID, snapshot_json={"schema_version": "1.0"}
    )
    db = make_mock_db()

    async def side_get(cls, obj_id):
        if cls is ORMVersion and obj_id == version_id:
            return empty_version
        if obj_id == kpi.id:
            return kpi
        if cls is ORMModel or obj_id == TEST_MODEL_ID:
            return model
        return None

    db.get = AsyncMock(side_effect=side_get)

    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)):
        resp = await client.post(f"{PREFIX}/{kpi.id}/evaluate")
    assert resp.status_code == 409
    assert "DEPLOYED_SNAPSHOT_INVALID" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_evaluate_deployed_kpi_absent_from_snapshot_withheld(client):
    """A deployed model whose snapshot does NOT contain this KPI id means the KPI
    was never deployed through model Save + Deploy -> withheld (404)."""
    from shared.db.models import Model as ORMModel, ModelVersion as ORMVersion

    version_id = uuid.uuid4()
    kpi = _kpi(expression='measure("Revenue")')
    other = _kpi(expression='measure("Revenue")')
    model = _deployed_model(version_id)
    version = _version(version_id, [_snapshot_kpi_dict(other)])
    db = make_mock_db()

    async def side_get(cls, obj_id):
        if cls is ORMVersion and obj_id == version_id:
            return version
        if obj_id == kpi.id:
            return kpi
        if cls is ORMModel or obj_id == TEST_MODEL_ID:
            return model
        return None

    db.get = AsyncMock(side_effect=side_get)

    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)):
        resp = await client.post(f"{PREFIX}/{kpi.id}/evaluate")
    assert resp.status_code == 404
