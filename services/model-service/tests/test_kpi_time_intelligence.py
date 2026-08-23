"""Integration tests for KPI time intelligence (Phase 13.b).

Tests the full pipeline: compiler -> SQL emission -> evaluator fallback.
"""
from __future__ import annotations

from datetime import date

import pytest

from shared.semantic.calendar_utils import (
    DateRange,
    period_to_date_range,
    prior_period_date,
    rolling_window_dates,
)
from shared.semantic.time_variants_sql import (
    VariantBinding,
    VariantSql,
    VariantSqlError,
    emit_variant_expression,
)
from src.kpi_compiler import CompilerContext, CompiledQuery, compile_expression

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Calendar utils
# ---------------------------------------------------------------------------


class TestPriorPeriodDate:
    def test_prior_year(self):
        assert prior_period_date(date(2024, 6, 15), "year") == date(2023, 6, 15)

    def test_prior_quarter(self):
        assert prior_period_date(date(2024, 6, 15), "quarter") == date(2024, 3, 15)

    def test_prior_month(self):
        assert prior_period_date(date(2024, 6, 15), "month") == date(2024, 5, 15)

    def test_prior_week(self):
        assert prior_period_date(date(2024, 6, 15), "week") == date(2024, 6, 8)

    def test_prior_year_leap_day(self):
        result = prior_period_date(date(2024, 2, 29), "year")
        assert result == date(2023, 2, 28)

    def test_prior_month_end_clamping(self):
        result = prior_period_date(date(2024, 3, 31), "month")
        assert result == date(2024, 2, 29)

    def test_unknown_grain_returns_same(self):
        assert prior_period_date(date(2024, 6, 15), "day") == date(2024, 6, 15)


class TestPeriodToDateRange:
    def test_ytd(self):
        r = period_to_date_range(date(2024, 6, 15), "year")
        assert r == DateRange(start=date(2024, 1, 1), end=date(2024, 6, 15))

    def test_ytd_fiscal_april(self):
        r = period_to_date_range(date(2024, 6, 15), "year", fiscal_year_start_month=4)
        assert r == DateRange(start=date(2024, 4, 1), end=date(2024, 6, 15))

    def test_ytd_fiscal_before_start(self):
        r = period_to_date_range(date(2024, 2, 15), "year", fiscal_year_start_month=4)
        assert r == DateRange(start=date(2023, 4, 1), end=date(2024, 2, 15))

    def test_qtd(self):
        r = period_to_date_range(date(2024, 5, 20), "quarter")
        assert r == DateRange(start=date(2024, 4, 1), end=date(2024, 5, 20))

    def test_mtd(self):
        r = period_to_date_range(date(2024, 6, 15), "month")
        assert r == DateRange(start=date(2024, 6, 1), end=date(2024, 6, 15))

    def test_wtd(self):
        # 2024-06-15 is a Saturday, ISO week starts Monday
        r = period_to_date_range(date(2024, 6, 15), "week")
        assert r.start == date(2024, 6, 10)  # Monday
        assert r.end == date(2024, 6, 15)


class TestRollingWindowDates:
    def test_monthly_window_3(self):
        dates = rolling_window_dates(date(2024, 6, 15), 3, "month")
        assert len(dates) == 3
        assert dates[0] == date(2024, 4, 15)
        assert dates[1] == date(2024, 5, 15)
        assert dates[2] == date(2024, 6, 15)

    def test_weekly_window_4(self):
        dates = rolling_window_dates(date(2024, 6, 15), 4, "week")
        assert len(dates) == 4
        assert dates[3] == date(2024, 6, 15)
        assert dates[0] == date(2024, 5, 25)

    def test_yearly_window_2(self):
        dates = rolling_window_dates(date(2024, 6, 15), 2, "year")
        assert dates == [date(2023, 6, 15), date(2024, 6, 15)]

    def test_single_period(self):
        dates = rolling_window_dates(date(2024, 6, 15), 1, "month")
        assert dates == [date(2024, 6, 15)]

    def test_month_end_clamping(self):
        dates = rolling_window_dates(date(2024, 3, 31), 3, "month")
        assert dates[0] == date(2024, 1, 31)
        assert dates[1] == date(2024, 2, 29)  # leap year
        assert dates[2] == date(2024, 3, 31)


# ---------------------------------------------------------------------------
# Compiler + variant handler integration
# ---------------------------------------------------------------------------


class TestCompilerVariantIntegration:
    """Test that the compiler produces SQL via time_variants_sql handlers."""

    def test_prior_period_month_produces_lag(self):
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression('prior_period(measure("Revenue"), "month")', ctx)
        assert result.has_time_intelligence is True
        assert "LAG" in result.select_expr
        assert result.measure_names == ["Revenue"]

    def test_prior_period_year_produces_lag(self):
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression('prior_period(measure("Revenue"), "year")', ctx)
        assert "LAG" in result.select_expr

    def test_period_to_date_ytd_produces_sum_window(self):
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression('period_to_date(measure("Revenue"), "year")', ctx)
        assert "SUM" in result.select_expr
        assert "UNBOUNDED PRECEDING" in result.select_expr

    def test_moving_avg_produces_avg_window(self):
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression(
            'moving_avg(measure("Revenue"), literal(3), "month")', ctx,
        )
        assert "AVG" in result.select_expr
        assert "ROWS BETWEEN" in result.select_expr

    def test_trailing_sum_produces_sum_window(self):
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression(
            'trailing_sum(measure("Revenue"), literal(6), "month")', ctx,
        )
        assert "SUM" in result.select_expr
        assert "ROWS BETWEEN" in result.select_expr

    def test_lead_produces_lead_function(self):
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression(
            'lead(measure("Revenue"), literal(1), "month")', ctx,
        )
        assert "LEAD" in result.select_expr

    def test_cagr_produces_power(self):
        # Bug-6645: CAGR requires month or coarser grain. Supply "year"
        # via the third argument so the SQL emitter can produce the POWER
        # expression. Without a grain, day is assumed and rejected.
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression(
            'cagr(measure("Revenue"), literal(3), "year")', ctx
        )
        assert "POWER" in result.select_expr

    def test_pct_change_produces_lag_nullif(self):
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression('pct_change(measure("Revenue"), "month")', ctx)
        assert "LAG" in result.select_expr
        assert "NULLIF" in result.select_expr


class TestCompilerKpiRefFallback:
    """Test that kpi() refs inside time functions fall back correctly."""

    def test_kpi_inside_prior_period_returns_null(self):
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression('prior_period(kpi("Child KPI"), "month")', ctx)
        assert "NULL" in result.select_expr
        assert result.kpi_names == ["Child KPI"]
        assert result.has_time_intelligence is True

    def test_kpi_inside_moving_avg_returns_null(self):
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression(
            'moving_avg(kpi("Revenue KPI"), literal(3), "month")', ctx,
        )
        assert "NULL" in result.select_expr
        assert result.kpi_names == ["Revenue KPI"]


# ---------------------------------------------------------------------------
# Variant SQL handlers (direct invocation)
# ---------------------------------------------------------------------------


class TestVariantHandlers:
    """Test time variant handlers produce valid SQL."""

    def test_lead_handler(self):
        b = VariantBinding(
            base_expression='SUM("Revenue")',
            fact_date_column='"order_date"',
        )
        result = emit_variant_expression("lead", b)
        assert "LEAD" in result.sql
        assert '"order_date"' in result.sql

    def test_cagr_handler(self):
        # Bug-6645: CAGR requires month or coarser grain.
        b = VariantBinding(
            base_expression='SUM("Revenue")',
            fact_date_column='"order_date"',
            n=3,
            time_grain="year",
        )
        result = emit_variant_expression("cagr", b)
        assert "POWER" in result.sql
        # The zero/negative-base guard is a CASE ... THEN NULL wrapper
        # (POWER over a non-positive base would be NaN/error, which NULLIF
        # alone could not prevent).
        assert "CASE WHEN" in result.sql
        assert "THEN NULL" in result.sql

    def test_pct_change_handler(self):
        b = VariantBinding(
            base_expression='SUM("Revenue")',
            fact_date_column='"order_date"',
        )
        result = emit_variant_expression("pct_change", b)
        assert "LAG" in result.sql
        assert "NULLIF" in result.sql

    def test_unknown_variant_raises(self):
        b = VariantBinding(
            base_expression='SUM("Revenue")',
            fact_date_column='"order_date"',
        )
        with pytest.raises(VariantSqlError, match="Unknown variant"):
            emit_variant_expression("nonexistent", b)


# ---------------------------------------------------------------------------
# Evaluator fallback for kpi() inside time functions
# ---------------------------------------------------------------------------


class TestEvaluatorTimeFallback:
    """Test the Python evaluator handles time functions with kpi() refs."""

    @pytest.mark.asyncio
    async def test_prior_period_kpi_ref_returns_none_in_fallback(self):
        """When kpi() is inside prior_period, the Python fallback cannot
        compute the time-shifted value and correctly returns None rather
        than silently returning the current-period value.
        """
        from src.kpi_evaluator import MeasureValueProvider, _resolve_expression_value

        async def mock_kpi_value(name: str) -> float | None:
            if name == "Child KPI":
                return 42.0
            return None

        provider = MeasureValueProvider(
            get_kpi_value=mock_kpi_value,
        )
        result = await _resolve_expression_value(
            'prior_period(kpi("Child KPI"), "month")', provider,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_time_function_with_measure_returns_none_in_fallback(self):
        """Time intelligence functions in the Python fallback path return
        None because they cannot compute time-shifted values without SQL.
        """
        from src.kpi_evaluator import MeasureValueProvider, _resolve_expression_value

        async def mock_measure_value(name: str) -> float | None:
            if name == "Revenue":
                return 1000.0
            return None

        provider = MeasureValueProvider(
            get_measure_value=mock_measure_value,
        )
        result = await _resolve_expression_value(
            'lag(measure("Revenue"), literal(1), "month")', provider,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_time_function_no_args_returns_none(self):
        from src.kpi_evaluator import MeasureValueProvider, _resolve_expression_value

        provider = MeasureValueProvider()
        # This will parse but have empty args for the time function
        result = await _resolve_expression_value(
            'measure("Revenue")', provider,
        )
        # measure() with no provider returns None
        assert result is None

    @pytest.mark.asyncio
    async def test_safe_div_with_time_function_returns_none(self):
        """safe_div(measure, prior_period(kpi)) returns None because
        prior_period cannot be evaluated in the Python fallback path.
        safe_div then has a None denominator and correctly returns None.
        """
        from src.kpi_evaluator import MeasureValueProvider, _resolve_expression_value

        async def mock_measure(name: str) -> float | None:
            return 1000.0

        async def mock_kpi(name: str) -> float | None:
            return 800.0

        provider = MeasureValueProvider(
            get_measure_value=mock_measure,
            get_kpi_value=mock_kpi,
        )
        result = await _resolve_expression_value(
            'safe_div(measure("Revenue"), prior_period(kpi("Prior Revenue"), "month"))',
            provider,
        )
        # prior_period returns None in fallback, so safe_div returns None
        assert result is None
