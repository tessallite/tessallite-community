"""KPI trend evaluation.

Implements Section 8 of the KPI requirements specification:
- Trend calculation via percentage change
- Trend classification (Improving / Stable / Declining)
- Sparkline data generation for the last N periods
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TrendResult:
    """Result of trend evaluation."""
    trend: Optional[int]        # 1=improving, 0=stable, -1=declining, None=insufficient data
    trend_label: Optional[str]
    trend_pct: Optional[float]  # raw percentage change as decimal (0.05 = 5%)
    # Bug-7238: direction-normalised percentage — positive means improving,
    # negative means declining, regardless of direction preference.  The
    # scorecard improvement chip should use this field, not the raw trend_pct,
    # so the sign and colour never contradict each other.
    trend_pct_normalised: Optional[float] = None


# Map trend integer to label
_TREND_LABELS = {1: "Improving", 0: "Stable", -1: "Declining"}

INSUFFICIENT_DATA = TrendResult(
    trend=None,
    trend_label="Insufficient Data",
    trend_pct=None,
    trend_pct_normalised=None,
)


# ---------------------------------------------------------------------------
# Trend computation
# ---------------------------------------------------------------------------

def evaluate_trend(
    current_value: Optional[float],
    prior_value: Optional[float],
    threshold: float = 0.01,
    direction: str = "higher_is_better",
    target: Optional[float] = None,
) -> TrendResult:
    """Evaluate the trend by comparing current value to prior period value.

    Parameters
    ----------
    current_value : float | None
        The current period's KPI value.
    prior_value : float | None
        The prior period's KPI value.
    threshold : float
        Threshold for classifying stable vs improving/declining. Default 0.01 (1%).
    direction : str
        Determines whether positive change is improving or declining.
    target : float | None
        Target value, required for closer_is_better to determine whether the
        value moved closer to or further from the goal.

    Returns
    -------
    TrendResult
    """
    if current_value is None or prior_value is None:
        return INSUFFICIENT_DATA

    if math.isnan(current_value) or math.isinf(current_value):
        return INSUFFICIENT_DATA
    if math.isnan(prior_value) or math.isinf(prior_value):
        return INSUFFICIENT_DATA

    # Compute percentage change
    if prior_value == 0:
        if current_value == 0:
            return TrendResult(trend=0, trend_label="Stable", trend_pct=0.0, trend_pct_normalised=0.0)
        # Can't compute percentage change from zero
        return INSUFFICIENT_DATA

    pct_change = (current_value - prior_value) / abs(prior_value)

    # Classify based on direction
    if direction == "closer_is_better" and target is not None:
        cur_distance = abs(current_value - target)
        prior_distance = abs(prior_value - target)
        if prior_distance == 0:
            effective_change = -abs(pct_change) if cur_distance > 0 else 0.0
        else:
            effective_change = (prior_distance - cur_distance) / prior_distance
    elif direction == "lower_is_better":
        effective_change = -pct_change
    else:
        effective_change = pct_change

    if effective_change > threshold:
        trend_int = 1
    elif effective_change < -threshold:
        trend_int = -1
    else:
        trend_int = 0

    return TrendResult(
        trend=trend_int,
        trend_label=_TREND_LABELS[trend_int],
        trend_pct=pct_change,
        # Bug-7238: normalised percentage — same magnitude as pct_change but
        # the sign follows the direction preference (positive = improving).
        trend_pct_normalised=effective_change,
    )


# ---------------------------------------------------------------------------
# Sparkline series
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SparklinePoint:
    """A single point in the trend sparkline series."""
    period: str
    value: Optional[float]


def build_sparkline_series(
    period_values: list[tuple[str, Optional[float]]],
    max_periods: int = 12,
) -> list[SparklinePoint]:
    """Build a sparkline series from period/value pairs.

    Parameters
    ----------
    period_values : list[tuple[str, float | None]]
        List of (period_label, value) tuples in chronological order.
        e.g., [("2025-01", 105000), ("2025-02", 112000), ...]
    max_periods : int
        Maximum number of periods to return (most recent N).

    Returns
    -------
    list[SparklinePoint]
        Sparkline data points. Periods with NULL values are omitted (gap handling).
    """
    # Take the most recent N periods
    recent = period_values[-max_periods:] if len(period_values) > max_periods else period_values

    # Omit NULL periods (gap handling per spec Section 7.2)
    return [
        SparklinePoint(period=period, value=value)
        for period, value in recent
        if value is not None
    ]
