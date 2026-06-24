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
        return abs(value - target)

    if evaluation_type == "percentage_variance":
        if target == 0:
            return None
        return abs(value - target) / abs(target)

    # percentage_of_target is the default
    if direction == "higher_is_better":
        if target == 0:
            return None
        return value / target

    if direction == "lower_is_better":
        if value == 0 and target == 0:
            # Both zero: perfect score (e.g. zero errors against zero-error target)
            return 1.0
        if value == 0:
            return None
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
    if not bands:
        bands_list = _get_default_bands(direction, evaluation_type)
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
        # For lower_is_better, a negative z-score means below mean (good).
        # Negate so that "good" maps to higher ratio matching standard bands.
        if ratio is not None and direction == "lower_is_better":
            ratio = -ratio
    elif evaluation_type == "percentile_rank":
        ratio = _compute_percentile_rank(value, peer_values or [])
        # F-017-03: _compute_percentile_rank always treats a high value as a
        # high (good) rank. For lower_is_better (e.g. a cost KPI ranked against
        # peers) a low value is good, so invert the percentile to the
        # complement (100 - p) — mirrors the z_score negation above so standard
        # bands classify a low-cost leader as "good".
        if ratio is not None and direction == "lower_is_better":
            ratio = 100.0 - ratio
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
) -> list[Band]:
    """Return default bands based on direction and evaluation type.

    Because ``_compute_ratio`` already normalises all directions so that
    higher ratio = better performance, the standard "low-is-bad /
    high-is-good" band order works for every direction.

    Variance evaluation types use the ``centred`` preset because they
    measure absolute deviation, not a directional ratio.
    """
    if evaluation_type in ("absolute_variance", "percentage_variance"):
        # F-017-02: variance ratios are raw deviation (0 = perfect), so bands
        # are authored best-first and increase with badness. The "centred"
        # preset is calibrated for the closeness ratio (1 - deviation, where
        # higher = better) and inverts the meaning here — value-on-target
        # (deviation 0) classified as red. Use the deviation-ordered preset.
        return get_preset_bands("variance")

    if direction == "closer_is_better":
        return get_preset_bands("centred")

    # higher_is_better and lower_is_better both use the same bands
    # because _compute_ratio already inverts the ratio for lower_is_better.
    return get_preset_bands("standard_3_band")


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
