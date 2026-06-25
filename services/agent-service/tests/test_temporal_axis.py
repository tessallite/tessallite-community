from __future__ import annotations

import pytest

from src.planning.temporal import temporal_part_sort_value


def test_month_names_sort_by_calendar_order_not_lexical():
    values = ["March", "January", "February"]

    assert sorted(values, key=lambda v: temporal_part_sort_value(v, part="month_name")) == [
        "January",
        "February",
        "March",
    ]


def test_weekday_names_sort_by_monday_start_order():
    values = ["Wednesday", "Monday", "Sunday"]

    assert sorted(values, key=lambda v: temporal_part_sort_value(v, part="weekday_name")) == [
        "Monday",
        "Wednesday",
        "Sunday",
    ]


def test_week_numbers_and_hours_sort_numerically():
    weeks = ["10", "2", "1"]
    hours = ["10", "2", "0"]

    assert sorted(weeks, key=lambda v: temporal_part_sort_value(v, part="week")) == [
        "1",
        "2",
        "10",
    ]
    assert sorted(hours, key=lambda v: temporal_part_sort_value(v, part="hour")) == [
        "0",
        "2",
        "10",
    ]


def test_quarter_labels_sort_by_quarter_number():
    values = ["Q4", "Q1", "Q3"]

    assert sorted(values, key=lambda v: temporal_part_sort_value(v, part="quarter")) == [
        "Q1",
        "Q3",
        "Q4",
    ]


def test_unknown_or_out_of_range_temporal_parts_return_none():
    assert temporal_part_sort_value("not-a-month", part="month") is None
    assert temporal_part_sort_value("24", part="hour") is None


def test_non_monday_week_start_is_explicitly_rejected():
    with pytest.raises(ValueError, match="monday"):
        temporal_part_sort_value("Sunday", part="weekday", week_start="sunday")
