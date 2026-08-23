"""Guards for residual L7 KPI debt Bugs 9478 / 9480 / 9481 / 9486."""
from __future__ import annotations

import inspect

import pytest

from src.kpi_compiler import CompilerContext, compile_expression


@pytest.mark.parametrize(
    "mode",
    [
        "automatic",
        "aggregate_first",
        "aggregate_of_aggregate",
        "semi_additive",
        "manual",
        "row_first",
        "pre_aggregated",
    ],
)
def test_bug9480_agg_of_agg_never_nests_sum_sum(mode: str):
    """Bug-9480: inner grain wrap must not double-aggregate the measure."""
    ctx = CompilerContext(
        model_slug="m",
        calc_agg_mode=mode,
        inner_agg="sum",
        outer_agg="avg",
        inner_grain="month",
        time_column="as_of_date",
    )
    sql = compile_expression('measure("Balance")', ctx).sql
    assert "SUM(SUM(" not in sql.upper().replace(" ", ""), sql
    assert 'SUM("Balance")' in sql or '"Balance"' in sql, sql


def test_bug9478_prior_period_exclusive_end_includes_anchor_day():
    """Bug-9478: default prior end must use CURRENT_DATE + INTERVAL '1 day'."""
    ctx = CompilerContext(
        model_slug="m",
        calc_agg_mode="row_first",
        ti_type="prior_period",
        ti_grain="month",
        base_expression='measure("Amount")',
        time_column="as_of_date",
    )
    sql = compile_expression('measure("Amount")', ctx).sql
    upper = sql.upper()
    assert "CURRENT_DATE + INTERVAL '1 DAY'" in upper, sql
    assert "- INTERVAL '1 MONTH'" in upper, sql


def test_bug9478_period_to_date_shares_exclusive_end():
    ctx = CompilerContext(
        model_slug="m",
        calc_agg_mode="row_first",
        ti_type="period_to_date",
        ti_grain="month",
        base_expression='measure("Amount")',
        time_column="as_of_date",
    )
    sql = compile_expression('measure("Amount")', ctx).sql
    assert "CURRENT_DATE + INTERVAL '1 DAY'" in sql.upper(), sql


def test_bug9481_carry_forward_share_refuses_at_compile():
    ctx = CompilerContext(
        model_slug="m",
        calc_agg_mode="row_first",
        carry_forward=True,
        share_type="share_of",
        share_dimension="Region",
        base_expression='measure("Balance")',
        time_column="as_of_date",
    )
    with pytest.raises(ValueError, match="carry_forward"):
        compile_expression('measure("Balance")', ctx)


def test_bug9481_carry_forward_cte_ti_refuses_at_compile():
    ctx = CompilerContext(
        model_slug="m",
        calc_agg_mode="row_first",
        carry_forward=True,
        ti_type="prior_period",
        ti_grain="month",
        base_expression='measure("Balance")',
        time_column="as_of_date",
    )
    with pytest.raises(ValueError, match="carry_forward"):
        compile_expression('measure("Balance")', ctx)


def test_bug9486_python_fallback_gate_includes_carry_forward():
    """Bug-9486: reduction gate must include carry_forward (source contract)."""
    from src.api import kpis as kpis_mod

    src = inspect.getsource(kpis_mod._evaluate_expression_via_sql)
    assert "carry_forward" in src
    assert "non_additive_agg or at_grain or carry_forward" in src.replace("\n", " ") or (
        "bool(non_additive_agg or at_grain or carry_forward)" in src
    )
