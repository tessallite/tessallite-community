"""Tests for calendar auto-registration from table classification."""
import pytest
from unittest.mock import MagicMock


def _make_col(name: str, data_type: str = "varchar") -> MagicMock:
    col = MagicMock()
    col.column_name = name
    col.data_type = data_type
    return col


class TestDetectCalendarColumns:
    """_detect_calendar_columns matches column names to calendar slots."""

    def _detect(self, columns):
        from src.api.calendar import _detect_calendar_columns
        return _detect_calendar_columns(columns)

    def test_standard_tessallite_columns(self):
        cols = [
            _make_col("date_key", "date"),
            _make_col("year_no", "integer"),
            _make_col("half_no", "integer"),
            _make_col("quarter_no", "integer"),
            _make_col("month_no", "integer"),
            _make_col("week_no", "integer"),
            _make_col("day_no", "integer"),
        ]
        result = self._detect(cols)
        assert result["date_column"] == "date_key"
        assert result["year_column"] == "year_no"
        assert result["half_column"] == "half_no"
        assert result["quarter_column"] == "quarter_no"
        assert result["month_column"] == "month_no"
        assert result["week_column"] == "week_no"
        assert result["day_column"] == "day_no"

    def test_common_alternative_names(self):
        cols = [
            _make_col("calendar_date", "date"),
            _make_col("cal_year", "integer"),
            _make_col("cal_quarter", "integer"),
            _make_col("cal_month", "integer"),
            _make_col("cal_week", "integer"),
            _make_col("day_of_month", "integer"),
        ]
        result = self._detect(cols)
        assert result["date_column"] == "calendar_date"
        assert result["year_column"] == "cal_year"
        assert result["quarter_column"] == "cal_quarter"
        assert result["month_column"] == "cal_month"
        assert result["week_column"] == "cal_week"
        assert result["day_column"] == "day_of_month"

    def test_date_type_fallback(self):
        cols = [
            _make_col("dt", "timestamp"),
            _make_col("fiscal_year", "integer"),
        ]
        result = self._detect(cols)
        assert result["date_column"] == "dt"
        assert result["year_column"] == "fiscal_year"

    def test_empty_columns_returns_all_none(self):
        result = self._detect([])
        assert all(v is None for v in result.values())

    def test_no_false_positives_on_day(self):
        """'birthday' or 'workday' should not match day_column."""
        cols = [
            _make_col("birthday", "date"),
            _make_col("workday_flag", "boolean"),
        ]
        result = self._detect(cols)
        assert result["day_column"] is None
        assert result["date_column"] == "birthday"
