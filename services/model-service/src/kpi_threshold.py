"""KPI threshold evaluation (RAG status).

Implements Section 6 of the KPI requirements specification:
- 3 direction modes (higher_is_better, lower_is_better, closer_is_better)
- 6 evaluation types (percentage_of_target, absolute_value, absolute_variance,
  percentage_variance, z_score, percentile_rank)
- Band matching with [min, max) interval convention
- Band presets (Standard 3/4-band, Tight tolerance, Centred)
- Colour-blind-safe palette variants (blue/orange)
- Band validation (2-5 bands, monotonically increasing boundaries)

All ratio computations normalise so that higher ratio = better performance,
regardless of direction.  Band ordering therefore always follows the
convention: first band = worst (status -1), last band = best (status 1).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Band:
    """A single threshold band."""
    label: str
    color: str
    min: Optional[float]  # inclusive lower bound; None = -infinity
    max: Optional[float]  # exclusive upper bound; None = +infinity


@dataclass(frozen=True)
class ThresholdResult:
    """Result of threshold evaluation."""
    status: Optional[int]       # 1=good, 0=warning, -1=bad, None=no data
    status_label: Optional[str]
    status_color: Optional[str]
    ratio: Optional[float]      # computed ratio/variance value used for band matching
    band_index: Optional[int]   # index into bands list; None if no match
    # Bug-1226: the exact bands this evaluation matched against — either the
    # caller's custom bands or the resolved default preset. The gauge plots its
    # needle from ``ratio`` against these SAME bands so the needle, band colour
    # and status badge cannot disagree (single authoritative scale).
    bands_used: Optional[list[Band]] = None


NO_DATA_RESULT = ThresholdResult(
    status=None,
    status_label="No Data",
    status_color="#9E9E9E",  # gray
    ratio=None,
    band_index=None,
)


# ---------------------------------------------------------------------------
# Band validation
# ---------------------------------------------------------------------------

class BandValidationError(ValueError):
    """Raised when a band configuration is invalid."""


def validate_bands(bands: list[dict]) -> list[str]:
    """Validate a band configuration and return a list of error messages.

    Rules:
    - Must have 2-5 bands.
    - Boundaries must be monotonically increasing.
    - First band min must be None (-inf), last band max must be None (+inf).
    - Bands must not overlap or leave gaps.
    - Every band must have a label and a color.

    Returns an empty list if valid.
    """
    errors: list[str] = []

    if len(bands) < 2:
        errors.append("Minimum 2 bands required.")
    if len(bands) > 5:
        errors.append("Maximum 5 bands allowed.")

    if not bands:
        return errors

    # Note: first min and last max may be explicit values (absolute_value mode)
    # or null (ratio/percentage modes). Both are valid.

    # Check labels and colors
    for i, band in enumerate(bands):
        if not band.get("label"):
            errors.append(f"Band {i} is missing a label.")
        if not band.get("color"):
            errors.append(f"Band {i} is missing a color.")

    # Check monotonically increasing boundaries and continuity
    prev_max: Optional[float] = None
    for i, band in enumerate(bands):
        band_min = band.get("min")
        band_max = band.get("max")

        # Check continuity: band_min should equal previous band_max
        if i > 0:
            if band_min != prev_max:
                errors.append(
                    f"Band {i} min ({band_min}) does not match "
                    f"band {i - 1} max ({prev_max}). "
                    "Bands must be contiguous."
                )

        # Check that min < max within the band
        if band_min is not None and band_max is not None:
            if band_min >= band_max:
                errors.append(
                    f"Band {i} min ({band_min}) must be less than "
                    f"max ({band_max})."
                )

        prev_max = band_max

    return errors


# ---------------------------------------------------------------------------
# Direction-specific ratio computation
# ---------------------------------------------------------------------------

def _compute_ratio(
    value: float,
    target: float,
    direction: str,
    evaluation_type: str,
) -> Optional[float]:
    """Compute the ratio/variance value for band matching.

    Returns None if the computation is undefined (e.g., division by zero).
    """
    if evaluation_type == "absolute_variance":
        # F-017-01 / F-103-06: variance must be DIRECTION-AWARE. A *favourable*
        # variance (beating the goal) is good; only closer-is-better treats a
        # deviation in either direction as bad. Returning an unsigned deviation
        # for a directional KPI painted a cost KPI that beat budget by 20% the
        # same red as one 20% over budget. Signed favourable variance (positive =
        # beating) mirrors ``kpi_formatter.format_variance`` so the RAG colour and
        # the signed variance number can never disagree on the same card.
        if direction == "lower_is_better":
            return target - value            # + when the actual is under target
        if direction == "closer_is_better":
            return abs(value - target)       # any deviation is bad (unsigned)
        return value - target                # higher_is_better: + when exceeding

    if evaluation_type == "percentage_variance":
        if target == 0:
            return None
        if direction == "lower_is_better":
            return (target - value) / abs(target)
        if direction == "closer_is_better":
            return abs(value - target) / abs(target)
        return (value - target) / abs(target)

    # percentage_of_target is the default
    if direction == "higher_is_better":
        if target == 0:
            return None
        return value / target

    if direction == "lower_is_better":
        if value == 0 and target == 0:
            # Both zero: perfect score (e.g. zero errors against zero-error target)
            return 1.0
        if value <= 0 and target > 0:
            # Bug-5688 / Bug-7223: value<=0 with lower_is_better means at or
            # below zero of the bad thing being measured (e.g. zero errors,
            # negative cost = credit).  This is the best possible outcome
            # regardless of the target, not "no data".  target/value is either
            # undefined (value=0) or NEGATIVE (value<0), which would flip the
            # ratio's sign and misclassify a best-case outcome as worst-band.
            # Return a large finite ratio that will always land in the best
            # (highest) band.  1e6 is safely finite (passes the isinf guard)
            # and exceeds any reasonable band boundary.
            return 1e6
        if value <= 0 and target <= 0:
            # Bug-7223 R1: both negative — lower is still better, so a value
            # further below zero beats a target further below zero.  Use
            # value/target: when value is more negative than target (better),
            # value/target > 1 (best band); when value is less negative
            # (worse), value/target < 1 (lower bands).  Guard div-by-zero.
            if target == 0:
                return None
            return value / target
        return target / value

    if direction == "closer_is_better":
        if target == 0:
            return None
        return 1.0 - abs(value - target) / abs(target)

    # Fallback to higher_is_better
    if target == 0:
        return None
    return value / target


def _compute_z_score(
    value: float,
    historical_values: list[float],
) -> Optional[float]:
    """Compute z-score from historical values.

    z = (value - mean) / stddev
    Returns None if insufficient data or zero standard deviation.
    """
    if len(historical_values) < 2:
        return None
    mean = sum(historical_values) / len(historical_values)
    variance = sum((x - mean) ** 2 for x in historical_values) / len(historical_values)
    stddev = math.sqrt(variance)
    if stddev == 0:
        return None
    return (value - mean) / stddev


def _compute_percentile_rank(
    value: float,
    peer_values: list[float],
) -> Optional[float]:
    """Compute percentile rank within a peer group.

    Returns a value from 0 to 100. Higher = better rank.
    Returns None if peer group is empty.
    """
    if not peer_values:
        return None
    count = len(peer_values)
    rank = sum(1 for v in peer_values if v <= value)
    return (rank / count) * 100


# ---------------------------------------------------------------------------
# Band matching
# ---------------------------------------------------------------------------

def _match_band(ratio: float, bands: list[Band]) -> Optional[int]:
    """Match a ratio value to a band using [min, max) convention.

    Returns the index of the matching band, or None if no match.
    """
    for i, band in enumerate(bands):
        lower_ok = band.min is None or ratio >= band.min
        upper_ok = band.max is None or ratio < band.max
        if lower_ok and upper_ok:
            return i
    return None


def _status_from_band_index(
    band_index: int,
    num_bands: int,
) -> int:
    """Derive status integer from band position.

    The convention is: first band = worst (-1), last band = best (1),
    middle bands = warning (0).
    """
    if num_bands <= 1:
        return 0
    if band_index == 0:
        return -1
    if band_index == num_bands - 1:
        return 1
    return 0


# Known RAG colours from the standard and colour-blind band presets. Used to
# derive status for absolute_value bands, which are authored in raw-value order
# so position does NOT imply worst->best — the colour carries the intent.
_BAD_BAND_COLORS = {"#d32f2f", "#757575"}
_WARN_BAND_COLORS = {"#f57c00", "#e65100", "#9e9e9e"}
_GOOD_BAND_COLORS = {"#388e3c", "#1565c0", "#0d47a1"}


def _status_from_color(color: str) -> Optional[int]:
    """Map a band colour to a RAG status (1 good / 0 warning / -1 bad).

    Recognises the standard and colour-blind preset colours exactly, then
    falls back to a hue heuristic for custom colours. Returns None when the
    colour cannot be classified, so the caller can fall back to positional
    ordering.
    """
    c = (color or "").strip().lower()
    if c in _BAD_BAND_COLORS:
        return -1
    if c in _WARN_BAND_COLORS:
        return 0
    if c in _GOOD_BAND_COLORS:
        return 1
    h = c.lstrip("#")
    if len(h) != 6:
        return None
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return None
    # Near-grayscale (no clear hue) -> neutral/warning.
    if max(r, g, b) - min(r, g, b) <= 24:
        return 0
    mx = max(r, g, b)
    if g == mx and g >= 110:          # green-dominant -> good
        return 1
    if b == mx and b >= 120:          # blue-dominant (exceeding) -> good
        return 1
    if r == mx and g < 95 and b < 95:  # red-dominant -> bad
        return -1
    return 0  # amber / orange / yellow / other -> warning


# ---------------------------------------------------------------------------
# Main evaluation entry point
# ---------------------------------------------------------------------------

def evaluate_threshold(
    value: Optional[float],
    target: Optional[float],
    direction: str = "higher_is_better",
    evaluation_type: str = "percentage_of_target",
    bands: Optional[list[dict]] = None,
    historical_values: Optional[list[float]] = None,
    peer_values: Optional[list[float]] = None,
    colorblind: bool = False,
) -> ThresholdResult:
    """Evaluate a KPI value against threshold bands.

    Parameters
    ----------
    value : float | None
        The current KPI value.
    target : float | None
        The target value (required for most evaluation types).
    direction : str
        One of: higher_is_better, lower_is_better, closer_is_better.
    evaluation_type : str
        One of: percentage_of_target, absolute_value, absolute_variance,
        percentage_variance, z_score, percentile_rank.
    bands : list[dict] | None
        List of band dicts with keys: label, color, min, max.
        If None, a default 3-band preset is used based on direction.
    historical_values : list[float] | None
        Historical values for z_score computation.
    peer_values : list[float] | None
        Peer values for percentile_rank computation.

    Returns
    -------
    ThresholdResult
    """
    # NULL handling: value is NULL -> No Data
    if value is None:
        return NO_DATA_RESULT

    # NULL target handling depends on evaluation type
    if target is None and evaluation_type not in (
        "z_score", "percentile_rank", "absolute_value",
    ):
        return NO_DATA_RESULT

    # Handle special float values defensively
    if math.isnan(value) or math.isinf(value):
        return NO_DATA_RESULT

    # Use default bands if none provided
    # Bug-7240: when colorblind mode is active, use the colorblind-safe
    # preset so the rendered band colours actually change.
    if not bands:
        bands_list = _get_default_bands(
            direction, evaluation_type, target=target, colorblind=colorblind
        )
    else:
        bands_list = [
            Band(
                label=b.get("label", ""),
                color=b.get("color", "#9E9E9E"),
                min=b.get("min"),
                max=b.get("max"),
            )
            for b in bands
        ]

    # Compute ratio based on evaluation type
    ratio: Optional[float] = None

    if evaluation_type == "absolute_value":
        ratio = value
    elif evaluation_type == "z_score":
        ratio = _compute_z_score(value, historical_values or [])
        if ratio is not None:
            if direction == "lower_is_better":
                # For lower_is_better, a negative z-score means below mean (good).
                # Negate so that "good" maps to higher ratio matching standard bands.
                ratio = -ratio
            elif direction == "closer_is_better":
                # Bug-7224: for closer_is_better, deviation in EITHER direction
                # is bad.  A far outlier (|z| large) should classify as worst,
                # not best.  Use -abs(z) so that values near the mean (z~0)
                # score highest and far outliers score lowest.
                ratio = -abs(ratio)
    elif evaluation_type == "percentile_rank":
        ratio = _compute_percentile_rank(value, peer_values or [])
        if ratio is not None:
            if direction == "lower_is_better":
                # F-017-03: _compute_percentile_rank always treats a high value as a
                # high (good) rank. For lower_is_better (e.g. a cost KPI ranked against
                # peers) a low value is good, so invert the percentile to the
                # complement (100 - p) — mirrors the z_score negation above so standard
                # bands classify a low-cost leader as "good".
                ratio = 100.0 - ratio
            elif direction == "closer_is_better":
                # Bug-7224: for closer_is_better, rank by closeness to the
                # median (50th percentile).  Deviation from the median in
                # either direction is worse.  Transform so that the 50th
                # percentile scores highest (100) and the extremes score
                # lowest (0).  Linearly maps: p=50 -> 100, p=0 -> 0, p=100 -> 0.
                ratio = (1.0 - abs(ratio - 50.0) / 50.0) * 100.0
    else:
        ratio = _compute_ratio(value, target, direction, evaluation_type)  # type: ignore[arg-type]

    if ratio is None or math.isnan(ratio) or math.isinf(ratio):
        return NO_DATA_RESULT

    # Match against bands
    band_idx = _match_band(ratio, bands_list)
    if band_idx is None:
        return ThresholdResult(
            status=0,
            status_label="Unclassified",
            status_color="#9E9E9E",
            ratio=ratio,
            band_index=None,
            bands_used=bands_list,
        )

    matched = bands_list[band_idx]
    if evaluation_type in ("absolute_value", "absolute_variance", "percentage_variance"):
        # Bug-974 / F-017-02: these evaluation types are authored in raw-value
        # order, not direction-normalised ratio order. absolute_value bands are
        # raw measure values; absolute_variance/percentage_variance bands are
        # raw deviation (0 = perfect), so they are naturally authored best-first
        # (green deviation band at position 0). Positional "first = worst"
        # ordering inverts the status int for these types, so derive status from
        # the band's colour instead, falling back to positional ordering only
        # when the colour cannot be classified.
        status = _status_from_color(matched.color)
        if status is None:
            status = _status_from_band_index(band_idx, len(bands_list))
    else:
        status = _status_from_band_index(band_idx, len(bands_list))

    return ThresholdResult(
        status=status,
        status_label=matched.label,
        status_color=matched.color,
        ratio=ratio,
        band_index=band_idx,
        bands_used=bands_list,
    )


# ---------------------------------------------------------------------------
# Band presets
# ---------------------------------------------------------------------------

def _get_default_bands(
    direction: str,
    evaluation_type: str,
    target: Optional[float] = None,
    colorblind: bool = False,
) -> list[Band]:
    """Return default bands based on direction and evaluation type.

    Because ``_compute_ratio`` already normalises all directions so that
    higher ratio = better performance, the standard "low-is-bad /
    high-is-good" band order works for every direction.

    Variance evaluation types use direction-specific presets: a directional
    (higher/lower) variance is signed favourable (positive = beating), so the
    favourable-ordered ``variance_directional`` preset applies; closer-is-better
    variance is unsigned deviation (0 = best), so the deviation-ordered
    ``variance`` preset applies. Absolute-variance bands are further scaled into
    the measure's own units from ``|target|`` (F-017-01 / F-103-06).
    """
    if evaluation_type in ("absolute_variance", "percentage_variance"):
        # F-017-01 / F-103-06: signed favourable variance (higher/lower) needs
        # favourable-ordered bands (missing = red, at/above target = green);
        # closer-is-better keeps the deviation-ordered "variance" preset
        # (0 = perfect green, badness increasing upward).
        if direction in ("higher_is_better", "lower_is_better"):
            preset = get_preset_bands("variance_directional", colorblind=colorblind)
        else:
            preset = get_preset_bands("variance", colorblind=colorblind)
        if evaluation_type == "absolute_variance":
            # The preset boundaries are authored on a fraction-of-target scale
            # (e.g. 0.20 = 20% of target). absolute_variance ratios are in the
            # measure's raw units, so scale the finite boundaries by |target|.
            # Without this a currency deviation of 20,000 is compared against a
            # 0.20 band edge and every currency KPI paints red (the F-017-01 bug).
            scale = abs(target) if target not in (None, 0) else 1.0
            preset = [
                Band(
                    label=b.label,
                    color=b.color,
                    min=None if b.min is None else b.min * scale,
                    max=None if b.max is None else b.max * scale,
                )
                for b in preset
            ]
        return preset

    if evaluation_type == "z_score":
        # Bug-6249: a z-score ranges roughly -3..+3 centred on 0, not around
        # 1.0. The standard 0.80/1.00 band scale mis-classifies every z-score
        # (a value one stddev above the mean lands in "Off Target"). Use a
        # sigma-scaled preset. _compute_z_score already negates for
        # lower_is_better, so higher ratio = better here for every direction.
        # Bug-7224 R1: closer_is_better uses -abs(z) which outputs (-inf, 0].
        # The standard z_score preset (On Track >= 1.0) is unreachable, so use
        # a dedicated closer-deviation preset calibrated for that range.
        if direction == "closer_is_better":
            return get_preset_bands("z_score_closer", colorblind=colorblind)
        return get_preset_bands("z_score", colorblind=colorblind)

    if evaluation_type == "percentile_rank":
        # Bug-6249: a percentile rank ranges 0..100. On the 0.80/1.00 scale
        # every rank >= 1 lands in "On Track", so the badge is meaningless.
        # _compute_percentile_rank (and the lower_is_better complement) keeps
        # higher = better, so a 0..100 preset with 25/50 breakpoints applies.
        # Bug-7224 R1: closer_is_better uses closeness-to-median transform
        # which outputs [0, 100], so the standard percentile_rank preset works.
        return get_preset_bands("percentile_rank", colorblind=colorblind)

    if direction == "closer_is_better":
        return get_preset_bands("centred", colorblind=colorblind)

    # higher_is_better and lower_is_better both use the same bands
    # because _compute_ratio already inverts the ratio for lower_is_better.
    return get_preset_bands("standard_3_band", colorblind=colorblind)


# ---------------------------------------------------------------------------
# Band presets
# ---------------------------------------------------------------------------

# Standard palette (red / orange / green / blue)
BAND_PRESETS: dict[str, list[Band]] = {
    "standard_3_band": [
        Band(label="Off Target",  color="#D32F2F", min=None, max=0.80),
        Band(label="Near Target", color="#F57C00", min=0.80, max=1.00),
        Band(label="On Track",    color="#388E3C", min=1.00, max=None),
    ],
    "standard_4_band": [
        Band(label="Critical",  color="#D32F2F", min=None, max=0.70),
        Band(label="Warning",   color="#F57C00", min=0.70, max=0.90),
        Band(label="On Track",  color="#388E3C", min=0.90, max=1.10),
        Band(label="Exceeding", color="#1565C0", min=1.10, max=None),
    ],
    "tight_tolerance": [
        Band(label="Off Target",  color="#D32F2F", min=None, max=0.95),
        Band(label="Near Target", color="#F57C00", min=0.95, max=1.00),
        Band(label="On Track",    color="#388E3C", min=1.00, max=None),
    ],
    "centred": [
        Band(label="Off Target",  color="#D32F2F", min=None, max=0.80),
        Band(label="Near Target", color="#F57C00", min=0.80, max=0.90),
        Band(label="On Track",    color="#388E3C", min=0.90, max=None),
    ],
    # F-017-02: deviation-ordered preset for absolute_variance /
    # percentage_variance. The ratio is raw deviation (0 = perfect), so the
    # green "On Track" band sits at the low end and badness increases upward.
    # For percentage_variance the boundaries read as 10% / 20% deviation; for
    # absolute_variance they are raw units (use custom bands for absolute).
    "variance": [
        Band(label="On Track",    color="#388E3C", min=None, max=0.10),
        Band(label="Near Target", color="#F57C00", min=0.10, max=0.20),
        Band(label="Off Target",  color="#D32F2F", min=0.20, max=None),
    ],
    # F-017-01 / F-103-06: directional (higher/lower) variance is SIGNED
    # favourable — positive = beating the goal. Bands are favourable-ordered
    # around zero: missing by more than 20% is red, missing up to 20% is amber,
    # at or above target (variance >= 0) is green. For absolute_variance the
    # finite boundaries are scaled by |target| in _get_default_bands.
    "variance_directional": [
        Band(label="Off Target",  color="#D32F2F", min=None, max=-0.20),
        Band(label="Near Target", color="#F57C00", min=-0.20, max=0.0),
        Band(label="On Track",    color="#388E3C", min=0.0,   max=None),
    ],
    # Bug-6249: sigma-scaled preset for z_score. Ratio is a z-score (higher =
    # better after direction normalisation). Below one stddev under the mean is
    # off target; the mean band is amber; at or above one stddev over is good.
    "z_score": [
        Band(label="Off Target",  color="#D32F2F", min=None, max=-1.0),
        Band(label="Near Target", color="#F57C00", min=-1.0, max=1.0),
        Band(label="On Track",    color="#388E3C", min=1.0, max=None),
    ],
    # Bug-6249: 0..100 preset for percentile_rank (higher = better after the
    # lower_is_better complement). Below the 25th percentile is off target,
    # 25th-50th is amber, at or above the median is on track.
    "percentile_rank": [
        Band(label="Off Target",  color="#D32F2F", min=None, max=25.0),
        Band(label="Near Target", color="#F57C00", min=25.0, max=50.0),
        Band(label="On Track",    color="#388E3C", min=50.0, max=None),
    ],
    # Bug-7224 R1: closer_is_better + z_score preset. The -abs(z) transform
    # outputs (-inf, 0] where 0 is best (value at the mean). Within half a
    # sigma is on track; between 0.5 and 1.0 sigma deviation is amber; beyond
    # 1.0 sigma is off target.
    "z_score_closer": [
        Band(label="Off Target",  color="#D32F2F", min=None, max=-1.0),
        Band(label="Near Target", color="#F57C00", min=-1.0, max=-0.5),
        Band(label="On Track",    color="#388E3C", min=-0.5, max=None),
    ],
}

# Colour-blind-safe palette (blue / orange / gray).
# Same boundary values as the standard palette; only colours differ.
BAND_PRESETS_COLORBLIND: dict[str, list[Band]] = {
    "standard_3_band": [
        Band(label="Off Target",  color="#757575", min=None, max=0.80),
        Band(label="Near Target", color="#E65100", min=0.80, max=1.00),
        Band(label="On Track",    color="#1565C0", min=1.00, max=None),
    ],
    "standard_4_band": [
        Band(label="Critical",  color="#757575", min=None, max=0.70),
        Band(label="Warning",   color="#E65100", min=0.70, max=0.90),
        Band(label="On Track",  color="#1565C0", min=0.90, max=1.10),
        Band(label="Exceeding", color="#0D47A1", min=1.10, max=None),
    ],
    "tight_tolerance": [
        Band(label="Off Target",  color="#757575", min=None, max=0.95),
        Band(label="Near Target", color="#E65100", min=0.95, max=1.00),
        Band(label="On Track",    color="#1565C0", min=1.00, max=None),
    ],
    "centred": [
        Band(label="Off Target",  color="#757575", min=None, max=0.80),
        Band(label="Near Target", color="#E65100", min=0.80, max=0.90),
        Band(label="On Track",    color="#1565C0", min=0.90, max=None),
    ],
    "variance": [
        Band(label="On Track",    color="#1565C0", min=None, max=0.10),
        Band(label="Near Target", color="#E65100", min=0.10, max=0.20),
        Band(label="Off Target",  color="#757575", min=0.20, max=None),
    ],
    # F-017-01 / F-103-06: colour-blind variant of the directional variance
    # preset. Same boundaries as the standard palette; only colours differ.
    "variance_directional": [
        Band(label="Off Target",  color="#757575", min=None, max=-0.20),
        Band(label="Near Target", color="#E65100", min=-0.20, max=0.0),
        Band(label="On Track",    color="#1565C0", min=0.0,   max=None),
    ],
    # Bug-6249: colour-blind variants of the z_score / percentile_rank presets.
    # Same boundaries as the standard palette; only colours differ.
    "z_score": [
        Band(label="Off Target",  color="#757575", min=None, max=-1.0),
        Band(label="Near Target", color="#E65100", min=-1.0, max=1.0),
        Band(label="On Track",    color="#1565C0", min=1.0, max=None),
    ],
    "percentile_rank": [
        Band(label="Off Target",  color="#757575", min=None, max=25.0),
        Band(label="Near Target", color="#E65100", min=25.0, max=50.0),
        Band(label="On Track",    color="#1565C0", min=50.0, max=None),
    ],
    # Bug-7224 R1: colour-blind variant of the z_score_closer preset.
    "z_score_closer": [
        Band(label="Off Target",  color="#757575", min=None, max=-1.0),
        Band(label="Near Target", color="#E65100", min=-1.0, max=-0.5),
        Band(label="On Track",    color="#1565C0", min=-0.5, max=None),
    ],
}


def get_preset_bands(
    preset_name: str,
    colorblind: bool = False,
) -> list[Band]:
    """Return bands for a named preset.

    Parameters
    ----------
    preset_name : str
        One of the preset names (standard_3_band, standard_4_band,
        tight_tolerance, centred).
    colorblind : bool
        If True, return the colour-blind-safe variant (blue/orange/gray).
    """
    source = BAND_PRESETS_COLORBLIND if colorblind else BAND_PRESETS
    fallback = BAND_PRESETS_COLORBLIND if colorblind else BAND_PRESETS
    return list(source.get(preset_name, fallback["standard_3_band"]))


def list_presets() -> list[str]:
    """Return the list of available preset names."""
    return list(BAND_PRESETS.keys())
