"""Tests for narration formatting (Bug-216, Bug-220, Bug-221).

Bug-216: Numbers in narration should be formatted per measure format tokens.
Bug-220: Narration prompt should prohibit LLM-computed percentages.
Bug-221: Narration prompt should include actual date range from data.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from src.narrate.formatting import (
    build_format_hints,
    extract_date_ranges,
    format_number,
    format_rows,
)

pytestmark = pytest.mark.unit


class TestFormatNumber:
    def test_currency(self):
        assert format_number(1234567.89, "currency", currency_symbol="$") == "$1,234,567.89"

    def test_currency_no_symbol(self):
        assert format_number(1234567.89, "currency") == "1,234,567.89"

    def test_currency_with_decimal(self):
        assert format_number(Decimal("9384627642323.3383"), "currency", currency_symbol="$") == "$9,384,627,642,323.34"

    def test_percent_2dp_fraction(self):
        # Bug-1200: percent_2dp stores a decimal ratio; scale by 100.
        assert format_number(0.125, "percent_2dp") == "12.50%"

    def test_percent_2dp_above_one(self):
        # 1.234 ratio == 123.40% (matches frontend measureFormat.test.ts).
        assert format_number(1.234, "percent_2dp") == "123.40%"

    def test_percent(self):
        # Bug-1200: percent stores a decimal ratio; scale by 100, 0 dp.
        assert format_number(0.5, "percent") == "50%"

    def test_percent_cross_layer_agreement(self):
        # Bug-1200: narration must render the same string the UI/KPI layer
        # produces for the same stored value. The canonical convention
        # (kpi_formatter.py:128-130, measureFormat.ts:76-79) treats a percent
        # value as a decimal ratio scaled by 100. For 0.452 the UI shows
        # "45.20%" (percent_2dp); narration must not show "0.45%".
        assert format_number(0.452, "percent_2dp") == "45.20%"
        assert format_number(0.452, "percent_2dp") != "0.45%"
        # `percent` token renders at 0 dp, matching the frontend "45%".
        assert format_number(0.452, "percent") == "45%"

    def test_integer(self):
        assert format_number(1234567, "integer") == "1,234,567"

    def test_decimal_0(self):
        assert format_number(1234.567, "decimal_0") == "1,235"

    def test_decimal_2dp(self):
        assert format_number(1234.5, "decimal_2dp") == "1,234.50"

    def test_decimal_3(self):
        assert format_number(1.23456, "decimal_3") == "1.235"

    def test_none_format_large_number(self):
        result = format_number(1234567.89, None)
        assert result == "1,234,567.89"

    def test_none_format_small_whole(self):
        assert format_number(42, None) == "42"

    def test_none_value(self):
        assert format_number(None, "currency") == ""

    def test_non_numeric(self):
        assert format_number("abc", "currency") == "abc"

    def test_decimal_type(self):
        result = format_number(Decimal("1234.56"), "currency", currency_symbol="$")
        assert result == "$1,234.56"


class TestFormatRows:
    def test_formats_measure_columns(self):
        rows = [{"month": "Jan", "revenue": Decimal("1234567.89")}]
        columns = ["month", "revenue"]
        formats = {"revenue": "currency"}
        result = format_rows(rows, columns, formats)
        assert result == [{"month": "Jan", "revenue": "$1,234,567.89"}]

    def test_leaves_non_measures_as_strings(self):
        rows = [{"country": "Germany", "count": 42}]
        columns = ["country", "count"]
        formats = {"count": "integer"}
        result = format_rows(rows, columns, formats)
        assert result[0]["country"] == "Germany"
        assert result[0]["count"] == "42"

    def test_multiple_measures(self):
        rows = [{"sales": Decimal("9999.99"), "margin": Decimal("0.15")}]
        columns = ["sales", "margin"]
        formats = {"sales": "currency", "margin": "percent_2dp"}
        result = format_rows(rows, columns, formats)
        assert result[0]["sales"] == "$9,999.99"
        # Bug-1200: 0.15 decimal ratio renders as 15.00% (matches UI/KPI).
        assert result[0]["margin"] == "15.00%"


class TestExtractDateRanges:
    def test_date_column_by_name(self):
        rows = [
            {"business_date": "2025-01-01", "revenue": 100},
            {"business_date": "2025-08-01", "revenue": 200},
        ]
        result = extract_date_ranges(rows, ["business_date", "revenue"])
        assert "business_date" in result
        assert result["business_date"] == ("2025-01-01", "2025-08-01")

    def test_month_no_column(self):
        rows = [
            {"month_no": 1, "revenue": 100},
            {"month_no": 8, "revenue": 200},
        ]
        result = extract_date_ranges(rows, ["month_no", "revenue"])
        assert "month_no" in result

    def test_no_date_columns(self):
        rows = [{"country": "DE", "count": 42}]
        result = extract_date_ranges(rows, ["country", "count"])
        assert result == {}

    def test_single_value_no_range(self):
        rows = [{"business_date": "2025-01-01", "revenue": 100}]
        result = extract_date_ranges(rows, ["business_date", "revenue"])
        assert result == {}

    def test_datetime_values(self):
        rows = [
            {"created_at": datetime(2025, 1, 1), "v": 1},
            {"created_at": datetime(2025, 6, 15), "v": 2},
        ]
        result = extract_date_ranges(rows, ["created_at", "v"])
        assert "created_at" in result

    def test_date_values_detected_by_value(self):
        rows = [
            {"when": "2025-01-15", "v": 1},
            {"when": "2025-08-20", "v": 2},
        ]
        result = extract_date_ranges(rows, ["when", "v"])
        assert "when" in result

    def test_month_names_sorted_chronologically(self):
        rows = [
            {"month": "Feb 2025", "v": 1},
            {"month": "Jan 2026", "v": 2},
            {"month": "Nov 2024", "v": 3},
        ]
        result = extract_date_ranges(rows, ["month", "v"])
        assert result["month"] == ("Nov 2024", "Jan 2026")

    def test_quarter_labels_sorted(self):
        rows = [
            {"quarter": "2025-Q3", "v": 1},
            {"quarter": "2024-Q4", "v": 2},
            {"quarter": "2025-Q1", "v": 3},
        ]
        result = extract_date_ranges(rows, ["quarter", "v"])
        assert result["quarter"] == ("2024-Q4", "2025-Q3")

    def test_integer_year_values(self):
        rows = [
            {"year": 2023, "v": 1},
            {"year": 2025, "v": 2},
            {"year": 2024, "v": 3},
        ]
        result = extract_date_ranges(rows, ["year", "v"])
        assert result["year"] == ("2023", "2025")


class TestBuildFormatHints:
    def test_currency_hint(self):
        result = build_format_hints({"revenue": "currency"})
        assert "currency" in result
        assert "revenue" in result

    def test_percent_hint(self):
        result = build_format_hints({"margin": "percent_2dp"})
        assert "percentage" in result
        assert "%" in result

    def test_empty_formats(self):
        assert build_format_hints({}) == ""


class TestNarrationPromptGuards:
    """Verify the narration prompt includes guard instructions for Bug-220/221."""

    def test_prompt_forbids_derived_calculations(self):
        from src.narrate.narrate import _build_narrate_prompt
        from src.exec.query import QueryExecution

        execution = QueryExecution(
            sql="SELECT 1",
            columns=["revenue"],
            rows=[{"revenue": 1000}],
            rows_returned=1,
            route_type="source",
            routed_sql=None,
            aggregate_id=None,
            pocket_id=None,
            execution_ms=10,
        )
        _, user_prompt = _build_narrate_prompt("sys", "q", execution)
        assert "never compute" in user_prompt.lower()
        assert "percentages" in user_prompt

    def test_prompt_forbids_date_generalisation(self):
        from src.narrate.narrate import _build_narrate_prompt
        from src.exec.query import QueryExecution

        execution = QueryExecution(
            sql="SELECT 1",
            columns=["business_date", "revenue"],
            rows=[
                {"business_date": "2025-01-01", "revenue": 100},
                {"business_date": "2025-08-01", "revenue": 200},
            ],
            rows_returned=2,
            route_type="source",
            routed_sql=None,
            aggregate_id=None,
            pocket_id=None,
            execution_ms=10,
        )
        _, user_prompt = _build_narrate_prompt("sys", "q", execution)
        assert "throughout the year" in user_prompt
        assert "Date range in the data" in user_prompt
        assert "2025-01-01" in user_prompt
        assert "2025-08-01" in user_prompt

    def test_prompt_includes_format_hints(self):
        from src.narrate.narrate import _build_narrate_prompt
        from src.exec.query import QueryExecution

        execution = QueryExecution(
            sql="SELECT 1",
            columns=["revenue"],
            rows=[{"revenue": Decimal("9384627642323.3383")}],
            rows_returned=1,
            route_type="source",
            routed_sql=None,
            aggregate_id=None,
            pocket_id=None,
            execution_ms=10,
        )
        _, user_prompt = _build_narrate_prompt(
            "sys", "q", execution,
            measure_formats={"revenue": "currency"},
        )
        assert "9,384,627,642,323.34" in user_prompt
        assert "pre-formatted" in user_prompt

    def test_compound_prompt_forbids_derived_calculations(self):
        from src.narrate.narrate import _build_compound_narrate_prompt

        _, user_prompt = _build_compound_narrate_prompt(
            "sys", "q",
            step_summaries=[],
            computed={"expression": "a + b", "label": "total", "value": 42},
        )
        assert "do not introduce any other arithmetic" in user_prompt.lower()
        assert "percentages" in user_prompt

    def test_compound_prompt_hides_intermediate_step_values(self):
        from src.narrate.narrate import _build_compound_narrate_prompt

        _, user_prompt = _build_compound_narrate_prompt(
            "sys",
            "Show the base amount to fee amount ratio",
            step_summaries=[
                {
                    "name": "base_total",
                    "columns": ["base_amount"],
                    "rows_returned": 1,
                    "sample_rows": [{"base_amount": 181901553.65}],
                },
                {
                    "name": "fee_total",
                    "columns": ["fee_amount"],
                    "rows_returned": 1,
                    "sample_rows": [{"fee_amount": 2758231.49}],
                },
            ],
            computed={
                "expression": "base_total / fee_total",
                "label": "Base amount to fee amount ratio",
                "value": 65.95,
            },
        )

        assert "65.95" in user_prompt
        assert "base_total" in user_prompt
        assert "sample_rows" not in user_prompt
        assert "181901553.65" not in user_prompt
        assert "2758231.49" not in user_prompt
        assert "internal step values" in user_prompt
        assert "remaining" in user_prompt
        assert "computed.result_rows" in user_prompt
