"""Select chart type from a tabular result using precedence rules.

Rules (applied in order, first match wins):
 1. rows==1, no dims → kpi (any number of measures)
 2. date_dims==1 AND cat_dims==1 AND measures==1 → multi_line
 3. date_dims>=1 AND measures>=2 AND cat_dims==0 → multi_line_wide
 3b. date_dims>=1 AND measures==1 AND cat_dims==0 → line
 4. cat_dims==1 AND measures>=2 AND date_dims==0 → grouped_bar
 4b. cat_dims>=2 AND measures>=1 AND date_dims==0 → bar/h_bar by row count
 5. cat_dims==1 AND measures==1 AND date_dims==0 AND rows>100 → None
 6. cat_dims==1 AND measures==1 AND date_dims==0 AND rows>8 → h_bar
 7. cat_dims==1 AND measures==1 AND date_dims==0 AND rows<=8 AND positive AND no sort → pie
 8. cat_dims==1 AND measures==1 AND date_dims==0 AND rows<=8 AND (negatives OR sorted) → bar
 9. cat_dims==0 AND date_dims==0 AND measures>=2 → bar (measures as categories)
10. no match → None
"""
from __future__ import annotations

import datetime
import re
from typing import Any

_DATE_RE = re.compile(
    r"^\d{4}[-/]\d{2}([-/]\d{2})?([T ]\d{2}:\d{2}(:\d{2})?)?$"  # YYYY-MM-DD with optional time
    r"|^\d{4}Q[1-4]$"                  # 2024Q1
    r"|^Q[1-4]\s+\d{4}$"               # Q1 2024
    r"|^(19|20)\d{2}$",                 # bare year (1900-2099)
    re.IGNORECASE,
)

_TEMPORAL_NAME_RE = re.compile(
    r"(?:^|[_\s])(year|month|quarter|qtr|week|day|date|period)(?:$|[_\s])",
    re.IGNORECASE,
)


def _looks_like_date(values: list[Any]) -> bool:
    sample = [v for v in values[:10] if v is not None]
    if not sample:
        return False
    if any(isinstance(v, (datetime.date, datetime.datetime)) for v in sample):
        return True
    str_sample = [str(v) for v in sample]
    return all(_DATE_RE.match(s) for s in str_sample)


def _temporal_int_column(col_name: str, values: list[Any]) -> bool:
    if not _TEMPORAL_NAME_RE.search(col_name):
        return False
    nums = []
    for v in values[:10]:
        if isinstance(v, bool):
            continue
        try:
            f = float(v)
            if f == int(f) and 0 <= f <= 9999:
                nums.append(f)
        except (TypeError, ValueError):
            pass
    return len(nums) >= 2


def _is_numeric(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def select_chart_type(
    result: dict[str, Any],
    max_rows: int = 500,
    has_sort: bool = False,
) -> str | None:
    """Return chart type string or None if result should not be charted."""
    columns: list[str] = result.get("columns", [])
    rows: list[list[Any]] = result.get("rows", [])

    if not columns or not rows:
        return None
    if len(rows) > max_rows:
        return None

    date_dims: list[int] = []
    cat_dims: list[int] = []
    measures: list[int] = []

    for idx in range(len(columns)):
        col_values = [row[idx] for row in rows[:10] if idx < len(row)]
        numeric_values = [v for v in col_values if v is not None]
        if _looks_like_date(col_values):
            date_dims.append(idx)
        elif _temporal_int_column(columns[idx], col_values):
            date_dims.append(idx)
        elif numeric_values and all(_is_numeric(v) for v in numeric_values):
            measures.append(idx)
        else:
            cat_dims.append(idx)

    n_rows = len(rows)
    n_date = len(date_dims)
    n_cat = len(cat_dims)
    n_meas = len(measures)

    # Rule 1: KPI — single row, no categorical or date dimensions.
    if n_rows == 1 and n_cat == 0 and n_date == 0:
        return "kpi"

    # Rule 2: multi_line (date + category + one measure)
    if n_date == 1 and n_cat == 1 and n_meas == 1:
        return "multi_line"

    # Rule 3: line (date dim present, no category dim)
    if n_date >= 1 and n_meas >= 2 and n_cat == 0:
        return "multi_line_wide"
    if n_date >= 1 and n_meas == 1 and n_cat == 0:
        return "line"

    # Rule 4: grouped bar (one category dim, multiple measures)
    if n_cat == 1 and n_meas >= 2 and n_date == 0:
        return "grouped_bar"

    # Rule 4b: multi-dimensional categorical (2+ cat dims)
    if n_cat >= 2 and n_meas >= 1 and n_date == 0:
        if n_rows > 100:
            return None
        if n_rows > 8:
            return "h_bar"
        if n_meas >= 2:
            return "grouped_bar"
        return "bar"

    if n_cat == 1 and n_meas == 1 and n_date == 0:
        meas_idx = measures[0]
        measure_values = [row[meas_idx] for row in rows if meas_idx < len(row)]

        # Rule 5: too many rows for a chart
        if n_rows > 100:
            return None

        # Rule 6: horizontal bar for 9-100 rows
        if n_rows > 8:
            return "h_bar"

        # Rules 7/8: small category set
        has_negatives = any(_is_numeric(v) and float(v) < 0 for v in measure_values if v is not None)
        if has_negatives or has_sort:
            return "bar"   # Rule 8: bar for negatives or ranked/sorted data
        return "pie"        # Rule 7

    # Rule 9: measures only (no dims at all)
    if n_cat == 0 and n_date == 0 and n_meas >= 2:
        return "bar"

    return None  # Rule 10
