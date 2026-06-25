"""Temporal-axis ordering helpers for shape safety and normalization."""
from __future__ import annotations

from typing import Any


MONTH_ORDER = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

WEEKDAY_ORDER_MONDAY_START = {
    "mon": 1,
    "monday": 1,
    "tue": 2,
    "tues": 2,
    "tuesday": 2,
    "wed": 3,
    "wednesday": 3,
    "thu": 4,
    "thur": 4,
    "thurs": 4,
    "thursday": 4,
    "fri": 5,
    "friday": 5,
    "sat": 6,
    "saturday": 6,
    "sun": 7,
    "sunday": 7,
}


def temporal_part_sort_value(value: Any, *, part: str, week_start: str = "monday") -> int | None:
    if value is None:
        return None
    key = str(value).strip().lower()
    if not key:
        return None
    if part in {"month", "month_name"}:
        return MONTH_ORDER.get(key) or _bounded_int(value, 1, 12)
    if part in {"weekday", "weekday_name", "day_of_week"}:
        if week_start != "monday":
            raise ValueError("only monday week-start ordering is currently supported")
        return WEEKDAY_ORDER_MONDAY_START.get(key) or _bounded_int(value, 1, 7)
    if part in {"quarter", "qtr"}:
        if key.startswith("q"):
            key = key[1:]
        return _bounded_int(key, 1, 4)
    if part in {"week", "week_number"}:
        return _bounded_int(value, 1, 53)
    if part in {"hour", "hour_of_day"}:
        return _bounded_int(value, 0, 23)
    if part in {"minute", "second"}:
        return _bounded_int(value, 0, 59)
    return None


def _bounded_int(value: Any, low: int, high: int) -> int | None:
    try:
        num = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    if low <= num <= high:
        return num
    return None
