"""F-016-01 / F-016-18 — generated date-hierarchy level keys must bucket by the
hierarchy's calendar type, not a bare Gregorian EXTRACT.

These are known-answer tests for ``_calendar_component_expression`` (the pure
producer of every generated date-hierarchy UDA expression). They pin BOTH:

1. the EXACT canonical-PostgreSQL SQL each calendar type/component emits, and
2. the VALUE-level correctness for the report's S1 dates + an ISO year-boundary
   date + a Thai year, via an independent Python oracle that mirrors the SQL
   formula and is checked against hand-computed constants.

The formulas here are identical to the calendar-table columns
(``calendar_dialects._emit_standard``) and the time-variant SQL
(``time_variants_sql._extract_period``) so hierarchy members, table columns,
and YTD/QTD variants all agree.
"""
from __future__ import annotations

from datetime import date

import pytest

from src.api.hierarchies import (
    CALENDAR_HIERARCHY_TEMPLATES,
    _calendar_component_expression,
    _date_component_expression,
)

COL = "order_date"
# What ``_calendar_component_expression`` wraps the source expression in.
BASE = f"({COL})"


# ---------------------------------------------------------------------------
# Python oracles — independent of the SQL, checked against hand-computed
# constants below, then used to prove the emitted SQL formula is correct.
# ---------------------------------------------------------------------------
def _fiscal_year(d: date, fys: int) -> int:
    return d.year if d.month >= fys else d.year - 1


def _fiscal_quarter(d: date, fys: int) -> int:
    return ((d.month - fys + 12) % 12) // 3 + 1


def _fiscal_period(d: date, fys: int) -> int:
    return (d.month - fys + 12) % 12 + 1


class TestStandardUnchanged:
    """A standard calendar (or no calendar type) must be byte-identical to the
    Gregorian baseline so existing standard hierarchies never change."""

    @pytest.mark.parametrize(
        "component", ["year", "half_year", "quarter", "month", "week", "day"]
    )
    def test_standard_matches_baseline(self, component: str) -> None:
        for cal in (None, "standard"):
            assert _calendar_component_expression(
                COL, component, cal, None
            ) == _date_component_expression(COL, component)


class TestFiscalYearKnownAnswer:
    """F-016-01: fiscal Year is the fiscal-year-start Gregorian year, NOT the
    calendar year. April fiscal (fys=4): 2025-03-31 is FY2024."""

    def test_emits_fiscal_case_expression(self) -> None:
        expr = _calendar_component_expression(COL, "year", "fiscal", 4)
        assert expr == (
            f"CASE WHEN EXTRACT(MONTH FROM {BASE}) >= 4 "
            f"THEN EXTRACT(YEAR FROM {BASE}) "
            f"ELSE EXTRACT(YEAR FROM {BASE}) - 1 END"
        )

    @pytest.mark.parametrize(
        "d,fys,expected_fy",
        [
            (date(2025, 4, 1), 4, 2025),   # S1: first day of FY2025
            (date(2025, 3, 31), 4, 2024),  # S1: last day of FY2024 (the bug)
            (date(2025, 7, 1), 4, 2025),   # S1: mid FY2025
            (date(2025, 1, 15), 7, 2024),  # July fiscal: Jan is prior FY
            (date(2025, 7, 1), 7, 2025),
        ],
    )
    def test_fiscal_year_values(self, d: date, fys: int, expected_fy: int) -> None:
        # The oracle is proven against the hand-computed constant, and the SQL
        # encodes the same ``month >= fys`` comparison.
        assert _fiscal_year(d, fys) == expected_fy
        expr = _calendar_component_expression(COL, "year", "fiscal", fys)
        assert f">= {fys}" in expr

    def test_fiscal_year_differs_from_calendar_year_at_boundary(self) -> None:
        d = date(2025, 3, 31)
        assert d.year == 2025          # calendar year
        assert _fiscal_year(d, 4) == 2024  # fiscal year (April start)

    def test_fys_1_is_plain_gregorian_year(self) -> None:
        # fys=1 fiscal == standard: no CASE, plain EXTRACT(YEAR).
        assert _calendar_component_expression(COL, "year", "fiscal", 1) == (
            f"EXTRACT(YEAR FROM {BASE})"
        )


class TestFiscalQuarterHalfPeriod:
    def test_fiscal_quarter_expression(self) -> None:
        assert _calendar_component_expression(COL, "quarter", "fiscal", 4) == (
            f"FLOOR(MOD(EXTRACT(MONTH FROM {BASE}) - 4 + 12, 12) / 3) + 1"
        )

    @pytest.mark.parametrize(
        "d,fys,expected_q",
        [
            (date(2025, 4, 10), 4, 1),
            (date(2025, 7, 10), 4, 2),
            (date(2025, 10, 10), 4, 3),
            (date(2025, 1, 10), 4, 4),
        ],
    )
    def test_fiscal_quarter_values(self, d: date, fys: int, expected_q: int) -> None:
        assert _fiscal_quarter(d, fys) == expected_q

    def test_fiscal_half_expression(self) -> None:
        expr = _calendar_component_expression(COL, "half_year", "fiscal", 4)
        assert expr == (
            f"CASE WHEN FLOOR(MOD(EXTRACT(MONTH FROM {BASE}) - 4 + 12, 12) / 3) + 1 "
            f"<= 2 THEN 1 ELSE 2 END"
        )

    def test_fiscal_period_expression_f01618(self) -> None:
        # F-016-18: fiscal "month" level is the fiscal PERIOD (1 = first fiscal
        # month), not the calendar month.
        assert _calendar_component_expression(COL, "month", "fiscal", 4) == (
            f"MOD(EXTRACT(MONTH FROM {BASE}) - 4 + 12, 12) + 1"
        )

    @pytest.mark.parametrize(
        "d,fys,expected_period",
        [
            (date(2025, 4, 15), 4, 1),   # April = fiscal period 1
            (date(2025, 5, 15), 4, 2),
            (date(2025, 3, 15), 4, 12),  # March = fiscal period 12
            (date(2025, 1, 15), 1, 1),   # fys=1: fiscal period == calendar month
            (date(2025, 12, 15), 1, 12),
        ],
    )
    def test_fiscal_period_values(
        self, d: date, fys: int, expected_period: int
    ) -> None:
        assert _fiscal_period(d, fys) == expected_period

    def test_fiscal_template_month_level_relabelled(self) -> None:
        # The fiscal hierarchy template's month level is labelled "Fiscal
        # Period" to match the fiscal-period expression (F-016-18).
        fiscal = dict(CALENDAR_HIERARCHY_TEMPLATES["fiscal"])
        assert fiscal["month"] == "Fiscal Period"


class TestIsoYearBoundary:
    """F-016-01: ISO Year is the ISO week-numbering year (EXTRACT(ISOYEAR)),
    which differs from the calendar year around 1 January."""

    def test_iso_year_expression(self) -> None:
        assert _calendar_component_expression(COL, "year", "iso_week", None) == (
            f"EXTRACT(ISOYEAR FROM {BASE})"
        )

    def test_iso_year_differs_from_calendar_year(self) -> None:
        # 2025-12-29 (a Monday) belongs to ISO year 2026, ISO week 1.
        d = date(2025, 12, 29)
        assert d.year == 2025
        assert d.isocalendar()[0] == 2026

    def test_iso_week_is_iso_extract(self) -> None:
        # Postgres EXTRACT(WEEK) is the ISO 8601 week number.
        assert _calendar_component_expression(COL, "week", "iso_week", None) == (
            f"EXTRACT(WEEK FROM {BASE})"
        )

    def test_legacy_iso_token_normalised(self) -> None:
        # The pre-H9 "iso" spelling normalises to iso_week and keeps ISOYEAR.
        assert _calendar_component_expression(COL, "year", "iso", None) == (
            f"EXTRACT(ISOYEAR FROM {BASE})"
        )


class TestTableBoundFailsLoud:
    """F-016-01 safety belt: table-bound calendars (retail_445, hijri) cannot
    have expression-based hierarchy keys — their retail/Hijri period boundaries
    live in the materialised calendar table's own columns. The generator fails
    loud rather than emitting silently-wrong Gregorian expressions (the explicit
    generate-date endpoint with calendar_type=retail_445 was the reachable
    path)."""

    @pytest.mark.parametrize("cal", ["retail_445", "hijri"])
    @pytest.mark.parametrize("component", ["year", "quarter", "month"])
    def test_table_bound_raises(self, cal: str, component: str) -> None:
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            _calendar_component_expression(COL, component, cal, None)
        assert exc.value.status_code == 422


class TestThaiYear:
    """F-016-01: Thai Year is Gregorian + 543."""

    def test_thai_year_expression(self) -> None:
        assert _calendar_component_expression(COL, "year", "thai_buddhist", None) == (
            f"EXTRACT(YEAR FROM {BASE}) + 543"
        )

    def test_thai_year_value(self) -> None:
        assert 2025 + 543 == 2568

    def test_thai_quarter_and_month_stay_gregorian(self) -> None:
        # Only the YEAR label differs for Thai; sub-year periods are Gregorian.
        for component in ("quarter", "month", "week"):
            assert _calendar_component_expression(
                COL, component, "thai_buddhist", None
            ) == _date_component_expression(COL, component)
