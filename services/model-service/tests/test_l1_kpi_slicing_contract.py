"""Permanent L1 regressions for request-filtered KPI batch evaluation."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from shared.db.models import ModelVersion
from shared.schemas.pydantic_models import KPIEvaluateResponse

pytestmark = pytest.mark.unit


def _scalar_result(items):
    from tests.result_fakes import FakeScalarResult

    class Result:
        def scalars(self):
            return FakeScalarResult(items)

    return Result()


def test_bug9510_filtered_batch_slices_bypass_outer_cache():
    """Bug-9510: two request slices cannot collide in the model-wide cache."""
    from src.api.kpis import _batch_outer_cache_allowed_for_request

    eu = [{"dimension_id": "d-region", "operator": "eq", "value": "EU"}]
    us = [{"dimension_id": "d-region", "operator": "eq", "value": "US"}]
    assert not _batch_outer_cache_allowed_for_request(eu, None, True)
    assert not _batch_outer_cache_allowed_for_request(us, None, True)
    assert _batch_outer_cache_allowed_for_request(None, None, True)


@pytest.mark.asyncio
async def test_bug9510_9511_endpoint_filtered_batch_skips_cache_and_prefetch(client):
    """The real evaluate-batch route binds a filtered provider without cache use."""
    from src.api import kpis as kpis_module
    from tests.conftest import (
        NOW,
        TEST_MODEL_ID,
        TEST_PROJECT_ID,
        async_gen_from,
        make_mock_db,
    )

    kpi_id = uuid4()
    dimension_id = uuid4()
    measure = SimpleNamespace(
        id=uuid4(), model_id=TEST_MODEL_ID, name="Revenue", default_agg="sum"
    )
    kpi = SimpleNamespace(
        id=kpi_id,
        model_id=TEST_MODEL_ID,
        name="Revenue KPI",
        expression='measure("Revenue")',
        target_expression=None,
        kpi_type=None,
        calc_agg_mode="automatic",
        at_grain=None,
        non_additive_agg=None,
        carry_forward=False,
        inner_agg=None,
        inner_grain=None,
        outer_agg=None,
        target_type=None,
        target_value=None,
        target_measure_id=None,
        target_period=None,
        direction="higher_is_better",
        certification_status="certified",
        business_definition=None,
        presentation_meta=None,
        parent_kpi_id=None,
        weight=1.0,
        display_name="Revenue KPI",
        updated_at=NOW,
    )
    model = SimpleNamespace(
        id=TEST_MODEL_ID, slug="acme_sales", deployed_version_id=None,
        data_epoch=0, deploy_epoch=0,
    )
    dimension = SimpleNamespace(
        id=dimension_id, model_id=TEST_MODEL_ID, name="region",
        source_column_id=None,
    )
    db = make_mock_db()
    db.get = AsyncMock(return_value=model)
    db.execute = AsyncMock(side_effect=[
        _scalar_result([measure]),
        _scalar_result([kpi]),
        _scalar_result([dimension]),
        _scalar_result([]),
    ])
    cache = MagicMock()
    captured_filters = []

    async def evaluate_one(*args, **kwargs):
        captured_filters.append(kwargs.get("request_filter_predicates"))
        return KPIEvaluateResponse(kpi_id=kpi_id, value=42.0)

    with (
        patch.object(kpis_module, "get_tenant_db", async_gen_from(db)),
        patch.object(kpis_module, "resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        patch.object(kpis_module, "caller_has_role", new_callable=AsyncMock, return_value=True),
        patch.object(kpis_module, "_kpi_outer_cache_allowed", new_callable=AsyncMock, return_value=True),
        patch.object(kpis_module, "get_kpi_cache", return_value=cache),
        patch.object(kpis_module, "_batch_get_measure_values", new_callable=AsyncMock) as prefetch,
        patch.object(kpis_module, "_evaluate_single_kpi", side_effect=evaluate_one),
        patch.object(kpis_module, "_upsert_kpi_latest_batch", new_callable=AsyncMock),
        patch.object(kpis_module, "_dimension_data_types", new_callable=AsyncMock, return_value={}),
    ):
        response = await client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/kpis/evaluate-batch",
            json={
                "kpi_ids": [str(kpi_id)],
                "filters": [
                    {"dimension_id": str(dimension_id), "operator": "eq", "value": "EU"}
                ],
            },
        )

    assert response.status_code == 200
    assert captured_filters == [['"region" = \'EU\'']]
    prefetch.assert_not_awaited()
    cache.get.assert_not_called()
    cache.put.assert_not_called()


@pytest.mark.parametrize(
    ("invalid_filter", "expected_detail"),
    [
        ({"operator": "eq", "value": "EU"}, "missing dimension_id"),
        (
            {
                "dimension_id": "d-region",
                "mode": "parameter",
                "parameter_name": "region",
            },
            "has no value",
        ),
        (
            {
                "dimension_id": "d-region",
                "mode": "relative",
                "value": "last_century",
            },
            "unknown relative preset",
        ),
        (
            {
                "dimension_id": "d-region",
                "mode": "paramter",
                "value": "EU",
            },
            "unsupported filter mode",
        ),
        (
            {
                "dimension_id": "d-region",
                "operator": "not_supported",
                "value": "EU",
            },
            "cannot be compiled",
        ),
    ],
    ids=["missing-dimension", "parameter-no-value", "unknown-relative", "bad-mode", "bad-operator"],
)
@pytest.mark.asyncio
async def test_bug9512_invalid_request_filters_refuse_before_consumers(
    client, invalid_filter, expected_detail,
):
    """Bug-9512: every unrepresentable slicer fails before any execution."""
    from src.api import kpis as kpis_module
    from tests.conftest import (
        TEST_MODEL_ID,
        TEST_PROJECT_ID,
        async_gen_from,
        make_mock_db,
    )
    from tests.test_kpi_evaluate import _kpi, _measure, _model

    kpi = _kpi()
    measure = _measure()
    dimension_id = uuid4()
    dimension = SimpleNamespace(
        id=dimension_id, model_id=TEST_MODEL_ID, name="region",
        source_column_id=None,
    )
    db = make_mock_db()
    db.get = AsyncMock(return_value=_model())
    db.execute = AsyncMock(side_effect=[
        _scalar_result([measure]),
        _scalar_result([kpi]),
        _scalar_result([dimension]),
        _scalar_result([]),
    ])
    cache = MagicMock()
    request_filter = dict(invalid_filter)
    if request_filter.get("dimension_id") == "d-region":
        request_filter["dimension_id"] = str(dimension_id)

    with (
        patch.object(kpis_module, "get_tenant_db", async_gen_from(db)),
        patch.object(kpis_module, "resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        patch.object(kpis_module, "caller_has_role", new_callable=AsyncMock, return_value=True),
        patch.object(kpis_module, "_kpi_outer_cache_allowed", new_callable=AsyncMock, return_value=True),
        patch.object(kpis_module, "get_kpi_cache", return_value=cache),
        patch.object(kpis_module, "_batch_get_measure_values", new_callable=AsyncMock) as prefetch,
        patch.object(kpis_module, "_evaluate_single_kpi", new_callable=AsyncMock) as evaluate_one,
        patch.object(kpis_module, "_upsert_kpi_latest_batch", new_callable=AsyncMock) as publish,
        patch.object(kpis_module, "_dimension_data_types", new_callable=AsyncMock, return_value={}),
    ):
        response = await client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/kpis/evaluate-batch",
            json={"kpi_ids": [str(kpi.id)], "filters": [request_filter]},
        )

    assert response.status_code == 400
    assert expected_detail in response.text
    prefetch.assert_not_awaited()
    evaluate_one.assert_not_awaited()
    publish.assert_not_awaited()
    cache.get.assert_not_called()
    cache.put.assert_not_called()


@pytest.mark.asyncio
async def test_bug9511_filtered_batch_prefetch_bypasses_model_wide_values():
    """Bug-9511: Python fallback asks the router for the bound slice."""
    from src.api.kpis import (
        _batch_prefetch_allowed_for_request,
        _build_measure_provider,
    )

    filters = [{"dimension_id": "d-region", "operator": "eq", "value": "EU"}]
    assert not _batch_prefetch_allowed_for_request(filters)

    measure = SimpleNamespace(default_agg="sum")
    with patch(
        "src.api.kpis._get_measure_value",
        new=AsyncMock(return_value=42.0),
    ) as get_value:
        provider = _build_measure_provider(
            uuid4(), "payments", "token", {"Revenue": measure},
            measure_value_cache=None,
            where_clause='"region" = \'EU\'',
        )
        assert await provider.get_measure_value("Revenue") == 42.0

    get_value.assert_awaited_once()
    assert get_value.await_args.kwargs["where_clause"] == '"region" = \'EU\''


@pytest.mark.asyncio
async def test_bug9511_filtered_batch_slices_kpi_referenced_target(client):
    """Bug-9511: real batch routing slices a referenced target KPI too."""
    from src.api import kpis as kpis_module
    from src.api.kpis import _COMPILER_UNSUPPORTED
    from tests.conftest import (
        TEST_MODEL_ID,
        TEST_PROJECT_ID,
        async_gen_from,
        make_mock_db,
    )
    from tests.result_fakes import FakeScalarResult
    from tests.test_kpi_evaluate import _kpi, _measure, _model

    parent = _kpi(name="Revenue", expression='measure("Revenue")')
    parent.target_expression = 'kpi("Regional Target")'
    referenced = _kpi(name="Regional Target", expression='measure("Budget")')
    model = _model()
    model.deployed_version_id = None
    measure = _measure(name="Revenue")
    budget = _measure(name="Budget")
    dimension_id = uuid4()
    dimension = SimpleNamespace(
        id=dimension_id, model_id=TEST_MODEL_ID, name="region",
        source_column_id=None,
    )
    sql_calls = []
    provider_calls = []

    db = make_mock_db()
    db.get = AsyncMock(return_value=model)
    db.execute = AsyncMock(side_effect=[
        _scalar_result([measure, budget]),
        _scalar_result([parent]),
        _scalar_result([referenced]),
        _scalar_result([dimension]),
        _scalar_result([]),
        _scalar_result([referenced]),
        _scalar_result([]),
    ])
    cache = MagicMock()

    async def fake_sql(expression, *_args, **kwargs):
        sql_calls.append((expression, kwargs.get("where_clause")))
        if expression == parent.expression:
            return 100.0
        return _COMPILER_UNSUPPORTED

    async def fake_measure(model_id, name, bearer, model_slug, agg, **kwargs):
        provider_calls.append((name, kwargs.get("where_clause")))
        return 60.0

    with (
        patch.object(kpis_module, "get_tenant_db", async_gen_from(db)),
        patch.object(kpis_module, "resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        patch.object(kpis_module, "caller_has_role", new_callable=AsyncMock, return_value=True),
        patch.object(kpis_module, "_kpi_outer_cache_allowed", new_callable=AsyncMock, return_value=True),
        patch.object(kpis_module, "get_kpi_cache", return_value=cache),
        patch.object(kpis_module, "_batch_get_measure_values", new_callable=AsyncMock) as prefetch,
        patch.object(kpis_module, "_upsert_kpi_latest_batch", new_callable=AsyncMock) as publish,
        patch.object(kpis_module, "_dimension_data_types", new_callable=AsyncMock, return_value={}),
        patch.object(kpis_module, "_evaluate_expression_via_sql", side_effect=fake_sql),
        patch.object(kpis_module, "_get_measure_value", side_effect=fake_measure),
    ):
        response = await client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/kpis/evaluate-batch",
            json={
                "kpi_ids": [str(parent.id)],
                "filters": [
                    {"dimension_id": str(dimension_id), "operator": "eq", "value": "EU"}
                ],
            },
        )

    predicate = '"region" = \'EU\''
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["value"] == 100.0
    assert result["target"] == 60.0
    assert result["status"] == 1
    assert (parent.expression, predicate) in sql_calls
    assert ("Budget", predicate) in provider_calls
    assert all(where == predicate for _name, where in provider_calls)
    prefetch.assert_not_awaited()
    publish.assert_not_awaited()
    cache.get.assert_not_called()
    cache.put.assert_not_called()


@pytest.mark.asyncio
async def test_bug9512_filter_metadata_comes_from_deployed_snapshot():
    """Bug-9512: a live draft rename cannot change a deployed filter."""
    from src.kpi_business_builder import _compile_filters
    from src.kpi_deploy_resolver import resolve_served_filter_metadata

    model_id = uuid4()
    version_id = uuid4()
    snapshot_dimension_id = uuid4()
    live_dimension_name = "draft_region"
    deployed_dimension_name = "deployed_region"

    class FakeDb:
        async def get(self, model_version_cls, object_id):
            assert model_version_cls is ModelVersion
            assert object_id == version_id
            return SimpleNamespace(
                model_id=model_id,
                snapshot_json={
                    "kpis": [],
                    "tables": [{"id": "table-payments"}],
                    "dimensions": [
                        {
                            "id": str(snapshot_dimension_id),
                            "name": deployed_dimension_name,
                            "source_column_id": "column-region",
                        }
                    ],
                    "columns": [
                        {"id": "column-region", "data_type": "text"}
                    ],
                },
            )

    model = SimpleNamespace(id=model_id, deployed_version_id=version_id)
    metadata = await resolve_served_filter_metadata(FakeDb(), model)
    assert metadata == (
        {str(snapshot_dimension_id): deployed_dimension_name},
        {str(snapshot_dimension_id): "text"},
    )
    predicates = _compile_filters(
        [{"dimension_id": str(snapshot_dimension_id), "operator": "eq", "value": "EU"}],
        metadata[0],
        None,
        metadata[1],
        strict=True,
    )
    assert predicates == ['"deployed_region" = \'EU\'']
    assert live_dimension_name not in predicates[0]


def test_bug9512_unknown_or_uncompilable_filter_refuses_loudly():
    """Bug-9512: unknown ids and invalid operators never degrade to unsliced."""
    from src.kpi_business_builder import _compile_filters

    with pytest.raises(ValueError, match="unknown dimension_id"):
        _compile_filters(
            [{"dimension_id": "not-deployed", "operator": "eq", "value": "EU"}],
            {"deployed-id": "region"},
            None,
            {},
            strict=True,
        )
    with pytest.raises(ValueError, match="cannot be compiled"):
        _compile_filters(
            [{"dimension_id": "deployed-id", "operator": "not-supported", "value": "EU"}],
            {"deployed-id": "region"},
            None,
            {},
            strict=True,
        )
