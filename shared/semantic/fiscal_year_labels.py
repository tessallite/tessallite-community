"""Canonical fiscal-year caption vocabulary.

The calendar key remains numeric (``year_no``/``retail_year``); this module
owns only the caption contract shared by settings validation, Python row
materialisation, and dialect-specific DDL emitters.
"""
from __future__ import annotations

from typing import Any

FISCAL_YEAR_LABEL_SETTING = "calendar.fiscal_year_label_format"
FISCAL_YEAR_LABEL_FORMATS = (
    "start_year",
    "span_short",
    "span_long",
    "span_fy",
    "end_year",
)
DEFAULT_FISCAL_YEAR_LABEL_FORMAT = "start_year"


def validate_fiscal_year_label_format(value: Any) -> None:
    """Validate one registry value shaped as ``{"format": token}``.

    The setting is deliberately a small object so future calendar presentation
    options can be added without changing the tenant-settings storage shape.
    For this contract, however, extra keys are rejected: accepting and ignoring
    them would make a tenant believe a value was active when it was not.
    """
    if not isinstance(value, dict):
        raise ValueError(
            f"{FISCAL_YEAR_LABEL_SETTING} must be an object with a 'format' field"
        )
    if set(value) != {"format"}:
        raise ValueError(
            f"{FISCAL_YEAR_LABEL_SETTING} accepts only the 'format' field"
        )
    token = value.get("format")
    if token not in FISCAL_YEAR_LABEL_FORMATS:
        raise ValueError(
            f"format must be one of {list(FISCAL_YEAR_LABEL_FORMATS)!r} "
            f"(got {token!r})"
        )


def extract_fiscal_year_label_format(value: Any) -> str:
    """Return the validated token from a stored setting or a raw token.

    ``emit_calendar_ddl`` is a pure function and accepts the token directly,
    while the resolver returns the JSON object. Keeping both forms at this
    boundary avoids duplicated shape handling in callers and still refuses
    unknown values loudly.
    """
    if isinstance(value, dict):
        validate_fiscal_year_label_format(value)
        return str(value["format"])
    if value in FISCAL_YEAR_LABEL_FORMATS:
        return str(value)
    raise ValueError(
        f"format must be one of {list(FISCAL_YEAR_LABEL_FORMATS)!r} "
        f"(got {value!r})"
    )


def render_fiscal_year_label(
    year_no: int,
    format_token: str = DEFAULT_FISCAL_YEAR_LABEL_FORMAT,
    *,
    spans_years: bool = True,
) -> str:
    """Render a caption while preserving the supplied numeric year key.

    ``spans_years=False`` is used for January-start and ISO calendars. Those
    calendars always expose the plain integer regardless of the tenant token.
    """
    token = extract_fiscal_year_label_format(format_token)
    if not spans_years or token == "start_year":
        return str(year_no)

    end_year = year_no + 1
    if token == "span_short":
        return f"{year_no}-{end_year % 100:02d}"
    if token == "span_long":
        return f"{year_no}-{end_year}"
    if token == "span_fy":
        return f"FY{year_no % 100:02d}-{end_year % 100:02d}"
    if token == "end_year":
        return f"FY{end_year}"
    # ``extract_fiscal_year_label_format`` makes this unreachable, but retain a
    # loud guard if the vocabulary is extended without updating this renderer.
    raise ValueError(f"Unsupported fiscal year label format: {token!r}")
