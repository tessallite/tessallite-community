"""Unit tests for kpi_formatter.py (Phase 2 — KPI v2 formatting)."""
from __future__ import annotations

import pytest

from src.kpi_formatter import (
    format_value,
    format_variance,
    needs_string_serialization,
    value_str_if_needed,
)

pytestmark = pytest.mark.unit


class TestFormatValue:
    def test_currency(self):
        result = format_value(1234.56, format_token="currency")
        assert result.display == "$1,234.56"

    def test_currency_negative(self):
        result = format_value(-1234.56, format_token="currency")
        assert result.display == "$-1,234.56"

    def test_currency_k_thousands(self):
        result = format_value(1500, format_token="currency_k")
        assert "K" in result.display
        assert "$" in result.display

    def test_currency_k_millions(self):
        result = format_value(2_500_000, format_token="currency_k")
        assert "M" in result.display

    def test_currency_k_billions(self):
        result = format_value(3_500_000_000, format_token="currency_k")
        assert "B" in result.display

    def test_currency_k_trillions(self):
        result = format_value(1_200_000_000_000, format_token="currency_k")
        assert "T" in result.display

    def test_currency_k_small_value(self):
        result = format_value(500, format_token="currency_k")
        assert result.display == "$500.00"
        assert "K" not in result.display

    def test_percent(self):
        """Value stored as decimal (0.452 = 45.2%)."""
        result = format_value(0.452, format_token="percent")
        assert result.display == "45.2%"

    def test_percent_decimal(self):
        """Value already in percentage form (45.2 = 45.2%)."""
        result = format_value(45.2, format_token="percent_decimal")
        assert result.display == "45.2%"

    def test_decimal_0dp(self):
        result = format_value(1234.567, format_token="decimal_0dp")
        assert result.display == "1,235"

    def test_decimal_1dp(self):
        result = format_value(1234.567, format_token="decimal_1dp")
        assert result.display == "1,234.6"

    def test_decimal_2dp(self):
        result = format_value(1234.567, format_token="decimal_2dp")
        assert result.display == "1,234.57"

    def test_integer(self):
        result = format_value(1234.9, format_token="integer")
        assert result.display == "1,235"

    def test_custom_format(self):
        result = format_value(3.14159, format_token="custom", format_custom="{:.4f}")
        assert result.display == "3.1416"

    def test_custom_format_invalid(self):
        """Invalid custom format should fall back to decimal_2dp."""
        result = format_value(3.14, format_token="custom", format_custom="{invalid}")
        assert result.display == "3.14"

    def test_custom_format_non_string_degrades_gracefully(self):
        """Bug-7233: a malformed format_custom (non-string type) must not 500.

        When format_custom is e.g. an integer or None-like object that lacks
        .format(), the AttributeError/TypeError must be caught and the value
        formatted with the decimal_2dp fallback.
        """
        result = format_value(42.5, format_token="custom", format_custom=12345)
        assert result.display == "42.50"

    def test_custom_format_bool_degrades_gracefully(self):
        """Bug-7233: format_custom=True should not 500."""
        result = format_value(7.0, format_token="custom", format_custom=True)
        assert result.display == "7.00"

    def test_default_format(self):
        result = format_value(1234.567)
        assert result.display == "1,234.57"  # default is decimal_2dp

    def test_null_value(self):
        result = format_value(None)
        assert result.display == "N/A"

    def test_null_value_custom_display(self):
        result = format_value(None, null_display_value="-")
        assert result.display == "-"

    def test_nan_value(self):
        result = format_value(float("nan"))
        assert result.display == "N/A"

    def test_inf_value(self):
        result = format_value(float("inf"))
        assert result.display == "N/A"

    def test_unit_label(self):
        result = format_value(42, format_token="integer", unit_label="units")
        assert result.display == "42 units"

    def test_currency_symbol(self):
        result = format_value(100, format_token="currency", currency_symbol="EUR ")
        assert result.display.startswith("EUR ")


class TestFormatVariance:
    def test_positive_variance_currency(self):
        abs_var, pct_var = format_variance(110, 100, format_token="currency")
        assert abs_var == "+$10.00"
        assert pct_var == "+10.0%"

    def test_negative_variance_currency(self):
        abs_var, pct_var = format_variance(90, 100, format_token="currency")
        assert abs_var == "-$10.00"
        assert pct_var == "-10.0%"

    def test_zero_variance(self):
        abs_var, pct_var = format_variance(100, 100)
        assert abs_var is not None
        assert "+0" in abs_var or "0.00" in abs_var
        assert pct_var == "+0.0%"

    def test_percent_variance_pp_suffix(self):
        abs_var, _ = format_variance(0.55, 0.50, format_token="percent")
        assert "pp" in abs_var  # percentage-point suffix

    def test_null_value(self):
        abs_var, pct_var = format_variance(None, 100)
        assert abs_var is None
        assert pct_var is None

    def test_null_target(self):
        abs_var, pct_var = format_variance(100, None)
        assert abs_var is None
        assert pct_var is None

    def test_zero_target(self):
        abs_var, pct_var = format_variance(100, 0)
        assert abs_var is not None
        assert pct_var is None  # can't compute percentage with zero target

    def test_lower_is_better_below_target_positive(self):
        """value=50, target=100, lower_is_better → variance should be positive (beating goal)."""
        abs_var, pct_var = format_variance(50, 100, direction="lower_is_better")
        assert abs_var is not None
        assert abs_var.startswith("+")

    def test_lower_is_better_above_target_negative(self):
        """value=150, target=100, lower_is_better → variance should be negative (missing goal)."""
        abs_var, pct_var = format_variance(150, 100, direction="lower_is_better")
        assert abs_var is not None
        assert abs_var.startswith("-")

    def test_closer_is_better_any_deviation_neutral(self):
        """F-017-07: closer_is_better has no beat/miss sign. A deviation in
        either direction is shown as a neutral '±' magnitude (distance from
        target), never a misleading minus on a green On Track card."""
        abs_over, pct_over = format_variance(120, 100, direction="closer_is_better")
        abs_under, pct_under = format_variance(80, 100, direction="closer_is_better")
        assert abs_over is not None and abs_over.startswith("±")
        assert abs_under is not None and abs_under.startswith("±")
        assert not abs_over.startswith("-") and not abs_under.startswith("-")
        # Equal distance either side of target formats identically.
        assert abs_over == abs_under
        assert pct_over == pct_under == "±20.0%"

    def test_closer_is_better_at_target_zero(self):
        """At target, deviation is zero (±0)."""
        abs_var, _ = format_variance(100, 100, direction="closer_is_better")
        assert abs_var is not None
        assert "0" in abs_var
        assert not abs_var.startswith("-")


class TestLargeNumberSerialization:
    def test_small_number_no_string(self):
        assert not needs_string_serialization(1000)
        assert value_str_if_needed(1000) is None

    def test_large_integer(self):
        large = 2**53 + 1
        assert needs_string_serialization(large)
        vs = value_str_if_needed(large)
        assert vs is not None
        assert isinstance(vs, str)

    def test_negative_large(self):
        large = -(2**53 + 1)
        assert needs_string_serialization(large)

    def test_exactly_at_boundary(self):
        at_boundary = float(2**53)
        assert needs_string_serialization(at_boundary)

    def test_below_boundary(self):
        below = float(2**53 - 1)
        assert not needs_string_serialization(below)

    def test_none_value(self):
        assert not needs_string_serialization(None)
        assert value_str_if_needed(None) is None

    def test_format_value_includes_value_str(self):
        large = float(2**53 + 1)
        result = format_value(large, format_token="integer")
        assert result.value_str is not None
