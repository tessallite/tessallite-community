"""Calendar date range utilities for KPI time intelligence fallback.

Used by the Python-side KPI evaluator when time intelligence functions
cannot be compiled to SQL (e.g. kpi() cross-references inside time
functions). Provides date arithmetic for prior-period lookups, period-
to-date ranges, and rolling windows.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class DateRange:
    """A date range [start, end] inclusive."""
    start: date
    end: date


def prior_period_date(
    ref_date: date,
    grain: str,
    fiscal_year_start_month: int = 1,
) -> date:
    """Compute the same-position date in the prior period.

    Parameters
    ----------
    ref_date : date
        The reference date.
    grain : str
        One of 'year', 'quarter', 'month', 'week'.
    fiscal_year_start_month : int
        Fiscal year start month (1-12). Only used when grain is 'year'
        and fiscal calendar is in effect.

    Returns
    -------
    date
        The corresponding date in the prior period.
    """
    if grain == "year":
        return _shift_year(ref_date, -1)
    if grain == "quarter":
        return _shift_months(ref_date, -3)
    if grain == "month":
        return _shift_months(ref_date, -1)
    if grain == "week":
        return ref_date - timedelta(weeks=1)
    return ref_date


def period_to_date_range(
    ref_date: date,
    grain: str,
    fiscal_year_start_month: int = 1,
) -> DateRange:
    """Compute the period-to-date range ending at ref_date.

    Parameters
    ----------
    ref_date : date
        The reference date (end of range).
    grain : str
        One of 'year', 'quarter', 'month', 'week'.
    fiscal_year_start_month : int
        Fiscal year start month (1-12).

    Returns
    -------
    DateRange
        The start and end of the period-to-date range.
    """
    if grain == "year":
        start_month = fiscal_year_start_month
        if ref_date.month >= start_month:
            start = date(ref_date.year, start_month, 1)
        else:
            start = date(ref_date.year - 1, start_month, 1)
        return DateRange(start=start, end=ref_date)
    if grain == "quarter":
        q_start_month = ((ref_date.month - 1) // 3) * 3 + 1
        start = date(ref_date.year, q_start_month, 1)
        return DateRange(start=start, end=ref_date)
    if grain == "month":
        start = date(ref_date.year, ref_date.month, 1)
        return DateRange(start=start, end=ref_date)
    if grain == "week":
        # ISO week starts on Monday
        start = ref_date - timedelta(days=ref_date.weekday())
        return DateRange(start=start, end=ref_date)
    return DateRange(start=ref_date, end=ref_date)


def rolling_window_dates(
    ref_date: date,
    n: int,
    grain: str,
) -> list[date]:
    """Compute n prior period dates for a rolling window.

    Returns a list of dates from (n-1) periods ago to ref_date, one per
    period boundary. For monthly grain with n=3 and ref_date 2024-03-15,
    returns [2024-01-15, 2024-02-15, 2024-03-15].

    Parameters
    ----------
    ref_date : date
        The reference date (most recent).
    n : int
        Number of periods in the window (including current).
    grain : str
        One of 'year', 'quarter', 'month', 'week'.

    Returns
    -------
    list[date]
        Dates from oldest to newest.
    """
    dates: list[date] = []
    for i in range(n - 1, -1, -1):
        if grain == "year":
            dates.append(_shift_year(ref_date, -i))
        elif grain == "quarter":
            dates.append(_shift_months(ref_date, -i * 3))
        elif grain == "month":
            dates.append(_shift_months(ref_date, -i))
        elif grain == "week":
            dates.append(ref_date - timedelta(weeks=i))
        else:
            dates.append(ref_date)
    return dates


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _shift_year(d: date, years: int) -> date:
    """Shift a date by a number of years, clamping to month end."""
    target_year = d.year + years
    try:
        return d.replace(year=target_year)
    except ValueError:
        # Feb 29 in a non-leap year
        import calendar
        last_day = calendar.monthrange(target_year, d.month)[1]
        return d.replace(year=target_year, day=min(d.day, last_day))


def _shift_months(d: date, months: int) -> date:
    """Shift a date by a number of months, clamping to month end."""
    import calendar
    total_months = (d.year * 12 + d.month - 1) + months
    new_year = total_months // 12
    new_month = total_months % 12 + 1
    last_day = calendar.monthrange(new_year, new_month)[1]
    return date(new_year, new_month, min(d.day, last_day))
