"""Tests for expression-based time-variant SQL (calendar-table-free).

Validates that VariantBinding with calendar_type (and no calendar_columns)
produces correct SQL using EXTRACT-based period boundaries.
"""
from __future__ import annotations

import pytest

from shared.semantic.time_variants_sql import (
    VariantBinding,
    VariantSqlError,
    emit_variant_expression,
    _extract_period,
    _prior_partition_keys,
)

pytestmark = pytest.mark.unit


def _bind(
    calendar_type: str = "standard",
    fiscal_year_start_month: int | None = None,
    dialect: str = "postgresql",
    partition_by: tuple[str, ...] = (),
) -> VariantBinding:
    return VariantBinding(
        base_expression="SUM(amount)",
        fact_date_column="f.order_date",
        calendar_columns=None,
        calendar_type=calendar_type,
        fiscal_year_start_month=fiscal_year_start_month,
        dialect=dialect,
        partition_by=partition_by,
    )


# ---------------------------------------------------------------------------
# _extract_period — standard calendar
# ---------------------------------------------------------------------------

class TestExtractPeriodStandard:
    def test_year(self):
        b = _bind()
        assert _extract_period(b, "year") == "EXTRACT(YEAR FROM f.order_date)"

    def test_quarter(self):
        b = _bind()
        assert _extract_period(b, "quarter") == "EXTRACT(QUARTER FROM f.order_date)"

    def test_month(self):
        b = _bind()
        assert _extract_period(b, "month") == "EXTRACT(MONTH FROM f.order_date)"

    def test_week(self):
        b = _bind()
        assert _extract_period(b, "week") == "EXTRACT(WEEK FROM f.order_date)"

    def test_date(self):
        b = _bind()
        assert _extract_period(b, "date") == "f.order_date"

    def test_half(self):
        b = _bind()
        result = _extract_period(b, "half")
        assert "EXTRACT(QUARTER FROM f.order_date)" in result
        assert "1" in result and "2" in result


# ---------------------------------------------------------------------------
# _extract_period — fiscal calendar
# ---------------------------------------------------------------------------

class TestExtractPeriodFiscal:
    def test_fiscal_year_april(self):
        b = _bind(calendar_type="fiscal", fiscal_year_start_month=4)
        result = _extract_period(b, "year")
        assert "CASE WHEN" in result
        assert "EXTRACT(MONTH FROM f.order_date) >= 4" in result
        assert "EXTRACT(YEAR FROM f.order_date) - 1" in result

    def test_fiscal_quarter_april(self):
        b = _bind(calendar_type="fiscal", fiscal_year_start_month=4)
        result = _extract_period(b, "quarter")
        assert "FLOOR" in result
        assert "MOD" in result
        assert "- 4" in result

    def test_fiscal_month_unchanged(self):
        """Fiscal calendar doesn't change month extraction."""
        b = _bind(calendar_type="fiscal", fiscal_year_start_month=4)
        assert _extract_period(b, "month") == "EXTRACT(MONTH FROM f.order_date)"

    def test_fiscal_january_start_same_as_standard(self):
        """Fiscal year starting January is identical to standard."""
        b = _bind(calendar_type="fiscal", fiscal_year_start_month=1)
        assert _extract_period(b, "year") == "EXTRACT(YEAR FROM f.order_date)"
        assert _extract_period(b, "quarter") == "EXTRACT(QUARTER FROM f.order_date)"


# ---------------------------------------------------------------------------
# _extract_period — ISO calendar
# ---------------------------------------------------------------------------

class TestExtractPeriodISO:
    def test_iso_year(self):
        b = _bind(calendar_type="iso")
        assert _extract_period(b, "year") == "EXTRACT(ISOYEAR FROM f.order_date)"


# ---------------------------------------------------------------------------
# _extract_period — unknown key
# ---------------------------------------------------------------------------

def test_extract_period_unknown_key():
    b = _bind()
    with pytest.raises(VariantSqlError, match="No expression for period key"):
        _extract_period(b, "century")


# ---------------------------------------------------------------------------
# emit_variant_expression — expression-based YTD (standard)
# ---------------------------------------------------------------------------

class TestYtdExpressionBased:
    def test_ytd_standard(self):
        b = _bind()
        sql = emit_variant_expression("ytd", b).sql
        assert "SUM(SUM(amount))" in sql
        assert "PARTITION BY" in sql
        assert "EXTRACT(YEAR FROM f.order_date)" in sql
        assert "UNBOUNDED PRECEDING" in sql

    def test_qtd_standard(self):
        b = _bind()
        sql = emit_variant_expression("qtd", b).sql
        assert "EXTRACT(QUARTER FROM f.order_date)" in sql

    def test_mtd_standard(self):
        b = _bind()
        sql = emit_variant_expression("mtd", b).sql
        assert "EXTRACT(MONTH FROM f.order_date)" in sql

    def test_wtd_standard(self):
        b = _bind()
        sql = emit_variant_expression("wtd", b).sql
        assert "EXTRACT(WEEK FROM f.order_date)" in sql


class TestYtdFiscal:
    def test_ytd_fiscal_april(self):
        b = _bind(calendar_type="fiscal", fiscal_year_start_month=4)
        sql = emit_variant_expression("ytd", b).sql
        assert "CASE WHEN" in sql
        assert ">= 4" in sql
        assert "UNBOUNDED PRECEDING" in sql


# ---------------------------------------------------------------------------
# emit_variant_expression — expression-based prior (standard)
# ---------------------------------------------------------------------------

class TestPriorExpressionBased:
    def test_prior_year_standard(self):
        b = _bind()
        sql = emit_variant_expression("prior_year", b).sql
        assert "LAG(SUM(amount), 1)" in sql
        assert "PARTITION BY" in sql
        assert "EXTRACT(MONTH FROM f.order_date)" in sql
        assert "EXTRACT(DAY FROM f.order_date)" in sql
        assert "EXTRACT(YEAR FROM f.order_date)" in sql

    def test_prior_quarter_standard(self):
        b = _bind()
        sql = emit_variant_expression("prior_quarter", b).sql
        assert "LAG(SUM(amount), 1)" in sql
        assert "EXTRACT(DAY FROM f.order_date)" in sql

    def test_prior_month_standard(self):
        b = _bind()
        sql = emit_variant_expression("prior_month", b).sql
        assert "LAG(SUM(amount), 1)" in sql

    def test_prior_week_standard(self):
        b = _bind()
        sql = emit_variant_expression("prior_week", b).sql
        assert "LAG(SUM(amount), 1)" in sql
        assert "EXTRACT(ISODOW FROM f.order_date)" in sql

    def test_prior_year_with_partition_by(self):
        """Expression-based prior supports multi-grain via LAG partition."""
        b = _bind(partition_by=("d.region",))
        sql = emit_variant_expression("prior_year", b).sql
        assert "d.region" in sql
        assert "LAG" in sql

    def test_prior_year_fiscal(self):
        b = _bind(calendar_type="fiscal", fiscal_year_start_month=7)
        sql = emit_variant_expression("prior_year", b).sql
        assert "LAG" in sql
        assert "CASE WHEN" in sql
        assert ">= 7" in sql


# ---------------------------------------------------------------------------
# emit_variant_expression — compound variants
# ---------------------------------------------------------------------------

class TestCompoundExpressionBased:
    def test_yoy_growth_standard(self):
        b = _bind()
        sql = emit_variant_expression("yoy_growth", b).sql
        assert "SUM(amount)" in sql
        assert "LAG" in sql

    def test_yoy_growth_pct_standard(self):
        b = _bind()
        sql = emit_variant_expression("yoy_growth_pct", b).sql
        assert "NULLIF" in sql

    def test_ytd_prior_year_standard(self):
        # F-015-02 ≡ F-016-01: this test previously asserted the defective
        # ``PARTITION BY (year - 1)`` shape ("- 1" + UNBOUNDED PRECEDING),
        # which only relabels partitions and returns the row's OWN YTD —
        # "YTD vs prior-year YTD" always showed zero growth. The correct
        # business outcome (prior-year YTD at the same point in time) is a
        # RANGE-offset frame over year*1000 + month*31 + day; numeric proof
        # in tessallite/tests/e2e/test_time_variants_numeric.py.
        b = _bind()
        sql = emit_variant_expression("ytd_prior_year", b).sql
        assert "EXTRACT(YEAR FROM f.order_date) * 1000" in sql
        assert "EXTRACT(MONTH FROM f.order_date) * 31 + EXTRACT(DAY FROM f.order_date)" in sql
        assert "RANGE BETWEEN 1403 PRECEDING AND 1000 PRECEDING" in sql
        assert "UNBOUNDED PRECEDING" not in sql


# ---------------------------------------------------------------------------
# Pure window variants (no calendar needed)
# ---------------------------------------------------------------------------

class TestWindowVariants:
    def test_lag_no_calendar_type(self):
        b = VariantBinding(
            base_expression="SUM(sales)",
            fact_date_column="f.dt",
            dialect="postgresql",
        )
        sql = emit_variant_expression("lag", b).sql
        assert "LAG(SUM(sales))" in sql

    def test_trailing_n(self):
        b = VariantBinding(
            base_expression="SUM(sales)",
            fact_date_column="f.dt",
            dialect="postgresql",
            n=6,
        )
        sql = emit_variant_expression("trailing_n", b).sql
        assert "SUM(SUM(sales))" in sql
        assert "6 PRECEDING" in sql

    def test_moving_avg_n(self):
        b = VariantBinding(
            base_expression="SUM(sales)",
            fact_date_column="f.dt",
            dialect="postgresql",
            n=30,
        )
        sql = emit_variant_expression("moving_avg_n", b).sql
        assert "AVG(SUM(sales))" in sql
        assert "30 PRECEDING" in sql


# ---------------------------------------------------------------------------
# Error: no calendar rules at all
# ---------------------------------------------------------------------------

def test_ytd_requires_calendar_rules():
    b = VariantBinding(
        base_expression="SUM(sales)",
        fact_date_column="f.dt",
        dialect="postgresql",
    )
    with pytest.raises(VariantSqlError, match="requires calendar rules"):
        emit_variant_expression("ytd", b)


# ---------------------------------------------------------------------------
# Backward compat: calendar_columns still preferred when present
# ---------------------------------------------------------------------------

def test_calendar_columns_preferred_over_expression():
    b = VariantBinding(
        base_expression="SUM(amount)",
        fact_date_column="f.order_date",
        calendar_alias="cal",
        calendar_columns={"year": "year_no", "quarter": "quarter_no"},
        calendar_type="standard",
        dialect="postgresql",
    )
    sql = emit_variant_expression("ytd", b).sql
    assert '"cal"."year_no"' in sql
    assert "EXTRACT" not in sql


# ---------------------------------------------------------------------------
# Fiscal half uses fiscal quarter, not Gregorian quarter
# ---------------------------------------------------------------------------

class TestFiscalHalf:
    def test_fiscal_half_uses_fiscal_quarter(self):
        b = _bind(calendar_type="fiscal", fiscal_year_start_month=4)
        result = _extract_period(b, "half")
        assert "MOD" in result
        assert "- 4" in result
        assert "CASE WHEN" in result

    def test_standard_half_uses_gregorian_quarter(self):
        b = _bind()
        result = _extract_period(b, "half")
        assert "EXTRACT(QUARTER FROM f.order_date)" in result
        assert "MOD" not in result

    def test_fiscal_jan_start_half_same_as_standard(self):
        b = _bind(calendar_type="fiscal", fiscal_year_start_month=1)
        result = _extract_period(b, "half")
        assert "EXTRACT(QUARTER FROM f.order_date)" in result
        assert "MOD" not in result


# ---------------------------------------------------------------------------
# prior_* with calendar columns + multi-grain (was blocked, now works)
# ---------------------------------------------------------------------------

class TestPriorCalendarMultiGrain:
    def test_prior_year_calendar_with_partition(self):
        b = VariantBinding(
            base_expression="SUM(amount)",
            fact_date_column="f.order_date",
            calendar_alias="cal",
            calendar_columns={"date": "date_key", "year": "year_no",
                              "month": "month_no"},
            calendar_type="standard",
            dialect="postgresql",
            partition_by=("d.country",),
        )
        sql = emit_variant_expression("prior_year", b).sql
        assert "LAG(" in sql
        assert "d.country" in sql
        assert '"cal"."month_no"' in sql
        assert '"cal"."year_no"' in sql

    def test_yoy_growth_calendar_with_partition(self):
        b = VariantBinding(
            base_expression="SUM(amount)",
            fact_date_column="f.order_date",
            calendar_alias="cal",
            calendar_columns={"date": "date_key", "year": "year_no",
                              "month": "month_no"},
            calendar_type="standard",
            dialect="postgresql",
            partition_by=("d.country",),
        )
        sql = emit_variant_expression("yoy_growth", b).sql
        assert "d.country" in sql
        assert "LAG(" in sql

    def test_prior_quarter_calendar_with_partition(self):
        b = VariantBinding(
            base_expression="SUM(amount)",
            fact_date_column="f.order_date",
            calendar_alias="cal",
            calendar_columns={"date": "date_key", "year": "year_no",
                              "quarter": "quarter_no"},
            calendar_type="standard",
            dialect="postgresql",
            partition_by=("d.region", "d.channel"),
        )
        sql = emit_variant_expression("prior_quarter", b).sql
        assert "LAG(" in sql
        assert "d.region" in sql
        assert "d.channel" in sql
