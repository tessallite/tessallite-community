"""Unit tests for kpi_trend.py (Phase 2 — KPI v2 trend evaluation)."""
from __future__ import annotations

import pytest

from src.kpi_trend import (
    INSUFFICIENT_DATA,
    SparklinePoint,
    TrendResult,
    build_sparkline_series,
    evaluate_trend,
)

pytestmark = pytest.mark.unit


class TestEvaluateTrend:
    def test_improving(self):
        result = evaluate_trend(110, 100, threshold=0.01)
        assert result.trend == 1
        assert result.trend_label == "Improving"
        assert result.trend_pct == pytest.approx(0.10)

    def test_declining(self):
        result = evaluate_trend(90, 100, threshold=0.01)
        assert result.trend == -1
        assert result.trend_label == "Declining"
        assert result.trend_pct == pytest.approx(-0.10)

    def test_stable(self):
        result = evaluate_trend(100.5, 100, threshold=0.01)
        assert result.trend == 0
        assert result.trend_label == "Stable"
        assert result.trend_pct == pytest.approx(0.005)

    def test_exactly_at_threshold(self):
        """Change of exactly threshold should be stable (not > threshold)."""
        result = evaluate_trend(101, 100, threshold=0.01)
        assert result.trend == 0  # 0.01 is not > 0.01

    def test_just_above_threshold(self):
        result = evaluate_trend(101.1, 100, threshold=0.01)
        assert result.trend == 1

    def test_lower_is_better_improving(self):
        """For lower_is_better, a decrease is improving."""
        result = evaluate_trend(90, 100, threshold=0.01, direction="lower_is_better")
        assert result.trend == 1
        assert result.trend_label == "Improving"

    def test_lower_is_better_declining(self):
        """For lower_is_better, an increase is declining."""
        result = evaluate_trend(110, 100, threshold=0.01, direction="lower_is_better")
        assert result.trend == -1
        assert result.trend_label == "Declining"

    def test_closer_is_better_moving_closer(self):
        """Value moves from 110 to 105 (closer to target 100) → Improving."""
        result = evaluate_trend(105, 110, threshold=0.01, direction="closer_is_better", target=100)
        assert result.trend == 1
        assert result.trend_label == "Improving"

    def test_closer_is_better_moving_further(self):
        """Value moves from 105 to 110 (further from target 100) → Declining."""
        result = evaluate_trend(110, 105, threshold=0.01, direction="closer_is_better", target=100)
        assert result.trend == -1
        assert result.trend_label == "Declining"

    def test_closer_is_better_below_target_moving_closer(self):
        """Value moves from 80 to 90 (closer to target 100) → Improving."""
        result = evaluate_trend(90, 80, threshold=0.01, direction="closer_is_better", target=100)
        assert result.trend == 1

    def test_closer_is_better_below_target_moving_further(self):
        """Value moves from 90 to 80 (further from target 100) → Declining."""
        result = evaluate_trend(80, 90, threshold=0.01, direction="closer_is_better", target=100)
        assert result.trend == -1

    def test_closer_is_better_at_target(self):
        """Value stays at target → Stable."""
        result = evaluate_trend(100, 100, threshold=0.01, direction="closer_is_better", target=100)
        assert result.trend == 0

    def test_closer_is_better_no_target_falls_back(self):
        """Without target, closer_is_better falls back to higher_is_better."""
        result = evaluate_trend(110, 100, threshold=0.01, direction="closer_is_better", target=None)
        assert result.trend == 1

    def test_null_current_value(self):
        result = evaluate_trend(None, 100)
        assert result.trend is None
        assert result.trend_label == "Insufficient Data"

    def test_null_prior_value(self):
        result = evaluate_trend(100, None)
        assert result.trend is None
        assert result.trend_label == "Insufficient Data"

    def test_zero_prior_value(self):
        result = evaluate_trend(100, 0)
        assert result.trend is None
        assert result.trend_label == "Insufficient Data"

    def test_both_zero(self):
        result = evaluate_trend(0, 0)
        assert result.trend == 0
        assert result.trend_label == "Stable"
        assert result.trend_pct == 0.0

    def test_nan_value(self):
        result = evaluate_trend(float("nan"), 100)
        assert result.trend is None

    def test_inf_value(self):
        result = evaluate_trend(float("inf"), 100)
        assert result.trend is None

    def test_negative_values(self):
        result = evaluate_trend(-50, -100, threshold=0.01)
        # pct_change = (-50 - (-100)) / |-100| = 50/100 = 0.5
        assert result.trend == 1
        assert result.trend_pct == pytest.approx(0.5)


class TestBuildSparklineSeries:
    def test_basic_series(self):
        data = [
            ("2026-01", 100.0),
            ("2026-02", 110.0),
            ("2026-03", 105.0),
        ]
        series = build_sparkline_series(data)
        assert len(series) == 3
        assert series[0].period == "2026-01"
        assert series[0].value == 100.0

    def test_max_periods(self):
        data = [(f"2025-{i:02d}", float(i * 10)) for i in range(1, 25)]
        series = build_sparkline_series(data, max_periods=12)
        assert len(series) == 12
        assert series[0].period == "2025-13"  # most recent 12

    def test_null_values_omitted(self):
        data = [
            ("2026-01", 100.0),
            ("2026-02", None),
            ("2026-03", 105.0),
        ]
        series = build_sparkline_series(data)
        assert len(series) == 2
        assert series[0].period == "2026-01"
        assert series[1].period == "2026-03"

    def test_empty_data(self):
        series = build_sparkline_series([])
        assert series == []

    def test_all_null(self):
        data = [("2026-01", None), ("2026-02", None)]
        series = build_sparkline_series(data)
        assert series == []
