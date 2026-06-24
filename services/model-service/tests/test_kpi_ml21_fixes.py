"""ML21 (units 017/019) business-outcome tests for the KPI-extras fixes.

Covers F-017-26 (ad-hoc preview trend series). Pure-unit: the gateway call is
mocked so the test asserts the helper's behaviour (real points when a single
grouped query succeeds; honest None when not computable) without a live stack.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.api import kpis as kpis_mod


@dataclass
class _Ctx:
    calc_agg_mode: str = "automatic"


def _measure(name: str, agg: str = "sum"):
    @dataclass
    class _M:
        name: str
        default_agg: str
    return _M(name=name, default_agg=agg)


@pytest.mark.asyncio
async def test_adhoc_trend_series_builds_points_from_grouped_query(monkeypatch):
    """F-017-26: a single-measure expression with a time dimension returns a
    real per-period series, oldest-first, from one grouped gateway query."""
    captured = {}

    async def fake_router(model_id, sql, bearer, persona_id=None, **kw):
        captured["sql"] = sql
        # Gateway returns newest-first (LIMIT ... ORDER BY period DESC).
        return {
            "rows": [
                {"period": "2026-03-01", "value": 30},
                {"period": "2026-02-01", "value": 20},
                {"period": "2026-01-01", "value": 10},
            ]
        }

    monkeypatch.setattr(kpis_mod, "_execute_via_router", fake_router)

    series = await kpis_mod._build_adhoc_trend_series(
        'measure("Revenue")',
        model_id="m1",
        model_slug="SalesModel",
        bearer="t",
        measure_map={"Revenue": _measure("Revenue")},
        ctx=_Ctx(),
        time_column="order_date",
        trend_period="month",
        filter_where_clause=None,
        n_periods=12,
    )

    assert series is not None
    # Oldest-first for charting.
    assert [p.value for p in series] == [10, 20, 30]
    # One grouped query, canonical PG DATE_TRUNC, single transpile point downstream.
    assert "DATE_TRUNC('month'" in captured["sql"]
    assert "GROUP BY" in captured["sql"]


@pytest.mark.asyncio
async def test_adhoc_trend_series_none_without_time_dimension(monkeypatch):
    """F-017-26: no time dimension → honest None (no misleading sparkline)."""
    async def fake_router(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("router should not be called without a time column")

    monkeypatch.setattr(kpis_mod, "_execute_via_router", fake_router)

    series = await kpis_mod._build_adhoc_trend_series(
        'measure("Revenue")',
        model_id="m1",
        model_slug="SalesModel",
        bearer="t",
        measure_map={"Revenue": _measure("Revenue")},
        ctx=_Ctx(),
        time_column=None,
        trend_period="month",
        filter_where_clause=None,
        n_periods=12,
    )
    assert series is None


@pytest.mark.asyncio
async def test_adhoc_trend_series_none_for_time_intelligence_expression(monkeypatch):
    """F-017-26: a time-intelligence expression cannot be a single GROUP BY
    select, so the helper returns None rather than a wrong per-period number."""
    async def fake_router(*a, **k):  # pragma: no cover
        raise AssertionError("router should not be called for a TI expression")

    monkeypatch.setattr(kpis_mod, "_execute_via_router", fake_router)

    series = await kpis_mod._build_adhoc_trend_series(
        'pct_change(measure("Revenue"), "month")',
        model_id="m1",
        model_slug="SalesModel",
        bearer="t",
        measure_map={"Revenue": _measure("Revenue")},
        ctx=_Ctx(),
        time_column="order_date",
        trend_period="month",
        filter_where_clause=None,
        n_periods=12,
    )
    assert series is None


@pytest.mark.asyncio
async def test_adhoc_trend_series_none_on_single_point(monkeypatch):
    """F-017-26: a single returned period is not a sparkline → None."""
    async def fake_router(*a, **k):
        return {"rows": [{"period": "2026-03-01", "value": 30}]}

    monkeypatch.setattr(kpis_mod, "_execute_via_router", fake_router)

    series = await kpis_mod._build_adhoc_trend_series(
        'measure("Revenue")',
        model_id="m1",
        model_slug="SalesModel",
        bearer="t",
        measure_map={"Revenue": _measure("Revenue")},
        ctx=_Ctx(),
        time_column="order_date",
        trend_period="month",
        filter_where_clause=None,
        n_periods=12,
    )
    assert series is None
