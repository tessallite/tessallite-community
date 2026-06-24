"""Server-side number formatting and date range extraction for narration.

Bug-216: LLM narration presents raw unformatted numbers because the
narration prompt sends Decimal / float values as-is. This module
pre-formats numbers according to the measure's format token (currency,
percent_2dp, etc.) so the LLM can quote them directly.

Bug-221: LLM narration misrepresents date ranges (e.g. "throughout 2025"
when data only covers Jan–Aug). This module extracts min/max values for
date-like columns from the result rows.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any


_DATE_COLUMN_RE = re.compile(
    r"(date|month|year|quarter|week|period|day|time)",
    re.IGNORECASE,
)


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_COUNT_SUFFIXES = ("_count", "_qty", "_quantity", "_num", "_total_count")


def format_number(
    value: Any,
    fmt: str | None,
    currency_symbol: str = "",
    currency_code: str = "",
    measure_name: str = "",
) -> str:
    """Format a numeric value according to a measure format token.

    Returns a human-readable string. Falls back to comma-separated with
    2 decimal places when the format is unknown or None. Count-like measures
    (names ending in _count, _qty, etc.) default to integer formatting.
    """
    n = _to_float(value)
    if n is None:
        return str(value) if value is not None else ""

    if fmt == "currency":
        return f"{currency_symbol}{n:,.2f}"
    # Bug-1200: the `percent` / `percent_2dp` tokens store a decimal ratio
    # (0.452 == 45.2%), per the canonical KPI formatter
    # (model-service kpi_formatter.py:128-130) and the H8 frontend renderer
    # (measureFormat.ts:76-79). Scale by 100 so narration agrees with the UI
    # for the same stored value. `percent` renders at 0 dp, `percent_2dp` at
    # 2 dp, matching the frontend exactly.
    if fmt == "percent":
        return f"{n * 100:,.0f}%"
    if fmt == "percent_2dp":
        return f"{n * 100:,.2f}%"
    if fmt == "integer" or fmt == "decimal_0":
        return f"{n:,.0f}"
    if fmt == "decimal_1":
        return f"{n:,.1f}"
    if fmt == "decimal_2" or fmt == "decimal_2dp":
        return f"{n:,.2f}"
    if fmt == "decimal_3":
        return f"{n:,.3f}"
    if fmt == "decimal_4":
        return f"{n:,.4f}"
    if fmt == "decimal_5":
        return f"{n:,.5f}"
    if fmt == "decimal_6":
        return f"{n:,.6f}"

    if fmt is None and measure_name:
        lower = measure_name.lower()
        if any(lower.endswith(s) for s in _COUNT_SUFFIXES):
            return f"{n:,.0f}"

    if abs(n) >= 1000:
        return f"{n:,.2f}"
    if n == int(n):
        return f"{int(n):,}"
    return f"{n:,.2f}"


def format_rows(
    rows: list[dict[str, Any]],
    columns: list[str],
    measure_formats: dict[str, str | None],
    currency_symbol: str = "$",
    currency_code: str = "USD",
) -> list[dict[str, str]]:
    """Return a copy of rows with numeric measure values pre-formatted.

    Non-measure columns and non-numeric values pass through as strings.
    """
    formatted: list[dict[str, str]] = []
    for row in rows:
        new_row: dict[str, str] = {}
        for col in columns:
            val = row.get(col)
            if col in measure_formats and _to_float(val) is not None:
                new_row[col] = format_number(
                    val, measure_formats[col],
                    currency_symbol=currency_symbol,
                    currency_code=currency_code,
                    measure_name=col,
                )
            else:
                new_row[col] = str(val) if val is not None else ""
        formatted.append(new_row)
    return formatted


def _is_date_value(v: Any) -> bool:
    if isinstance(v, (date, datetime)):
        return True
    if isinstance(v, str) and len(v) >= 8:
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
            return True
        except (ValueError, TypeError):
            return False
    return False


_QUARTER_RE = re.compile(
    r"^(\d{4})[- ]?Q([1-4])$|^Q([1-4])[- ]?(\d{4})$", re.IGNORECASE,
)

_MONTH_NAMES = {
    m.lower(): i
    for i, m in enumerate(
        [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ],
        1,
    )
}
_MONTH_ABBR = {m[:3]: i for m, i in _MONTH_NAMES.items()}


def _parse_sortable(v: Any) -> tuple | None:
    """Parse a value into a sortable tuple for date-range ordering.

    Returns (year, sub_period) tuples for comparable ordering, or None
    if the value cannot be parsed as a temporal value.
    """
    if isinstance(v, datetime):
        return (v.year, v.month, v.day, v.hour, v.minute, v.second)
    if isinstance(v, date):
        return (v.year, v.month, v.day)
    if isinstance(v, (int, float)) and 1900 <= v <= 2200:
        return (int(v), 0, 0)

    s = str(v).strip()
    if not s:
        return None

    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
    except (ValueError, TypeError):
        pass

    m = _QUARTER_RE.match(s)
    if m:
        year = int(m.group(1) or m.group(4))
        qtr = int(m.group(2) or m.group(3))
        return (year, qtr * 3, 0)

    parts = re.split(r"[\s,/-]+", s)
    if len(parts) == 2:
        month_str, year_str = None, None
        for p in parts:
            low = p.lower().rstrip(".")
            if low in _MONTH_NAMES:
                month_str = _MONTH_NAMES[low]
            elif low in _MONTH_ABBR:
                month_str = _MONTH_ABBR[low]
            elif p.isdigit() and 1900 <= int(p) <= 2200:
                year_str = int(p)
        if month_str and year_str:
            return (year_str, month_str, 0)

    return None


def extract_date_ranges(
    rows: list[dict[str, Any]],
    columns: list[str],
) -> dict[str, tuple[str, str]]:
    """Detect date-like columns and return {column: (min_str, max_str)}.

    A column is date-like if its name matches common date patterns or
    its values parse as dates. Sorts by parsed temporal value rather
    than string representation to handle month names and quarter labels.
    """
    candidates: list[str] = []
    for col in columns:
        if _DATE_COLUMN_RE.search(col):
            candidates.append(col)
            continue
        if rows:
            first_val = rows[0].get(col)
            if _is_date_value(first_val):
                candidates.append(col)

    result: dict[str, tuple[str, str]] = {}
    for col in candidates:
        raw_vals = [r.get(col) for r in rows if r.get(col) is not None]
        if len(raw_vals) < 2:
            continue
        parsed = []
        for v in raw_vals:
            key = _parse_sortable(v)
            if key is not None:
                parsed.append((key, str(v)))
        if len(parsed) < 2:
            sorted_strs = sorted(set(str(v) for v in raw_vals))
            if sorted_strs[0] != sorted_strs[-1]:
                result[col] = (sorted_strs[0], sorted_strs[-1])
            continue
        parsed.sort(key=lambda t: t[0])
        unique = list(dict.fromkeys(p[1] for p in parsed))
        if len(unique) >= 2:
            result[col] = (parsed[0][1], parsed[-1][1])
    return result


def build_format_hints(
    measure_formats: dict[str, str | None],
    currency_symbol: str = "",
    currency_code: str = "",
) -> str:
    """Build a concise format hint string for the narration prompt."""
    if not measure_formats:
        return ""
    lines: list[str] = []
    for name, fmt in sorted(measure_formats.items()):
        if fmt == "currency":
            if currency_code:
                lines.append(
                    f"  {name}: currency ({currency_code}, {currency_symbol} symbol, "
                    "comma separators, 2 decimal places)"
                )
            else:
                lines.append(
                    f"  {name}: numeric amount (comma separators, 2 decimal places"
                    " — do not add a currency symbol or code; quote the value exactly as shown)"
                )
        elif fmt in ("percent", "percent_2dp"):
            lines.append(f"  {name}: percentage (include % symbol)")
        elif fmt == "integer" or fmt == "decimal_0":
            lines.append(f"  {name}: whole number (no decimal places)")
        elif fmt and fmt.startswith("decimal_"):
            dp = fmt.replace("decimal_", "").replace("dp", "")
            lines.append(f"  {name}: {dp} decimal places")
        else:
            lines.append(f"  {name}: use comma separators, 2 decimal places")
    return "Measure formatting:\n" + "\n".join(lines)


def aggregate_date_ranges(
    step_summaries: list[dict],
) -> dict[str, tuple[str, str]]:
    """Collect date ranges across multiple compound step result sets."""
    per_col: dict[str, list[tuple]] = {}
    for step in step_summaries:
        cols = step.get("columns", [])
        rows = step.get("sample_rows", [])
        if not rows or not cols:
            continue
        for col, (mn, mx) in extract_date_ranges(rows, cols).items():
            for v in [mn, mx]:
                key = _parse_sortable(v)
                if key is not None:
                    per_col.setdefault(col, []).append((key, str(v)))
    result: dict[str, tuple[str, str]] = {}
    for col, pairs in per_col.items():
        if len(pairs) >= 2:
            pairs.sort(key=lambda t: t[0])
            result[col] = (pairs[0][1], pairs[-1][1])
    return result
