"""Tests for KPI CRUD and evaluate endpoint."""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.schemas.pydantic_models import KPIEvaluateResponse

from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    make_mock_db,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/kpis"

MEASURE_VALUE_ID = uuid.uuid4()
MEASURE_GOAL_ID = uuid.uuid4()


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return list(self._items)


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
    db = make_mock_db()
    kpi = _kpi(certification_status="shared")

    async def mock_refresh(obj):
        for attr, val in vars(kpi).items():
            setattr(obj, attr, val)

    db.refresh = mock_refresh
    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)):
        resp = await client.post(PREFIX, json={
            "name": "Revenue KPI",
            "certification_status": "shared",
            "owner_user_id": "admin@test.com",
        })
    assert resp.status_code == 201


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

