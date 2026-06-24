"""Bug-3657 — KPI member-function recognition and resolution.

Covers token detection (dax_parser.find_kpi_member_functions) and property
resolution to executable MDX scalar expressions (mdx_execute.resolve_kpi_property_expr).
"""
from __future__ import annotations

import pytest

from src.dax.dax_parser import find_kpi_member_functions, KPI_MEMBER_FUNCTIONS
from src.dax.mdx_execute import resolve_kpi_property_expr
from src.dax.xmla_server import _compute_kpi_status


_MEASURES = [
    {"id": "m-fee", "name": "fee_amount", "default_agg": "sum"},
    {"id": "m-base", "name": "base_amount", "default_agg": "sum"},
]

# KPI "aa": simple_measure, value=fee_amount, static goal 10000, higher is better.
_KPI_AA = {
    "name": "aa",
    "display_name": "aa",
    "value_measure_id": "m-fee",
    "target_type": "static",
    "target_value": 10000.0,
    "direction": "higher_is_better",
    "status_expression": None,
    "trend_expression": None,
}


# ---------------------------------------------------------------------------
# Token recognition
# ---------------------------------------------------------------------------

def test_find_all_four_functions():
    for fn in KPI_MEMBER_FUNCTIONS:
        stmt = f'SELECT FROM [modely] WHERE ({fn}("aa"))'
        found = find_kpi_member_functions(stmt)
        assert len(found) == 1
        _, name, caption = found[0]
        assert name == fn
        assert caption == "aa"


def test_find_none_when_absent():
    assert find_kpi_member_functions(
        "SELECT {[Measures].[fee_amount]} ON COLUMNS FROM [modely]"
    ) == []


def test_find_case_insensitive_and_single_quotes():
    found = find_kpi_member_functions("WHERE (kpistatus('aa'))")
    assert len(found) == 1
    assert found[0][1] == "KPIStatus"
    assert found[0][2] == "aa"


def test_find_multiple_in_order():
    stmt = '{KPIValue("aa"), KPIGoal("aa")} ON COLUMNS'
    found = find_kpi_member_functions(stmt)
    assert [f[1] for f in found] == ["KPIValue", "KPIGoal"]


# ---------------------------------------------------------------------------
# Property resolution
# ---------------------------------------------------------------------------

def test_resolve_value_to_measure_ref():
    assert resolve_kpi_property_expr(_KPI_AA, "KPIValue", _MEASURES) == \
        "[Measures].[fee_amount]"


def test_resolve_goal_static_literal():
    assert resolve_kpi_property_expr(_KPI_AA, "KPIGoal", _MEASURES) == "10000.0"


def test_resolve_status_higher_is_better_case():
    expr = resolve_kpi_property_expr(_KPI_AA, "KPIStatus", _MEASURES)
    assert expr is not None
    assert "[Measures].[fee_amount]" in expr
    assert ">= 10000.0" in expr
    assert expr.strip().upper().startswith("CASE")


def test_resolve_status_lower_is_better():
    kpi = dict(_KPI_AA, direction="lower_is_better")
    expr = resolve_kpi_property_expr(kpi, "KPIStatus", _MEASURES)
    assert "<= 10000.0" in expr


def test_resolve_trend_empty_returns_none():
    assert resolve_kpi_property_expr(_KPI_AA, "KPITrend", _MEASURES) is None


def test_resolve_legacy_status_expression_preferred():
    kpi = dict(_KPI_AA, status_expression="CASE WHEN 1=1 THEN 1 ELSE -1 END")
    assert resolve_kpi_property_expr(kpi, "KPIStatus", _MEASURES) == \
        "CASE WHEN 1=1 THEN 1 ELSE -1 END"


# ---------------------------------------------------------------------------
# Live status path (_compute_kpi_status) — F-P4b1-02
#
# The resolver tests above prove the STRING resolver prefers status_expression.
# These assert the LIVE numeric evaluator shares that precedence, closing the
# round-1 masking gap where _compute_kpi_status ignored status_expression and
# always re-derived the default band.
# ---------------------------------------------------------------------------

async def _const_cell(value):
    async def _cell(_name):
        return value
    return _cell


@pytest.mark.asyncio
async def test_compute_status_direction_fallback_null_expression():
    """Null status_expression → status derived from value vs goal (KPI aa case:
    value 2753735.21 >= goal 10000, higher_is_better → 1)."""
    measure_cell = await _const_cell(2753735.21)
    status = await _compute_kpi_status(_KPI_AA, "fee_amount", measure_cell, 10000.0)
    assert status == 1


@pytest.mark.asyncio
async def test_compute_status_lower_is_better_fallback():
    kpi = dict(_KPI_AA, direction="lower_is_better")
    measure_cell = await _const_cell(50.0)  # 50 <= goal 100 → 1
    assert await _compute_kpi_status(kpi, "fee_amount", measure_cell, 100.0) == 1
    measure_cell = await _const_cell(200.0)  # 200 > goal*1.1 → -1
    assert await _compute_kpi_status(kpi, "fee_amount", measure_cell, 100.0) == -1


@pytest.mark.asyncio
async def test_compute_status_honours_literal_status_expression():
    """A published numeric-literal status_expression is honoured directly,
    overriding the value-vs-goal band (precedence matches the resolver)."""
    kpi = dict(_KPI_AA, status_expression="-1")
    # value 2753735.21 >> goal 10000 would yield 1 from the default band;
    # the published status_expression -1 must take precedence.
    measure_cell = await _const_cell(2753735.21)
    assert await _compute_kpi_status(kpi, "fee_amount", measure_cell, 10000.0) == -1


@pytest.mark.asyncio
async def test_compute_status_fails_loud_on_nonliteral_expression():
    """An arbitrary CASE status_expression cannot be evaluated by the numeric
    live path — it FAILS LOUD rather than silently returning the default band."""
    kpi = dict(_KPI_AA, status_expression="CASE WHEN 1=1 THEN 1 ELSE -1 END")
    measure_cell = await _const_cell(2753735.21)
    with pytest.raises(ValueError, match="status_expression"):
        await _compute_kpi_status(kpi, "fee_amount", measure_cell, 10000.0)
