"""Bug-6831: count_distinct cumulation guard for trailing_sum/moving_avg in KPI compiler.

COUNT_DISTINCT is non-additive. Summing or averaging per-period distinct counts
double-counts values that recur across periods and produces an inflated result.
The query-router's source_sql.py already guards this (Bug-6229); this test
covers the kpi_compiler's CTE-based scalar path (trailing_sum / moving_avg).

Run from tessallite/services/model-service/:
    pytest tests/test_bug_6831_count_distinct_guard.py
"""
from __future__ import annotations

import pytest

from src.kpi_compiler import (
    CompilerContext,
    compile_scalar_kpi_sql,
)


class TestCountDistinctCumulationGuard:
    def test_trailing_sum_rejects_count_distinct_base(self):
        with pytest.raises(ValueError, match="COUNT\\(DISTINCT\\).*trailing_sum"):
            compile_scalar_kpi_sql(
                'COUNT(DISTINCT "customer_id")',
                CompilerContext(time_column="order_date"),
                ti_type="trailing_sum",
                ti_grain="month",
                ti_n_periods=3,
                time_window_start_sql="DATE_TRUNC('month', CURRENT_DATE)",
                time_window_end_sql="CURRENT_DATE",
            )

    def test_moving_avg_rejects_count_distinct_base(self):
        with pytest.raises(ValueError, match="COUNT\\(DISTINCT\\).*moving_avg"):
            compile_scalar_kpi_sql(
                'COUNT(DISTINCT "customer_id")',
                CompilerContext(time_column="order_date"),
                ti_type="moving_avg",
                ti_grain="month",
                ti_n_periods=3,
                time_window_start_sql="DATE_TRUNC('month', CURRENT_DATE)",
                time_window_end_sql="CURRENT_DATE",
            )

    def test_trailing_sum_rejects_count_distinct_via_default_agg(self):
        with pytest.raises(ValueError, match="COUNT\\(DISTINCT\\).*trailing_sum"):
            compile_scalar_kpi_sql(
                'SUM("revenue")',
                CompilerContext(
                    time_column="order_date",
                    default_agg="COUNT_DISTINCT",
                ),
                ti_type="trailing_sum",
                ti_grain="month",
                ti_n_periods=3,
                time_window_start_sql="DATE_TRUNC('month', CURRENT_DATE)",
                time_window_end_sql="CURRENT_DATE",
            )

    def test_trailing_sum_allows_sum_base(self):
        result = compile_scalar_kpi_sql(
            'SUM("revenue")',
            CompilerContext(time_column="order_date"),
            ti_type="trailing_sum",
            ti_grain="month",
            ti_n_periods=3,
            time_window_start_sql="DATE_TRUNC('month', CURRENT_DATE)",
            time_window_end_sql="CURRENT_DATE",
        )
        assert result is not None
        assert "SUM(value)" in result

    def test_moving_avg_allows_sum_base(self):
        result = compile_scalar_kpi_sql(
            'SUM("revenue")',
            CompilerContext(time_column="order_date"),
            ti_type="moving_avg",
            ti_grain="month",
            ti_n_periods=3,
            time_window_start_sql="DATE_TRUNC('month', CURRENT_DATE)",
            time_window_end_sql="CURRENT_DATE",
        )
        assert result is not None
        assert "AVG(value)" in result

    def test_trailing_sum_rejects_composite_with_count_distinct(self):
        """Composite expressions containing COUNT(DISTINCT) anywhere must
        also be rejected -- e.g. SUM(revenue) + COUNT(DISTINCT customer_id).
        The guard uses containment check, not startswith, to catch this."""
        with pytest.raises(ValueError, match="COUNT\\(DISTINCT\\).*trailing_sum"):
            compile_scalar_kpi_sql(
                'SUM("revenue") + COUNT(DISTINCT "customer_id")',
                CompilerContext(time_column="order_date"),
                ti_type="trailing_sum",
                ti_grain="month",
                ti_n_periods=3,
                time_window_start_sql="DATE_TRUNC('month', CURRENT_DATE)",
                time_window_end_sql="CURRENT_DATE",
            )

    def test_moving_avg_rejects_composite_with_count_distinct(self):
        """Same as above but for moving_avg."""
        with pytest.raises(ValueError, match="COUNT\\(DISTINCT\\).*moving_avg"):
            compile_scalar_kpi_sql(
                'SUM("revenue") + COUNT(DISTINCT "customer_id")',
                CompilerContext(time_column="order_date"),
                ti_type="moving_avg",
                ti_grain="month",
                ti_n_periods=3,
                time_window_start_sql="DATE_TRUNC('month', CURRENT_DATE)",
                time_window_end_sql="CURRENT_DATE",
            )

    def test_period_to_date_allows_count_distinct(self):
        # period_to_date computes a single aggregate over the period --
        # no cross-period summation, so count_distinct is valid.
        result = compile_scalar_kpi_sql(
            'COUNT(DISTINCT "customer_id")',
            CompilerContext(time_column="order_date"),
            ti_type="period_to_date",
            ti_grain="month",
        )
        assert result is not None
        assert 'COUNT(DISTINCT "customer_id")' in result
