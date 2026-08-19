"""Bug-7197: calendar coverage interior gap detection.

The coverage endpoint must detect interior gaps (missing dates within the
calendar range), not just outer boundary mismatches.
"""
import pytest

from src.api.calendar import CalendarCoverageResponse, _build_gap_count_sql


pytestmark = pytest.mark.unit


def test_build_gap_count_sql_produces_count_distinct():
    """The gap-count SQL must use COUNT(DISTINCT date_col) with a bounded
    WHERE clause so interior gaps are detectable."""
    sql = _build_gap_count_sql(
        "postgresql",
        table_name="public.cal_table",
        date_col="date_key",
        range_lo="2023-01-01",
        range_hi="2023-12-31",
    )
    assert "COUNT(DISTINCT" in sql.upper() or "count(DISTINCT" in sql
    assert "date_key" in sql or '"date_key"' in sql
    assert "2023-01-01" in sql
    assert "2023-12-31" in sql


def test_coverage_response_interior_gap_fields_default_none():
    """New interior gap fields must default to None for backward
    compatibility with consumers that do not expect them."""
    resp = CalendarCoverageResponse(covered=True)
    assert resp.interior_gap is None
    assert resp.calendar_date_count is None
    assert resp.expected_date_count is None


def test_coverage_response_interior_gap_true():
    """When interior gaps are detected, the response must surface them."""
    resp = CalendarCoverageResponse(
        covered=False,
        calendar_min="2023-01-01",
        calendar_max="2023-12-31",
        fact_min="2023-01-01",
        fact_max="2023-12-31",
        gap="interior",
        interior_gap=True,
        calendar_date_count=300,
        expected_date_count=365,
        warning="interior gaps detected",
    )
    assert resp.interior_gap is True
    assert resp.calendar_date_count == 300
    assert resp.expected_date_count == 365
    assert resp.gap == "interior"
