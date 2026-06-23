from shared.semantic.calendar_dialects import emit_calendar_ddl
from datetime import date
import pytest


# ---------------------------------------------------------------------------
# Gregorian baseline (fiscal_year_start_month=1)
# ---------------------------------------------------------------------------

def test_standard_calendar_unchanged():
    ddl = emit_calendar_ddl("postgresql", "dim_date", date(2020, 1, 1), date(2030, 12, 31))
    assert "fiscal" not in ddl.lower()
    assert "year_no" in ddl


def test_fiscal_calendar_shifts_year():
    ddl = emit_calendar_ddl(
        "postgresql", "dim_date", date(2020, 1, 1), date(2030, 12, 31),
        fiscal_year_start_month=4,
    )
    assert "CASE WHEN" in ddl
    assert ">= 4" in ddl
    assert "year_no" in ddl


def test_fiscal_quarter_correct_for_april_start():
    ddl = emit_calendar_ddl(
        "postgresql", "dim_date", date(2020, 1, 1), date(2030, 12, 31),
        fiscal_year_start_month=4,
    )
    assert "quarter_no" in ddl
    assert "% 12" in ddl


def test_invalid_month_raises():
    with pytest.raises(ValueError, match="fiscal_year_start_month"):
        emit_calendar_ddl(
            "postgresql", "dim_date", date(2020, 1, 1), date(2030, 12, 31),
            fiscal_year_start_month=13,
        )


# ---------------------------------------------------------------------------
# BigQuery dialect
# ---------------------------------------------------------------------------

def test_bigquery_standard_no_fiscal():
    ddl = emit_calendar_ddl("bigquery", "dim_date", date(2020, 1, 1), date(2030, 12, 31))
    assert "EXTRACT(YEAR FROM date_key)" in ddl
    assert "fiscal" not in ddl.lower()


def test_bigquery_fiscal_shifts_year():
    ddl = emit_calendar_ddl(
        "bigquery", "dim_date", date(2020, 1, 1), date(2030, 12, 31),
        fiscal_year_start_month=4,
    )
    assert "CASE WHEN" in ddl
    assert ">= 4" in ddl
    assert "MOD(" in ddl


# ---------------------------------------------------------------------------
# Spark dialect
# ---------------------------------------------------------------------------

def test_spark_standard_no_fiscal():
    ddl = emit_calendar_ddl("hadoop_spark", "dim_date", date(2020, 1, 1), date(2030, 12, 31))
    assert "EXTRACT(YEAR FROM date_key)" in ddl
    assert "fiscal" not in ddl.lower()


def test_spark_fiscal_shifts_year():
    ddl = emit_calendar_ddl(
        "hadoop_spark", "dim_date", date(2020, 1, 1), date(2030, 12, 31),
        fiscal_year_start_month=7,
    )
    assert "CASE WHEN" in ddl
    assert ">= 7" in ddl
    assert "% 12" in ddl


# ---------------------------------------------------------------------------
# Boundary: month=12 (December fiscal year start)
# ---------------------------------------------------------------------------

def test_december_fiscal_start_postgres():
    ddl = emit_calendar_ddl(
        "postgresql", "dim_date", date(2020, 1, 1), date(2030, 12, 31),
        fiscal_year_start_month=12,
    )
    assert "CASE WHEN" in ddl
    assert ">= 12" in ddl


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_month_zero_raises():
    with pytest.raises(ValueError, match="fiscal_year_start_month"):
        emit_calendar_ddl(
            "postgresql", "dim_date", date(2020, 1, 1), date(2030, 12, 31),
            fiscal_year_start_month=0,
        )


def test_end_before_start_raises():
    with pytest.raises(ValueError, match="end_date"):
        emit_calendar_ddl("postgresql", "dim_date", date(2030, 1, 1), date(2020, 1, 1))


def test_unsupported_dialect_raises():
    with pytest.raises(ValueError, match="dialect"):
        emit_calendar_ddl("mysql", "dim_date", date(2020, 1, 1), date(2030, 12, 31))
