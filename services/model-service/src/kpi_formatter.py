"""KPI value formatting engine.

Implements Sections 7.3 and 14.3 of the KPI requirements specification:
- Format tokens: currency, currency_k, percent, percent_decimal,
  decimal_0dp/1dp/2dp, integer, custom
- Locale-aware thousand separators
- K/M/B/T abbreviations for currency_k
- NULL display with configurable null_display_value
- Variance formatting with sign and pp suffix
- Large number JSON serialization (value_str for values >= 2^53)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FormattedValue:
    """Result of formatting a KPI value."""
    display: str          # formatted string for UI display
    value_str: Optional[str]  # string representation for large numbers; None if not needed


# Maximum safe integer for IEEE 754 double precision
_MAX_SAFE_INT = 2 ** 53


# ---------------------------------------------------------------------------
# Large number serialization (Section 7.3.1)
# ---------------------------------------------------------------------------

def needs_string_serialization(value: Optional[float]) -> bool:
    """Return True if the value exceeds the safe integer range for JSON."""
    if value is None:
        return False
    return abs(value) >= _MAX_SAFE_INT


def value_str_if_needed(value: Optional[float]) -> Optional[str]:
    """Return value_str if the value needs string serialization, else None."""
    if value is None or not needs_string_serialization(value):
        return None
    # Format as integer if it is one, otherwise as float
    if value == int(value):
        return str(int(value))
    return str(value)


# ---------------------------------------------------------------------------
# Format tokens
# ---------------------------------------------------------------------------

# K/M/B/T abbreviation thresholds
_ABBREVIATIONS = [
    (1e12, "T"),
    (1e9,  "B"),
    (1e6,  "M"),
    (1e3,  "K"),
]


def _abbreviate(value: float) -> tuple[float, str]:
    """Abbreviate a number with K/M/B/T suffix."""
    abs_val = abs(value)
    for threshold, suffix in _ABBREVIATIONS:
        if abs_val >= threshold:
            return value / threshold, suffix
    return value, ""


def format_value(
    value: Optional[float],
    format_token: Optional[str] = None,
    format_custom: Optional[str] = None,
    null_display_value: str = "N/A",
    unit_label: Optional[str] = None,
    currency_symbol: str = "$",
) -> FormattedValue:
    """Format a KPI value according to the format token.

    Parameters
    ----------
    value : float | None
        The numeric value to format.
    format_token : str | None
        One of: currency, currency_k, percent, percent_decimal,
        decimal_0dp, decimal_1dp, decimal_2dp, integer, custom.
        If None, defaults to decimal_2dp.
    format_custom : str | None
        Python format string for custom formatting (e.g., "{:,.3f}").
    null_display_value : str
        String to display when value is None.
    unit_label : str | None
        Unit suffix to append (e.g., "units", "hrs").
    currency_symbol : str
        Currency symbol for currency formats.

    Returns
    -------
    FormattedValue
    """
    if value is None:
        return FormattedValue(display=null_display_value, value_str=None)

    if math.isnan(value) or math.isinf(value):
        return FormattedValue(display=null_display_value, value_str=None)

    v_str = value_str_if_needed(value)
    token = format_token or "decimal_2dp"
    result: str

    if token == "currency":
        result = f"{currency_symbol}{value:,.2f}"

    elif token == "currency_k":
        abbreviated, suffix = _abbreviate(value)
        if suffix:
            result = f"{currency_symbol}{abbreviated:,.1f}{suffix}"
        else:
            result = f"{currency_symbol}{value:,.2f}"

    elif token == "percent":
        # Value stored as decimal (0.452 = 45.2%)
        result = f"{value * 100:,.1f}%"

    elif token == "percent_decimal":
        # Value already in percentage form (45.2 = 45.2%)
        result = f"{value:,.1f}%"

    elif token == "decimal_0dp":
        result = f"{value:,.0f}"

    elif token == "decimal_1dp":
        result = f"{value:,.1f}"

    elif token == "decimal_2dp":
        result = f"{value:,.2f}"

    elif token == "integer":
        result = f"{value:,.0f}"

    elif token == "custom":
        if format_custom:
            try:
                result = format_custom.format(value)
            except (ValueError, KeyError, IndexError, AttributeError, TypeError):
                # Bug-7233: malformed format_custom (non-string type, or a
                # string whose .format() raises) must degrade gracefully
                # instead of 500-ing every evaluation of the KPI.
                result = f"{value:,.2f}"
        else:
            result = f"{value:,.2f}"

    else:
        result = f"{value:,.2f}"

    if unit_label:
        result = f"{result} {unit_label}"

    return FormattedValue(display=result, value_str=v_str)


# ---------------------------------------------------------------------------
# Variance formatting
# ---------------------------------------------------------------------------

def format_variance(
    value: Optional[float],
    target: Optional[float],
    format_token: Optional[str] = None,
    null_display_value: str = "N/A",
    currency_symbol: str = "$",
    direction: str = "higher_is_better",
) -> tuple[Optional[str], Optional[str]]:
    """Format variance as both absolute and percentage.

    For lower_is_better the sign is inverted so that positive variance
    means "beating the goal" regardless of direction.

    Returns
    -------
    tuple[str | None, str | None]
        (formatted_absolute_variance, formatted_percentage_variance)
        Both include +/- sign. Percentage uses 'pp' suffix for percentage-
        point differences when the base KPI is percentage-formatted.
    """
    if value is None or target is None:
        return None, None

    if math.isnan(value) or math.isinf(value):
        return None, None
    if math.isnan(target) or math.isinf(target):
        return None, None

    raw_variance = value - target

    if direction == "closer_is_better":
        # F-017-07: closer_is_better has no "beat/miss" meaning — any distance
        # from target is a deviation, not a signed shortfall. The old code
        # returned -abs(...), so a value inside the green "On Track" band still
        # printed a minus (e.g. green card, "-5"), which reads as missing. Show
        # the deviation MAGNITUDE with a neutral "±" (distance from target).
        magnitude = abs(raw_variance)
        token = format_token or "decimal_2dp"
        if token in ("currency", "currency_k"):
            formatted_abs = f"±{currency_symbol}{magnitude:,.2f}"
        elif token == "percent":
            formatted_abs = f"±{magnitude * 100:,.1f}pp"
        elif token == "percent_decimal":
            formatted_abs = f"±{magnitude:,.1f}pp"
        else:
            formatted_abs = f"±{magnitude:,.2f}"
        if target == 0:
            formatted_pct = None
        else:
            formatted_pct = f"±{(magnitude / abs(target)) * 100:,.1f}%"
        return formatted_abs, formatted_pct

    if direction == "lower_is_better":
        abs_variance = -raw_variance
    else:
        abs_variance = raw_variance
    sign = "+" if abs_variance >= 0 else ""

    # Format absolute variance
    token = format_token or "decimal_2dp"
    if token in ("currency", "currency_k"):
        abs_sign = "+" if abs_variance >= 0 else "-"
        formatted_abs = f"{abs_sign}{currency_symbol}{abs(abs_variance):,.2f}"
    elif token in ("percent", "percent_decimal"):
        # Variance in percentage points
        if token == "percent":
            formatted_abs = f"{sign}{abs_variance * 100:,.1f}pp"
        else:
            formatted_abs = f"{sign}{abs_variance:,.1f}pp"
    else:
        formatted_abs = f"{sign}{abs_variance:,.2f}"

    # Format percentage variance
    if target == 0:
        formatted_pct = None
    else:
        pct = (abs_variance / abs(target)) * 100
        pct_sign = "+" if pct >= 0 else ""
        formatted_pct = f"{pct_sign}{pct:,.1f}%"

    return formatted_abs, formatted_pct
