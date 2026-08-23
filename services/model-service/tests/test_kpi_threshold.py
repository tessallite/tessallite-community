"""Unit tests for kpi_threshold.py (Phases 2+4 — KPI v2 threshold evaluation)."""
from __future__ import annotations

import math

import pytest

from src.kpi_threshold import (
    BAND_PRESETS,
    BAND_PRESETS_COLORBLIND,
    Band,
    BandValidationError,
    ThresholdResult,
    evaluate_threshold,
    get_preset_bands,
    list_presets,
    validate_bands,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Direction handling
# ---------------------------------------------------------------------------

class TestDirectionHigherIsBetter:
    def test_value_above_target(self):
        result = evaluate_threshold(120, 100, direction="higher_is_better")
        assert result.status == 1  # best band
        assert result.status_label is not None

    def test_value_below_target(self):
        result = evaluate_threshold(50, 100, direction="higher_is_better")
        assert result.status == -1  # worst band

    def test_value_near_target(self):
        result = evaluate_threshold(85, 100, direction="higher_is_better")
        assert result.status == 0  # middle band


class TestDirectionLowerIsBetter:
    def test_value_below_target(self):
        """Value below target is GOOD for lower_is_better (e.g., spending less)."""
        result = evaluate_threshold(
            80, 100, direction="lower_is_better",
        )
        # ratio = target/value = 100/80 = 1.25 → above 1.0 → falls in best band
        assert result.status == 1
        assert result.status_label == "On Track"
        assert result.ratio == pytest.approx(1.25)

    def test_value_above_target(self):
        """Value above target is BAD for lower_is_better (e.g., overspending)."""
        result = evaluate_threshold(
            120, 100, direction="lower_is_better",
        )
        # ratio = target/value = 100/120 = 0.833 → falls in Near Target band
        assert result.status_label == "Near Target"
        assert result.ratio == pytest.approx(100 / 120)

    def test_value_way_above_target(self):
        """Greatly exceeding target is worst for lower_is_better."""
        result = evaluate_threshold(
            200, 100, direction="lower_is_better",
        )
        # ratio = target/value = 100/200 = 0.5 → falls in Off Target band
        assert result.status == -1
        assert result.status_label == "Off Target"

    def test_uses_standard_bands(self):
        """lower_is_better uses standard_3_band since ratio is already inverted."""
        result = evaluate_threshold(80, 100, direction="lower_is_better")
        # Should use standard_3_band, not inverted
        assert result.status_label == "On Track"
        assert result.status_color == "#388E3C"


class TestDirectionCloserIsBetter:
    def test_on_target(self):
        result = evaluate_threshold(100, 100, direction="closer_is_better")
        # ratio = 1 - |100-100|/100 = 1.0, should be best band
        assert result.status == 1

    def test_far_from_target(self):
        result = evaluate_threshold(50, 100, direction="closer_is_better")
        # ratio = 1 - |50-100|/100 = 0.5, should be worst band
        assert result.status == -1


# ---------------------------------------------------------------------------
# Bug-7223: lower_is_better with NEGATIVE values
# ---------------------------------------------------------------------------

class TestLowerIsBetterNegativeValues:
    """Bug-7223: negative values under lower_is_better must NOT be classified
    as worst (Off Target).  A negative value for a cost/error KPI is the
    best possible outcome — a credit, a negative variance, etc."""

    def test_negative_value_positive_target_is_best(self):
        """value=-50, target=100: value is far below target, best outcome."""
        result = evaluate_threshold(-50, 100, direction="lower_is_better")
        assert result.status == 1
        assert result.status_label == "On Track"

    def test_small_negative_value_positive_target_is_best(self):
        """value=-1, target=100: any negative is better than zero, which is best."""
        result = evaluate_threshold(-1, 100, direction="lower_is_better")
        assert result.status == 1

    def test_large_negative_value_is_best(self):
        """value=-1000, target=50: extreme negative = extreme outperformance."""
        result = evaluate_threshold(-1000, 50, direction="lower_is_better")
        assert result.status == 1

    def test_negative_value_composite_normalisation(self):
        """Composite normaliser must also score negative lower-is-better as 100."""
        from src.kpi_composite import normalise_pct_target
        result = normalise_pct_target(-50, 100, direction="lower_is_better")
        assert result == 100.0

    def test_negative_value_composite_small(self):
        """Even slightly negative gives perfect composite contribution."""
        from src.kpi_composite import normalise_pct_target
        result = normalise_pct_target(-0.01, 100, direction="lower_is_better")
        assert result == 100.0

    def test_both_negative_more_negative_is_better(self):
        """value=-200, target=-100: value more negative than target = better."""
        result = evaluate_threshold(-200, -100, direction="lower_is_better")
        # ratio = value/target = (-200)/(-100) = 2.0 -> On Track (>= 1.0)
        assert result.status == 1
        assert result.ratio == pytest.approx(2.0)

    def test_both_negative_less_negative_is_worse(self):
        """value=-50, target=-100: value less negative than target = worse."""
        result = evaluate_threshold(-50, -100, direction="lower_is_better")
        # ratio = value/target = (-50)/(-100) = 0.5 -> Off Target (< 0.80)
        assert result.status == -1
        assert result.ratio == pytest.approx(0.5)

    def test_both_negative_on_target(self):
        """value=-100, target=-100: exactly on target."""
        result = evaluate_threshold(-100, -100, direction="lower_is_better")
        # ratio = value/target = 1.0 -> On Track (>= 1.0)
        assert result.status == 1
        assert result.ratio == pytest.approx(1.0)

    def test_both_negative_composite(self):
        """Composite normaliser: more negative value = better contribution."""
        from src.kpi_composite import normalise_pct_target
        result = normalise_pct_target(-200, -100, direction="lower_is_better")
        # (value/target)*100 = 200, clamped to 100.0
        assert result == 100.0

    def test_both_negative_composite_worse(self):
        """Composite normaliser: less negative value = lower contribution."""
        from src.kpi_composite import normalise_pct_target
        result = normalise_pct_target(-50, -100, direction="lower_is_better")
        # (value/target)*100 = (-50/-100)*100 = 50.0
        assert result == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# Bug-7224: closer_is_better + z_score / percentile_rank
# ---------------------------------------------------------------------------

class TestCloserIsBetterStatistical:
    """Bug-7224: closer_is_better combined with z_score or percentile_rank
    must NOT produce inverted statuses.  A far outlier should classify as
    worst, not best."""

    def test_zscore_far_outlier_is_bad(self):
        """A value 3+ sigma above mean should be Off Target for closer_is_better."""
        result = evaluate_threshold(
            value=130,
            target=100,
            direction="closer_is_better",
            evaluation_type="z_score",
            historical_values=[90, 95, 100, 105, 110],
        )
        # 130 is ~3.25 sigma above mean=100.  -abs(z)=-3.25 should land in
        # the z_score Off Target band (min=None, max=-1.0).
        assert result.status == -1
        assert result.status_label == "Off Target"

    def test_zscore_far_outlier_below_is_also_bad(self):
        """A value 3+ sigma BELOW mean is equally bad for closer_is_better."""
        result = evaluate_threshold(
            value=70,
            target=100,
            direction="closer_is_better",
            evaluation_type="z_score",
            historical_values=[90, 95, 100, 105, 110],
        )
        # 70 is ~3.25 sigma below mean.  -abs(z)=-3.25 -> Off Target.
        assert result.status == -1

    def test_zscore_near_mean_is_on_track(self):
        """A value at the mean is On Track for closer_is_better."""
        result = evaluate_threshold(
            value=100,
            target=100,
            direction="closer_is_better",
            evaluation_type="z_score",
            historical_values=[90, 95, 100, 105, 110],
        )
        # z=0 -> -abs(0)=0 -> z_score_closer On Track band (-0.5, None).
        assert result.status == 1
        assert result.status_label == "On Track"

    def test_percentile_at_median_is_best(self):
        """The 50th percentile is the best rank for closer_is_better."""
        result = evaluate_threshold(
            value=100,
            target=100,
            direction="closer_is_better",
            evaluation_type="percentile_rank",
            peer_values=[80, 90, 100, 110, 120],
        )
        # Percentile ~60, |60-50|/50 = 0.2, (1-0.2)*100=80 -> On Track (>=50).
        assert result.status == 1

    def test_percentile_at_extreme_high_is_bad(self):
        """A very high percentile is bad for closer_is_better (too far from median)."""
        result = evaluate_threshold(
            value=200,
            target=100,
            direction="closer_is_better",
            evaluation_type="percentile_rank",
            peer_values=[80, 90, 100, 110, 120],
        )
        # Percentile=100 (all peers below).  |100-50|/50=1.0, (1-1)*100=0
        # -> Off Target band (min=None, max=25.0).
        assert result.status == -1

    def test_percentile_at_extreme_low_is_bad(self):
        """A very low percentile is equally bad for closer_is_better."""
        result = evaluate_threshold(
            value=50,
            target=100,
            direction="closer_is_better",
            evaluation_type="percentile_rank",
            peer_values=[80, 90, 100, 110, 120],
        )
        # Percentile=0 (no peers below).  |0-50|/50=1.0, (1-1)*100=0
        # -> Off Target.
        assert result.status == -1


# ---------------------------------------------------------------------------
# Evaluation types
# ---------------------------------------------------------------------------

class TestAbsoluteValueBandColour:
    """Bug-974: absolute_value status derives from each band's colour, not its
    position — "good" may sit at either end (e.g. a KYC breach ratio)."""

    _KYC_BANDS = [
        {"label": "Good", "color": "#388E3C", "min": None, "max": 0.30},
        {"label": "Watch", "color": "#F57C00", "min": 0.30, "max": 0.50},
        {"label": "Breach", "color": "#D32F2F", "min": 0.50, "max": None},
    ]

    def test_good_at_low_end_is_good_status(self):
        result = evaluate_threshold(
            value=0.10, target=None,
            evaluation_type="absolute_value", bands=self._KYC_BANDS,
        )
        assert result.band_index == 0
        assert result.status == 1  # green band -> good, NOT -1 (old positional)
        assert result.status_label == "Good"

    def test_bad_at_high_end_is_bad_status(self):
        result = evaluate_threshold(
            value=0.70, target=None,
            evaluation_type="absolute_value", bands=self._KYC_BANDS,
        )
        assert result.band_index == 2
        assert result.status == -1  # red band -> bad, NOT 1 (old positional)
        assert result.status_label == "Breach"

    def test_warning_in_middle(self):
        result = evaluate_threshold(
            value=0.40, target=None,
            evaluation_type="absolute_value", bands=self._KYC_BANDS,
        )
        assert result.status == 0  # amber band -> warning


class TestEvaluationTypes:
    def test_percentage_of_target(self):
        result = evaluate_threshold(
            90, 100,
            evaluation_type="percentage_of_target",
        )
        assert result.ratio == pytest.approx(0.9)

    def test_absolute_variance(self):
        # F-017-01: closer_is_better keeps the unsigned-deviation semantics
        # (|value - target|), so a deviation of 10 lands in the middle band of
        # deviation-ordered custom bands. Directional variance is signed
        # favourable and is covered by TestDirectionalVariance below.
        result = evaluate_threshold(
            90, 100,
            direction="closer_is_better",
            evaluation_type="absolute_variance",
            bands=[
                {"label": "On Track", "color": "#388E3C", "min": None, "max": 5},
                {"label": "Warning", "color": "#F57C00", "min": 5, "max": 15},
                {"label": "Off Target", "color": "#D32F2F", "min": 15, "max": None},
            ],
        )
        assert result.ratio == pytest.approx(10.0)
        assert result.status == 0  # middle band (5 <= 10 < 15)

    def test_percentage_variance(self):
        # F-017-01: closer_is_better keeps unsigned deviation |value-target|/target.
        result = evaluate_threshold(
            90, 100,
            direction="closer_is_better",
            evaluation_type="percentage_variance",
            bands=[
                {"label": "On Track", "color": "#388E3C", "min": None, "max": 0.05},
                {"label": "Warning", "color": "#F57C00", "min": 0.05, "max": 0.15},
                {"label": "Off Target", "color": "#D32F2F", "min": 0.15, "max": None},
            ],
        )
        assert result.ratio == pytest.approx(0.1)
        assert result.status == 0  # middle band (0.05 <= 0.1 < 0.15)

    def test_z_score(self):
        historical = [95, 100, 105, 98, 102]
        result = evaluate_threshold(
            120, None,
            evaluation_type="z_score",
            historical_values=historical,
            bands=[
                {"label": "Normal", "color": "#388E3C", "min": None, "max": 1.0},
                {"label": "Warning", "color": "#F57C00", "min": 1.0, "max": 2.0},
                {"label": "Outlier", "color": "#D32F2F", "min": 2.0, "max": None},
            ],
        )
        assert result.ratio is not None
        # 120 is well above the mean of 100, z-score should be > 2
        assert result.ratio > 2.0

    def test_z_score_insufficient_data(self):
        result = evaluate_threshold(
            100, None,
            evaluation_type="z_score",
            historical_values=[100],  # need >= 2
        )
        assert result.status is None
        assert result.status_label == "No Data"

    def test_percentile_rank(self):
        peers = [50, 60, 70, 80, 90, 100]
        result = evaluate_threshold(
            85, None,
            evaluation_type="percentile_rank",
            peer_values=peers,
            bands=[
                {"label": "Bottom", "color": "#D32F2F", "min": None, "max": 25},
                {"label": "Middle", "color": "#F57C00", "min": 25, "max": 75},
                {"label": "Top", "color": "#388E3C", "min": 75, "max": None},
            ],
        )
        assert result.ratio is not None
        # 85 is above 4 of 6 values, percentile = 4/6*100 = 66.7
        assert 50 < result.ratio < 100


# ---------------------------------------------------------------------------
# F-017-02 / F-017-03: status/band/colour agreement for variance and
# statistical evaluation types
# ---------------------------------------------------------------------------

class TestVarianceStatusAgreement:
    """Variance bands are authored best-first (deviation 0 = perfect), so the
    status int must be derived from the band colour, not its position.
    """

    _VARIANCE_BANDS = [
        {"label": "On Track", "color": "#388E3C", "min": None, "max": 0.05},
        {"label": "Warning", "color": "#F57C00", "min": 0.05, "max": 0.15},
        {"label": "Off Target", "color": "#D32F2F", "min": 0.15, "max": None},
    ]

    def test_percentage_variance_best_band_is_good(self):
        # closer_is_better: value 99 vs target 100 -> deviation 0.01 -> first
        # (green) band. status MUST be +1, not -1 (the F-017-02 inversion).
        result = evaluate_threshold(
            99, 100,
            direction="closer_is_better",
            evaluation_type="percentage_variance",
            bands=self._VARIANCE_BANDS,
        )
        assert result.status_label == "On Track"
        assert result.status_color == "#388E3C"
        assert result.status == 1  # band, label, colour all agree on "good"

    def test_percentage_variance_worst_band_is_bad(self):
        # closer_is_better: value 130 vs target 100 -> deviation 0.30 -> last
        # (red) band.
        result = evaluate_threshold(
            130, 100,
            direction="closer_is_better",
            evaluation_type="percentage_variance",
            bands=self._VARIANCE_BANDS,
        )
        assert result.status_label == "Off Target"
        assert result.status == -1

    def test_absolute_variance_best_band_is_good(self):
        bands = [
            {"label": "On Track", "color": "#388E3C", "min": None, "max": 5},
            {"label": "Warning", "color": "#F57C00", "min": 5, "max": 15},
            {"label": "Off Target", "color": "#D32F2F", "min": 15, "max": None},
        ]
        result = evaluate_threshold(
            101, 100, direction="closer_is_better",
            evaluation_type="absolute_variance", bands=bands,
        )
        assert result.status_label == "On Track"
        assert result.status == 1

    def test_variance_default_bands_on_target_is_good(self):
        # No custom bands: a value exactly on target (variance 0) must classify
        # green, not the inverted red the centred preset produced (F-017-02).
        result = evaluate_threshold(
            100, 100, evaluation_type="percentage_variance",
        )
        assert result.status == 1
        assert result.status_color == "#388E3C"

    def test_variance_default_bands_far_off_is_bad(self):
        # F-017-01: for higher_is_better a value 60% UNDER target is an
        # unfavourable variance (-0.60) and must classify red under the default
        # directional bands. (Exceeding target is now green, not red — that was
        # the direction-blind bug this fix corrects.)
        result = evaluate_threshold(
            40, 100, direction="higher_is_better",
            evaluation_type="percentage_variance",
        )
        assert result.status == -1


class TestDirectionalVariance:
    """F-017-01 / F-103-06: directional (higher/lower) variance is signed
    favourable — a KPI beating its goal is green, one missing it is red — even
    with the DEFAULT bands and in raw currency units. Before the fix the default
    variance bands were the 0.10/0.20 fraction preset, so any currency deviation
    painted red and a beating cost KPI showed a red card next to a +$ variance.
    """

    def test_f017_01_cost_kpi_beating_absolute_variance_is_green(self):
        # A cost KPI (lower_is_better) that beats a $100,000 budget by $20,000
        # must be GREEN with the default absolute_variance bands.
        result = evaluate_threshold(
            80_000, 100_000,
            direction="lower_is_better",
            evaluation_type="absolute_variance",
        )
        assert result.status == 1
        assert result.status_color == "#388E3C"
        # signed favourable variance: target - value = +20,000 (beating).
        assert result.ratio == pytest.approx(20_000.0)

    def test_cost_kpi_over_budget_absolute_variance_is_red(self):
        # Same KPI $30,000 OVER budget must be red.
        result = evaluate_threshold(
            130_000, 100_000,
            direction="lower_is_better",
            evaluation_type="absolute_variance",
        )
        # target - value = -30,000, below -0.20*100,000 = -20,000 -> Off Target.
        assert result.status == -1
        assert result.ratio == pytest.approx(-30_000.0)

    def test_higher_is_better_beating_absolute_variance_is_green(self):
        # Revenue KPI exceeding a $100,000 target by $10,000 is green.
        result = evaluate_threshold(
            110_000, 100_000,
            direction="higher_is_better",
            evaluation_type="absolute_variance",
        )
        assert result.status == 1
        assert result.ratio == pytest.approx(10_000.0)

    def test_lower_is_better_beating_percentage_variance_is_green(self):
        # Cost KPI 20% under budget: signed favourable pct variance = +0.20.
        result = evaluate_threshold(
            80_000, 100_000,
            direction="lower_is_better",
            evaluation_type="percentage_variance",
        )
        assert result.status == 1
        assert result.ratio == pytest.approx(0.20)

    def test_lower_is_better_missing_percentage_variance_is_red(self):
        # Cost KPI 30% over budget: signed favourable pct variance = -0.30 -> red.
        result = evaluate_threshold(
            130_000, 100_000,
            direction="lower_is_better",
            evaluation_type="percentage_variance",
        )
        assert result.status == -1
        assert result.ratio == pytest.approx(-0.30)

    def test_absolute_variance_zero_target_does_not_crash(self):
        # Degenerate target: scaling by |target| would collapse the bands, so the
        # fix falls back to an unscaled fraction preset. Must return a status,
        # never raise.
        result = evaluate_threshold(
            5, 0,
            direction="higher_is_better",
            evaluation_type="absolute_variance",
        )
        assert result.status in (-1, 0, 1)


class TestPercentileDirection:
    """F-017-03: percentile_rank must honour lower_is_better."""

    _BANDS = [
        {"label": "Bottom", "color": "#D32F2F", "min": None, "max": 25},
        {"label": "Middle", "color": "#F57C00", "min": 25, "max": 75},
        {"label": "Top", "color": "#388E3C", "min": 75, "max": None},
    ]
    _PEERS = [50, 60, 70, 80, 90, 100]

    def test_higher_is_better_high_value_ranks_top(self):
        # 95 beats 5 of 6 peers -> percentile ~83 -> Top (good).
        result = evaluate_threshold(
            95, None, evaluation_type="percentile_rank",
            direction="higher_is_better", peer_values=self._PEERS,
            bands=self._BANDS,
        )
        assert result.ratio == pytest.approx(100 * 5 / 6, abs=0.1)
        assert result.status_label == "Top"
        assert result.status == 1

    def test_lower_is_better_low_value_ranks_top(self):
        # For a cost KPI, 45 (below every peer) is BEST. Raw percentile is 0,
        # inverted to 100 -> Top (good).
        result = evaluate_threshold(
            45, None, evaluation_type="percentile_rank",
            direction="lower_is_better", peer_values=self._PEERS,
            bands=self._BANDS,
        )
        assert result.ratio == pytest.approx(100.0)
        assert result.status_label == "Top"
        assert result.status == 1

    def test_lower_is_better_high_value_ranks_bottom(self):
        # 105 (above every peer) is WORST for a cost KPI: raw percentile 100,
        # inverted to 0 -> Bottom (bad).
        result = evaluate_threshold(
            105, None, evaluation_type="percentile_rank",
            direction="lower_is_better", peer_values=self._PEERS,
            bands=self._BANDS,
        )
        assert result.ratio == pytest.approx(0.0)
        assert result.status == -1


class TestPositionMatchesStatusBand:
    """Bug-1226: the exposed gauge position (``ratio``) must land inside the
    band the status was matched to (``bands_used[band_index]``), and that band's
    colour/label must equal the reported status. This is the contract the
    frontend gauge relies on: needle = position, plotted against bands_used, so
    needle colour cannot disagree with the status badge for any evaluation type.
    """

    @staticmethod
    def _position_in_band(pos, band) -> bool:
        lower_ok = band.min is None or pos >= band.min
        upper_ok = band.max is None or pos < band.max
        return lower_ok and upper_ok

    def _assert_agreement(self, result):
        assert result.bands_used is not None and len(result.bands_used) > 0
        assert result.band_index is not None
        matched = result.bands_used[result.band_index]
        # The exposed position lands in the matched band → needle plotted from
        # `ratio` against `bands_used` sits in the same coloured arc the badge
        # reports.
        assert self._position_in_band(result.ratio, matched)
        assert result.status_color == matched.color
        assert result.status_label == matched.label

    def test_percentage_variance_beating_target_agrees(self):
        # Cost KPI beating target: 2,753,735 vs 2,800,000, deviation 1.65%.
        # The round-1 reproduction: badge GREEN but legacy gauge RED. With the
        # authoritative position the needle (0.0165) lands in the green band.
        result = evaluate_threshold(
            2_753_735, 2_800_000,
            direction="lower_is_better",
            evaluation_type="percentage_variance",
        )
        assert result.status == 1  # GREEN badge
        self._assert_agreement(result)
        # Needle position is the deviation, NOT the percentage-of-target 101.68
        # that produced the red gauge in round 1.
        assert result.ratio == pytest.approx(0.0165, abs=0.001)

    def test_percentage_variance_missing_target_agrees(self):
        # Cost KPI 30% over target → red badge, needle in red band.
        result = evaluate_threshold(
            3_640_000, 2_800_000,
            direction="lower_is_better",
            evaluation_type="percentage_variance",
        )
        assert result.status == -1
        self._assert_agreement(result)

    def test_z_score_beating_agrees(self):
        # Recent value well above its history → high z → top band.
        bands = [
            {"label": "Low", "color": "#D32F2F", "min": None, "max": -0.5},
            {"label": "Mid", "color": "#F57C00", "min": -0.5, "max": 0.5},
            {"label": "High", "color": "#388E3C", "min": 0.5, "max": None},
        ]
        result = evaluate_threshold(
            120, None, evaluation_type="z_score",
            historical_values=[100, 102, 98, 101, 99],
            bands=bands,
        )
        assert result.status == 1
        self._assert_agreement(result)

    def test_z_score_missing_agrees(self):
        bands = [
            {"label": "Low", "color": "#D32F2F", "min": None, "max": -0.5},
            {"label": "Mid", "color": "#F57C00", "min": -0.5, "max": 0.5},
            {"label": "High", "color": "#388E3C", "min": 0.5, "max": None},
        ]
        result = evaluate_threshold(
            70, None, evaluation_type="z_score",
            historical_values=[100, 102, 98, 101, 99],
            bands=bands,
        )
        assert result.status == -1
        self._assert_agreement(result)

    def test_percentile_beating_agrees(self):
        bands = [
            {"label": "Bottom", "color": "#D32F2F", "min": None, "max": 25},
            {"label": "Middle", "color": "#F57C00", "min": 25, "max": 75},
            {"label": "Top", "color": "#388E3C", "min": 75, "max": None},
        ]
        result = evaluate_threshold(
            95, None, evaluation_type="percentile_rank",
            direction="higher_is_better",
            peer_values=[50, 60, 70, 80, 90, 100],
            bands=bands,
        )
        assert result.status == 1
        self._assert_agreement(result)

    def test_percentile_inverted_missing_agrees(self):
        # Inverted (lower_is_better cost KPI): high value ranks bottom → red.
        bands = [
            {"label": "Bottom", "color": "#D32F2F", "min": None, "max": 25},
            {"label": "Middle", "color": "#F57C00", "min": 25, "max": 75},
            {"label": "Top", "color": "#388E3C", "min": 75, "max": None},
        ]
        result = evaluate_threshold(
            105, None, evaluation_type="percentile_rank",
            direction="lower_is_better",
            peer_values=[50, 60, 70, 80, 90, 100],
            bands=bands,
        )
        assert result.status == -1
        self._assert_agreement(result)

    def test_percentage_of_target_default_bands_agrees(self):
        # Default percentage_of_target preset: beating and missing both agree.
        beating = evaluate_threshold(110, 100, direction="higher_is_better")
        assert beating.status == 1
        self._assert_agreement(beating)
        missing = evaluate_threshold(70, 100, direction="higher_is_better")
        assert missing.status == -1
        self._assert_agreement(missing)


# ---------------------------------------------------------------------------
# Band matching
# ---------------------------------------------------------------------------

class TestBandMatching:
    def test_inclusive_lower_exclusive_upper(self):
        """Band boundaries: [min, max) convention."""
        bands = [
            {"label": "Low", "color": "#D32F2F", "min": None, "max": 0.80},
            {"label": "Mid", "color": "#F57C00", "min": 0.80, "max": 1.00},
            {"label": "High", "color": "#388E3C", "min": 1.00, "max": None},
        ]
        # Exactly 0.80 should fall in Mid (inclusive lower)
        result = evaluate_threshold(80, 100, bands=bands)
        assert result.ratio == pytest.approx(0.80)
        assert result.status_label == "Mid"

        # Exactly 1.00 should fall in High (inclusive lower)
        result = evaluate_threshold(100, 100, bands=bands)
        assert result.ratio == pytest.approx(1.00)
        assert result.status_label == "High"

    def test_null_bounds(self):
        """None min = -infinity, None max = +infinity."""
        bands = [
            {"label": "All", "color": "#388E3C", "min": None, "max": None},
        ]
        result = evaluate_threshold(999, 100, bands=bands)
        assert result.status_label == "All"


# ---------------------------------------------------------------------------
# NULL and edge cases
# ---------------------------------------------------------------------------

class TestNullHandling:
    def test_null_value(self):
        result = evaluate_threshold(None, 100)
        assert result.status is None
        assert result.status_label == "No Data"
        assert result.status_color == "#9E9E9E"

    def test_null_target_percentage(self):
        result = evaluate_threshold(100, None)
        assert result.status is None
        assert result.status_label == "No Data"

    def test_null_target_ok_for_z_score(self):
        """z_score and percentile_rank don't need a target."""
        result = evaluate_threshold(
            100, None,
            evaluation_type="z_score",
            historical_values=[90, 95, 100, 105, 110],
            bands=[
                {"label": "Normal", "color": "#388E3C", "min": None, "max": 2.0},
                {"label": "Outlier", "color": "#D32F2F", "min": 2.0, "max": None},
            ],
        )
        assert result.status is not None

    def test_zero_target(self):
        result = evaluate_threshold(100, 0)
        assert result.status is None
        assert result.status_label == "No Data"

    def test_nan_value(self):
        result = evaluate_threshold(float("nan"), 100)
        assert result.status is None

    def test_inf_value(self):
        result = evaluate_threshold(float("inf"), 100)
        assert result.status is None

    def test_negative_value(self):
        """Negative values should still evaluate normally."""
        result = evaluate_threshold(-50, 100)
        assert result.status is not None
        assert result.ratio is not None


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

class TestPresets:
    def test_all_presets_exist(self):
        presets = list_presets()
        assert "standard_3_band" in presets
        assert "standard_4_band" in presets
        assert "tight_tolerance" in presets
        assert "centred" in presets
        assert "variance" in presets

    def test_variance_preset_is_deviation_ordered(self):
        # F-017-02: green band sits at the low (small-deviation) end.
        bands = get_preset_bands("variance")
        assert len(bands) == 3
        assert bands[0].label == "On Track"
        assert bands[0].color == "#388E3C"
        assert bands[0].max == pytest.approx(0.10)
        assert bands[-1].label == "Off Target"
        assert bands[-1].min == pytest.approx(0.20)

    def test_standard_3_band(self):
        bands = get_preset_bands("standard_3_band")
        assert len(bands) == 3
        assert bands[0].label == "Off Target"
        assert bands[2].label == "On Track"

    def test_standard_4_band(self):
        bands = get_preset_bands("standard_4_band")
        assert len(bands) == 4

    def test_unknown_preset_returns_default(self):
        bands = get_preset_bands("nonexistent")
        assert len(bands) == 3  # falls back to standard_3_band


# ---------------------------------------------------------------------------
# Colour-blind-safe palette
# ---------------------------------------------------------------------------

class TestColorblindPalette:
    def test_colorblind_presets_exist(self):
        """All standard presets have colorblind counterparts."""
        for preset_name in BAND_PRESETS:
            assert preset_name in BAND_PRESETS_COLORBLIND

    def test_colorblind_same_boundaries(self):
        """Colorblind bands have same boundaries as standard bands."""
        for preset_name in BAND_PRESETS:
            std = BAND_PRESETS[preset_name]
            cb = BAND_PRESETS_COLORBLIND[preset_name]
            assert len(std) == len(cb)
            for s, c in zip(std, cb):
                assert s.min == c.min
                assert s.max == c.max
                assert s.label == c.label

    def test_colorblind_different_colors(self):
        """Colorblind colours differ from standard palette."""
        std = get_preset_bands("standard_3_band", colorblind=False)
        cb = get_preset_bands("standard_3_band", colorblind=True)
        # At least some colours must differ
        any_different = any(s.color != c.color for s, c in zip(std, cb))
        assert any_different

    def test_colorblind_uses_blue_not_green(self):
        """Colorblind On Track uses blue, not green."""
        cb = get_preset_bands("standard_3_band", colorblind=True)
        on_track = cb[2]
        assert on_track.label == "On Track"
        assert "#15" in on_track.color or "#0D" in on_track.color  # blue range

    def test_colorblind_no_red_green(self):
        """Colorblind palette avoids red and green."""
        for preset_name in BAND_PRESETS_COLORBLIND:
            for band in BAND_PRESETS_COLORBLIND[preset_name]:
                # No pure red (#D32F2F) or pure green (#388E3C)
                assert band.color != "#D32F2F"
                assert band.color != "#388E3C"

    def test_get_preset_bands_colorblind_flag(self):
        std = get_preset_bands("standard_3_band")
        cb = get_preset_bands("standard_3_band", colorblind=True)
        assert std[0].color != cb[0].color


# ---------------------------------------------------------------------------
# Band validation
# ---------------------------------------------------------------------------

class TestBandValidation:
    def test_valid_3_band(self):
        errors = validate_bands([
            {"label": "Bad", "color": "#D32F2F", "min": None, "max": 0.8},
            {"label": "Ok", "color": "#F57C00", "min": 0.8, "max": 1.0},
            {"label": "Good", "color": "#388E3C", "min": 1.0, "max": None},
        ])
        assert errors == []

    def test_valid_2_band(self):
        errors = validate_bands([
            {"label": "Bad", "color": "#D32F2F", "min": None, "max": 0.5},
            {"label": "Good", "color": "#388E3C", "min": 0.5, "max": None},
        ])
        assert errors == []

    def test_valid_5_band(self):
        errors = validate_bands([
            {"label": "A", "color": "#1", "min": None, "max": 0.2},
            {"label": "B", "color": "#2", "min": 0.2, "max": 0.4},
            {"label": "C", "color": "#3", "min": 0.4, "max": 0.6},
            {"label": "D", "color": "#4", "min": 0.6, "max": 0.8},
            {"label": "E", "color": "#5", "min": 0.8, "max": None},
        ])
        assert errors == []

    def test_too_few_bands(self):
        errors = validate_bands([
            {"label": "Only", "color": "#388E3C", "min": None, "max": None},
        ])
        assert any("Minimum 2" in e for e in errors)

    def test_too_many_bands(self):
        bands = [
            {"label": f"Band{i}", "color": f"#{i}", "min": i * 0.1 if i > 0 else None, "max": (i + 1) * 0.1 if i < 5 else None}
            for i in range(6)
        ]
        errors = validate_bands(bands)
        assert any("Maximum 5" in e for e in errors)

    def test_first_band_explicit_min_is_valid(self):
        errors = validate_bands([
            {"label": "Bad", "color": "#D32F2F", "min": 0.0, "max": 0.5},
            {"label": "Good", "color": "#388E3C", "min": 0.5, "max": None},
        ])
        assert errors == []

    def test_last_band_explicit_max_is_valid(self):
        errors = validate_bands([
            {"label": "Bad", "color": "#D32F2F", "min": None, "max": 0.5},
            {"label": "Good", "color": "#388E3C", "min": 0.5, "max": 1.0},
        ])
        assert errors == []

    def test_fully_bounded_bands_are_valid(self):
        errors = validate_bands([
            {"label": "Off Target", "color": "#D32F2F", "min": 0, "max": 50},
            {"label": "Near Target", "color": "#F57C00", "min": 50, "max": 80},
            {"label": "On Track", "color": "#388E3C", "min": 80, "max": 100},
        ])
        assert errors == []

    def test_gap_between_bands(self):
        errors = validate_bands([
            {"label": "Bad", "color": "#D32F2F", "min": None, "max": 0.5},
            {"label": "Good", "color": "#388E3C", "min": 0.7, "max": None},
        ])
        assert any("contiguous" in e for e in errors)

    def test_missing_label(self):
        errors = validate_bands([
            {"label": "", "color": "#D32F2F", "min": None, "max": 0.5},
            {"label": "Good", "color": "#388E3C", "min": 0.5, "max": None},
        ])
        assert any("missing a label" in e for e in errors)

    def test_missing_color(self):
        errors = validate_bands([
            {"label": "Bad", "color": "", "min": None, "max": 0.5},
            {"label": "Good", "color": "#388E3C", "min": 0.5, "max": None},
        ])
        assert any("missing a color" in e for e in errors)

    def test_min_gte_max(self):
        errors = validate_bands([
            {"label": "Bad", "color": "#D32F2F", "min": None, "max": 0.5},
            {"label": "Ok", "color": "#F57C00", "min": 0.5, "max": 0.5},
            {"label": "Good", "color": "#388E3C", "min": 0.5, "max": None},
        ])
        assert any("must be less than" in e for e in errors)

    def test_empty_list(self):
        errors = validate_bands([])
        assert any("Minimum 2" in e for e in errors)

    def test_all_presets_pass_validation(self):
        """Every built-in preset should pass validation."""
        for preset_name in BAND_PRESETS:
            bands = BAND_PRESETS[preset_name]
            band_dicts = [
                {"label": b.label, "color": b.color, "min": b.min, "max": b.max}
                for b in bands
            ]
            errors = validate_bands(band_dicts)
            assert errors == [], f"Preset {preset_name} failed: {errors}"


class TestCoerceCloserAbsolute:
    """Tests for the closer+absolute coercion in the KPI API layer."""

    _ABS_BANDS = [
        {"label": "Off Target", "color": "#D32F2F", "min": None, "max": 80},
        {"label": "Near Target", "color": "#F57C00", "min": 80, "max": 90},
        {"label": "On Track", "color": "#388E3C", "min": 90, "max": None},
    ]

    def test_coerces_closer_absolute_type_and_bands_to_ratio_scale(self):
        from src.api.kpis import _coerce_closer_absolute
        data = {
            "direction": "closer_is_better",
            "presentation_meta": {
                "evaluation_type": "absolute_value",
                "bands": [dict(b) for b in self._ABS_BANDS],
            },
        }
        _coerce_closer_absolute(data)
        pm = data["presentation_meta"]
        assert pm["evaluation_type"] == "percentage_of_target"
        bands = pm["bands"]
        assert bands[0]["max"] == 0.80
        assert bands[1]["min"] == 0.80
        assert bands[1]["max"] == 0.90
        assert bands[2]["min"] == 0.90
        assert bands[2]["max"] is None

    def test_coerced_closer_on_target_evaluates_on_track(self):
        """End-to-end: coerced bands produce correct status for on-target value."""
        from src.api.kpis import _coerce_closer_absolute
        data = {
            "direction": "closer_is_better",
            "presentation_meta": {
                "evaluation_type": "absolute_value",
                "bands": [dict(b) for b in self._ABS_BANDS],
            },
        }
        _coerce_closer_absolute(data)
        result = evaluate_threshold(
            value=100, target=100,
            direction="closer_is_better",
            evaluation_type="percentage_of_target",
            bands=data["presentation_meta"]["bands"],
        )
        assert result.status_label == "On Track"

    def test_leaves_closer_percentage_unchanged(self):
        from src.api.kpis import _coerce_closer_absolute
        ratio_bands = [
            {"label": "Off Target", "color": "#D32F2F", "min": None, "max": 0.80},
            {"label": "On Track", "color": "#388E3C", "min": 0.80, "max": None},
        ]
        data = {
            "direction": "closer_is_better",
            "presentation_meta": {"evaluation_type": "percentage_of_target", "bands": ratio_bands},
        }
        _coerce_closer_absolute(data)
        assert data["presentation_meta"]["evaluation_type"] == "percentage_of_target"
        assert data["presentation_meta"]["bands"] is ratio_bands

    def test_leaves_higher_absolute_unchanged(self):
        from src.api.kpis import _coerce_closer_absolute
        data = {
            "direction": "higher_is_better",
            "presentation_meta": {
                "evaluation_type": "absolute_value",
                "bands": [dict(b) for b in self._ABS_BANDS],
            },
        }
        _coerce_closer_absolute(data)
        assert data["presentation_meta"]["evaluation_type"] == "absolute_value"
        assert data["presentation_meta"]["bands"][0]["max"] == 80

    def test_no_presentation_meta_is_noop(self):
        from src.api.kpis import _coerce_closer_absolute
        data = {"direction": "closer_is_better"}
        _coerce_closer_absolute(data)
        assert "presentation_meta" not in data

    def test_no_direction_is_noop(self):
        from src.api.kpis import _coerce_closer_absolute
        data = {
            "presentation_meta": {
                "evaluation_type": "absolute_value",
                "bands": [dict(b) for b in self._ABS_BANDS],
            },
        }
        _coerce_closer_absolute(data)
        assert data["presentation_meta"]["evaluation_type"] == "absolute_value"

    def test_closer_ratio_bands_match_centred_preset(self):
        """Pin _CLOSER_RATIO_BANDS to the 'centred' band preset so cross-layer drift is caught."""
        from src.api.kpis import _CLOSER_RATIO_BANDS
        centred = BAND_PRESETS["centred"]
        assert len(_CLOSER_RATIO_BANDS) == len(centred)
        for api_band, preset_band in zip(_CLOSER_RATIO_BANDS, centred):
            assert api_band["label"] == preset_band.label
            assert api_band["min"] == preset_band.min
            assert api_band["max"] == preset_band.max


# ---------------------------------------------------------------------------
# Bug-6819: default-band presets and evaluator-parity regressions
# ---------------------------------------------------------------------------


class TestDefaultBandPresets:
    """Bug-6249/6250 shipped default-band presets for z_score and
    percentile_rank but had no direct regression tests verifying that a value
    in each default band yields the expected status label."""

    def test_z_score_positive_sigma_on_track(self):
        """A z-score of +1.5 with no explicit bands should resolve to the
        z_score default preset and land in the 'On Track' band (min=1.0)."""
        historical = [100.0, 100.0, 100.0, 100.0, 100.0]
        # Value 106 -> mean=100, std~=0 -> z very high. Use varied history.
        historical = [98, 100, 102, 99, 101]  # mean=100, std~1.58
        # Value ~102.37 -> z ~ +1.5
        import statistics
        mean = statistics.mean(historical)
        std = statistics.pstdev(historical)
        value = mean + 1.5 * std

        result = evaluate_threshold(
            value, None,
            evaluation_type="z_score",
            direction="higher_is_better",
            historical_values=historical,
            # No bands -> use z_score default preset
        )
        assert result.status_label == "On Track"
        assert result.status == 1

    def test_z_score_negative_sigma_off_target(self):
        """A z-score of -1.5 with no explicit bands should land in 'Off Target'
        (max=-1.0 in the z_score default preset)."""
        historical = [98, 100, 102, 99, 101]
        import statistics
        mean = statistics.mean(historical)
        std = statistics.pstdev(historical)
        value = mean - 1.5 * std

        result = evaluate_threshold(
            value, None,
            evaluation_type="z_score",
            direction="higher_is_better",
            historical_values=historical,
        )
        assert result.status_label == "Off Target"
        assert result.status == -1

    def test_z_score_near_mean_near_target(self):
        """A z-score of ~0 should land in 'Near Target' (-1.0 to 1.0)."""
        historical = [98, 100, 102, 99, 101]
        import statistics
        mean = statistics.mean(historical)

        result = evaluate_threshold(
            mean, None,
            evaluation_type="z_score",
            direction="higher_is_better",
            historical_values=historical,
        )
        assert result.status_label == "Near Target"
        assert result.status == 0

    def test_percentile_rank_top_quartile_on_track(self):
        """Percentile >= 50 with default bands should be 'On Track'."""
        peers = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        result = evaluate_threshold(
            95, None,
            evaluation_type="percentile_rank",
            direction="higher_is_better",
            peer_values=peers,
        )
        assert result.status_label == "On Track"
        assert result.status == 1

    def test_percentile_rank_bottom_quartile_off_target(self):
        """Percentile < 25 with default bands should be 'Off Target'."""
        peers = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        result = evaluate_threshold(
            5, None,
            evaluation_type="percentile_rank",
            direction="higher_is_better",
            peer_values=peers,
        )
        assert result.status_label == "Off Target"
        assert result.status == -1


class TestEvaluatorParity:
    """Bug-6250 parity: evaluate_threshold must produce identical results
    regardless of whether bands are passed explicitly or derived from the
    default preset lookup. This catches drift between the preset data and the
    evaluator's band-matching logic."""

    def test_explicit_vs_default_z_score_same_result(self):
        """Passing the z_score preset bands explicitly should yield the same
        ThresholdResult as letting the evaluator pick the default."""
        historical = [98, 100, 102, 99, 101]
        value = 105.0  # above mean -> positive z

        result_default = evaluate_threshold(
            value, None,
            evaluation_type="z_score",
            direction="higher_is_better",
            historical_values=historical,
        )
        result_explicit = evaluate_threshold(
            value, None,
            evaluation_type="z_score",
            direction="higher_is_better",
            historical_values=historical,
            bands=[
                {"label": b.label, "color": b.color, "min": b.min, "max": b.max}
                for b in get_preset_bands("z_score")
            ],
        )
        assert result_default.status == result_explicit.status
        assert result_default.status_label == result_explicit.status_label
        assert result_default.ratio == pytest.approx(result_explicit.ratio)

    def test_explicit_vs_default_percentile_rank_same_result(self):
        """Passing the percentile_rank preset bands explicitly should yield
        the same ThresholdResult as letting the evaluator pick the default."""
        peers = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        value = 75.0

        result_default = evaluate_threshold(
            value, None,
            evaluation_type="percentile_rank",
            direction="higher_is_better",
            peer_values=peers,
        )
        result_explicit = evaluate_threshold(
            value, None,
            evaluation_type="percentile_rank",
            direction="higher_is_better",
            peer_values=peers,
            bands=[
                {"label": b.label, "color": b.color, "min": b.min, "max": b.max}
                for b in get_preset_bands("percentile_rank")
            ],
        )
        assert result_default.status == result_explicit.status
        assert result_default.status_label == result_explicit.status_label
        assert result_default.ratio == pytest.approx(result_explicit.ratio)
