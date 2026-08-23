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
@pytest.mark.parametrize(
    "reduction",
    [
        {"at_grain": "day"},
        {"non_additive_agg": "last"},
        {"carry_forward": True},
    ],
    ids=["at_grain", "non_additive_agg", "carry_forward"],
)
async def test_adhoc_trend_series_refuses_every_field_of_the_reduction(
    monkeypatch, reduction,
):
    """L7B-01 — the sparkline gate must know all THREE reduction fields.

    A semi-additive reduction cannot be applied per period in one grouped
    SELECT, so this builder returns no series rather than un-reduced per-period
    sums (Bug-8570). ``carry_forward`` was missing from that gate: since
    Bug-9482 routes a carry-forward-only KPI through the bucketed builder, the
    preview SCALAR is a per-bucket reduction while the sparkline beside it drew
    raw per-period sums — a balance of 90 under a series whose last point is
    310.

    Parameterised over the whole reduction vocabulary so adding a fourth field
    to it and forgetting this gate fails here.
    """
    # A router that SUCCEEDS with a drawable two-point series. Raising here
    # would be swallowed by the builder's best-effort ``except Exception ->
    # None`` and the test would pass against a missing gate.
    issued: list[str] = []

    async def fake_router(model_id, sql, bearer, **kw):
        issued.append(sql)
        return {
            "rows": [
                {"period": "2026-02-01", "value": 310},
                {"period": "2026-01-01", "value": 20},
            ]
        }

    monkeypatch.setattr(kpis_mod, "_execute_via_router", fake_router)

    series = await kpis_mod._build_adhoc_trend_series(
        'measure("Balance")',
        model_id="m1",
        model_slug="SalesModel",
        bearer="t",
        measure_map={"Balance": _measure("Balance")},
        ctx=_Ctx(),
        time_column="order_date",
        trend_period="month",
        filter_where_clause=None,
        n_periods=12,
        **reduction,
    )
    assert issued == [], (
        f"a reduced KPI ({reduction}) reached the gateway: {issued}. The "
        "grouped SELECT cannot apply the reduction, so the series it draws "
        "contradicts the scalar the same preview serves"
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
