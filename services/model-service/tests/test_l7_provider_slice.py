"""Bug-8682 — the KPI measure provider's fallback must answer the SAME question.

The batch evaluate path pre-fetches all measures once, model-wide. When a KPI's
SQL compilation returns ``_COMPILER_UNSUPPORTED`` the per-KPI provider falls
back to individual ``_get_measure_value`` calls — and those were built WITHOUT
the KPI's business-definition WHERE, so they issued a bare
``SELECT SUM("X") FROM "<model>"``. An EMEA KPI was then answered with the
all-regions number, at HTTP 200, in a cell that looks legitimate.

Root cause is structural: all FIVE ``_evaluate_single_kpi`` call sites build the
provider BEFORE the per-KPI scope exists, so no call site could pass it. The fix
binds the slice inside ``_evaluate_single_kpi`` — the one place that knows it —
and ``_build_measure_provider``'s closure reads it at CALL time.

Test escape: coverage exercised the provider's CACHE HIT path, never the
on-miss fallback with a filtered KPI. Guard: this module. Tier: T2.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.api import kpis as kpis_mod

pytestmark = pytest.mark.unit


class _ScalarResult:
    def all(self):
        return []

    def first(self):
        return None


class _Result:
    def scalars(self):
        return _ScalarResult()

    def all(self):
        return []

    def first(self):
        return None


class _Db:
    def __init__(self, by_id: dict):
        self._by_id = by_id

    async def get(self, _model, pk):
        return self._by_id.get(pk)

    async def execute(self, *_a, **_k):
        return _Result()


class TestProviderFallbackHonoursTheLateBoundSlice:
    @pytest.mark.asyncio
    async def test_on_miss_lookup_uses_the_slice_set_after_construction(self):
        captured: list[str] = []

        async def _fake_router(_model_id, sql, _bearer, **_kw):
            captured.append(sql)
            return {"rows": [{"value": 42.0}]}

        provider = kpis_mod._build_measure_provider(
            uuid.uuid4(), "modelx", "tok",
            {"revenue": SimpleNamespace(name="revenue", default_agg="sum")},
        )
        # The caller could not know this at construction time.
        provider.measure_where_clause = "\"region\" = 'EMEA'"

        with patch.object(kpis_mod, "_execute_via_router", _fake_router):
            value = await provider.get_measure_value("revenue")

        assert value == 42.0
        assert captured, "the fallback must reach the router"
        assert "region" in captured[0] and "EMEA" in captured[0], (
            "the on-miss measure lookup was UNFILTERED — an EMEA KPI would be "
            f"answered with the all-regions number (Bug-8682): {captured[0]}"
        )

    @pytest.mark.asyncio
    async def test_a_constructor_supplied_slice_still_works(self):
        """Callers that already know the slice keep passing it."""
        captured: list[str] = []

        async def _fake_router(_model_id, sql, _bearer, **_kw):
            captured.append(sql)
            return {"rows": [{"value": 1.0}]}

        provider = kpis_mod._build_measure_provider(
            uuid.uuid4(), "modelx", "tok",
            {"revenue": SimpleNamespace(name="revenue", default_agg="sum")},
            where_clause="\"region\" = 'APAC'",
        )
        with patch.object(kpis_mod, "_execute_via_router", _fake_router):
            await provider.get_measure_value("revenue")

        assert "APAC" in captured[0]


class TestEvaluateSingleKpiBindsTheSlice:
    @pytest.mark.asyncio
    async def test_the_business_definition_where_reaches_the_provider(self):
        """Production path: the caller builds an unsliced provider and
        ``_evaluate_single_kpi`` binds the KPI's own scope onto it."""
        from src.kpi_evaluator import MeasureValueProvider

        model_id = uuid.uuid4()
        time_dim_id = uuid.uuid4()
        kpi = SimpleNamespace(
            id=uuid.uuid4(),
            name="EMEA revenue",
            display_name=None,
            expression='measure("revenue")',
            kpi_type="single_measure",
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
            time_dimension_id=time_dim_id,
            business_definition={
                "_compiled": {
                    "where_clause": "\"region\" = 'EMEA'",
                    "filter_predicates": ["\"region\" = 'EMEA'"],
                    "time_window_predicates": [],
                },
            },
        )
        db = _Db({
            time_dim_id: SimpleNamespace(
                id=time_dim_id, model_id=model_id, is_time_dim=True,
                name="as_of_date",
            ),
        })
        provider = MeasureValueProvider()

        async def _fake_router(_model_id, _sql, _bearer, **_kw):
            return {"rows": [{"value": 1.0}]}

        with patch.object(kpis_mod, "_execute_via_router", _fake_router):
            await kpis_mod._evaluate_single_kpi(
                kpi, db, model_id, "modelx", "tok", provider,
                measure_map={
                    "revenue": SimpleNamespace(name="revenue", default_agg="sum"),
                },
            )

        assert provider.measure_where_clause == "\"region\" = 'EMEA'", (
            "the per-KPI slice was never bound onto the provider, so any "
            "Python-fallback measure lookup answers the unfiltered question "
            "(Bug-8682)"
        )

    @pytest.mark.asyncio
    async def test_request_level_filters_are_included_in_the_bound_slice(self):
        """The slice must include the Bug-5252 request filters, not just the
        stored business definition — otherwise the fallback and the SQL path
        disagree about the request's own narrowing."""
        from src.kpi_evaluator import MeasureValueProvider

        model_id = uuid.uuid4()
        kpi = SimpleNamespace(
            id=uuid.uuid4(),
            name="Revenue",
            display_name=None,
            expression='measure("revenue")',
            kpi_type="single_measure",
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
            time_dimension_id=None,
            business_definition={
                "_compiled": {
                    "where_clause": None,
                    "filter_predicates": [],
                    "time_window_predicates": [],
                },
            },
        )
        provider = MeasureValueProvider()

        async def _fake_router(_model_id, _sql, _bearer, **_kw):
            return {"rows": [{"value": 1.0}]}

        with patch.object(kpis_mod, "_execute_via_router", _fake_router):
            await kpis_mod._evaluate_single_kpi(
                kpi, _Db({}), model_id, "modelx", "tok", provider,
                measure_map={
                    "revenue": SimpleNamespace(name="revenue", default_agg="sum"),
                },
                request_filter_predicates=["\"country\" = 'FR'"],
            )

        assert provider.measure_where_clause == "\"country\" = 'FR'"
