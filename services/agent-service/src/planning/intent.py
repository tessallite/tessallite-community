"""Deterministic analytical-intent detection for agent planning."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from src.planning.enums import AnalyticalShape


@dataclass
class AnalyticalIntent:
    shape_hint: AnalyticalShape | None
    wants_trend: bool = False
    wants_comparison: bool = False
    wants_ranking: bool = False
    wants_breakdown: bool = False
    wants_distribution: bool = False
    wants_detail_rows: bool = False
    wants_composition: bool = False
    wants_separate_series: bool = False
    wants_scatter: bool = False
    preserve_previous_breakdown_dimensions: bool = False
    preserve_previous_breakdown_where: bool = False
    requested_grain: str | None = None
    requested_period_may_cross_year: bool = False
    ranking_direction: str | None = None
    requested_limit: int | None = None
    confidence: str = "heuristic"
    notes: list[str] = field(default_factory=list)

    def as_trace(self) -> dict[str, Any]:
        return {
            "shape_hint": self.shape_hint.value if self.shape_hint else None,
            "wants_trend": self.wants_trend,
            "wants_comparison": self.wants_comparison,
            "wants_ranking": self.wants_ranking,
            "wants_breakdown": self.wants_breakdown,
            "wants_distribution": self.wants_distribution,
            "wants_detail_rows": self.wants_detail_rows,
            "wants_composition": self.wants_composition,
            "wants_separate_series": self.wants_separate_series,
            "wants_scatter": self.wants_scatter,
            "preserve_previous_breakdown_dimensions": (
                self.preserve_previous_breakdown_dimensions
            ),
            "preserve_previous_breakdown_where": self.preserve_previous_breakdown_where,
            "requested_grain": self.requested_grain,
            "requested_period_may_cross_year": self.requested_period_may_cross_year,
            "ranking_direction": self.ranking_direction,
            "requested_limit": self.requested_limit,
            "confidence": self.confidence,
            "notes": list(self.notes),
        }


_TREND_RE = re.compile(
    r"\b(trend|over time|time series|monthly|weekly|yearly|quarterly|daily|"
    r"by month|by week|by quarter|by year|by day|over the last)\b",
    re.I,
)
_COMPARISON_RE = re.compile(
    r"\b(compare|compared|versus|vs\.?|against|alongside|difference between|"
    r"compare that to|compare it to)\b",
    re.I,
)
_RANKING_RE = re.compile(
    # ``most`` is a supported superlative for a descending ranking. Keep the
    # negative lookahead so composition wording such as ``most of the total``
    # does not accidentally become a Top-N request.
    r"\b(top|bottom|highest|lowest|best|worst|largest|smallest|"
    r"rank|ranking|most(?!\s+of\b))\b",
    re.I,
)
_BREAKDOWN_RE = re.compile(r"\b(by|split by|group(?:ed)? by|per|across)\b", re.I)
_DISTRIBUTION_RE = re.compile(
    r"\b(distribution|distributed|histogram|bucket|buckets|ranges?)\b",
    re.I,
)
_SCATTER_RE = re.compile(
    r"\b(scatter(?:\s+plot)?|correlation plot|relationship between)\b",
    re.I,
)
_DETAIL_STRONG_RE = re.compile(
    r"\b(raw rows?|individual|records?|examples?|list(?: the)? rows?)\b",
    re.I,
)
_TRANSACTION_ROWS_RE = re.compile(r"\btransactions?\b", re.I)
_DETAIL_COMMAND_RE = re.compile(r"\b(show|list|give me|display|see)\b", re.I)
_AGGREGATE_CUE_RE = re.compile(
    r"\b(trend|by|group|split|per|across|top|bottom|share|total|sum|avg|"
    r"average|count|amount|revenue|cost|margin|rate|ratio|over time)\b",
    re.I,
)
_COMPOSITION_RE = re.compile(
    r"\b(share|proportion|mix|contribution|percentage of total|percent of total|"
    r"part of total|parts of a whole)\b",
    re.I,
)
_SEPARATE_SERIES_RE = re.compile(
    r"\b(separate (?:trend )?lines?|each .* line|each .* trend|per .* line|"
    r"split .* into lines?)\b",
    re.I,
)
_TEMPORAL_BREAKDOWN_FOLLOW_UP_RE = re.compile(
    r"\b(?:break|split)\s+(?:that|it|those|these|them|this)\s+down\b",
    re.I,
)
_BREAKDOWN_REPLACEMENT_OR_EXCLUSION_RE = re.compile(
    r"\b(?:instead(?:\s+of)?|rather\s+than|replace|not\s+by|only\s+by|drop|remove)\b",
    re.I,
)
_BREAKDOWN_WHERE_CHANGE_RE = re.compile(
    r"\b(?:where|filter(?:ed|ing)?|excluding|except|without|"
    r"ignore(?:d|ing)?|condition|threshold)\b",
    re.I,
)
_BREAKDOWN_PERIOD_CHANGE_RE = re.compile(
    r"\b(?:for|from|between|during|since|until|before|after|in|on|over)\s+"
    r"(?:the\s+)?(?:"
    r"(?:19|20)\d{2}\b|"
    r"\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?\b|"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\b|"
    r"q[1-4]\b|"
    r"(?:this|current|previous|prior|next|last)\s+"
    r"(?:period|year|quarter|month|week|day|"
    r"\d+\s+(?:days?|weeks?|months?|quarters?|years?))\b|"
    r"(?:different|new|another)\s+period\b"
    r")",
    re.I,
)
_BREAKDOWN_PERIOD_WINDOW_RE = re.compile(
    r"\b(?:last|past|next|previous|prior)\s+\d+\s+"
    r"(?:days?|weeks?|months?|quarters?|years?)\b",
    re.I,
)
_TEMPORAL_BREAKDOWN_AXIS_RE = re.compile(
    r"\b(?:break|split)\s+(?:that|it|those|these|them|this)\s+down\s+"
    r"(?:by|per)\s+(?:month|monthly|week|weekly|quarter|quarterly|"
    r"year|yearly|annual|annually|day|daily|hour|hourly)\b",
    re.I,
)
_GRAIN_PATTERNS = (
    ("month", re.compile(r"\b(month|monthly|by month|last \d+ months?)\b", re.I)),
    ("week", re.compile(r"\b(week|weekly|by week|last \d+ weeks?)\b", re.I)),
    ("quarter", re.compile(r"\b(quarter|quarterly|by quarter|last \d+ quarters?)\b", re.I)),
    ("year", re.compile(r"\b(year|yearly|annual|annually|by year|last \d+ years?)\b", re.I)),
    ("day", re.compile(r"\b(day|daily|by day|last \d+ days?)\b", re.I)),
    ("hour", re.compile(r"\b(hour|hourly|by hour|hour of day)\b", re.I)),
)


def detect_analytical_intent(
    user_message: str,
    previous_plan: dict[str, Any] | None = None,
) -> AnalyticalIntent:
    text = " ".join((user_message or "").lower().split())
    notes: list[str] = []

    wants_trend = bool(_TREND_RE.search(text))
    wants_comparison = bool(_COMPARISON_RE.search(text))
    wants_ranking = bool(_RANKING_RE.search(text))
    wants_breakdown = bool(_BREAKDOWN_RE.search(text))
    wants_distribution = bool(_DISTRIBUTION_RE.search(text))
    wants_scatter = bool(_SCATTER_RE.search(text))
    wants_composition = bool(_COMPOSITION_RE.search(text))
    wants_separate_series = bool(_SEPARATE_SERIES_RE.search(text))
    wants_detail_rows = _detect_detail_intent(text)
    requested_grain = _detect_requested_grain(text)
    ranking_direction = _detect_ranking_direction(text)
    requested_limit = _detect_requested_limit(text)
    requested_period_may_cross_year = _period_may_cross_year(text, previous_plan)
    preserve_previous_breakdown_dimensions = bool(
        previous_plan
        and requested_grain
        and _TEMPORAL_BREAKDOWN_FOLLOW_UP_RE.search(text)
        and not _BREAKDOWN_REPLACEMENT_OR_EXCLUSION_RE.search(text)
    )
    preserve_previous_breakdown_where = bool(
        previous_plan
        and requested_grain
        and _TEMPORAL_BREAKDOWN_FOLLOW_UP_RE.search(text)
        and not _breakdown_changes_period_or_filter(text)
    )

    if _looks_like_follow_up(text):
        prev_shape = _previous_shape(previous_plan)
        if _previous_has_trend(previous_plan):
            wants_trend = True
            notes.append("inherited_trend_from_previous_plan")
        if prev_shape == AnalyticalShape.MULTI_SERIES_TIME:
            wants_separate_series = True
            notes.append("inherited_separate_series_from_previous_shape")

    if preserve_previous_breakdown_dimensions:
        notes.append("preserve_previous_breakdown_dimensions")
    if preserve_previous_breakdown_where:
        notes.append("preserve_previous_breakdown_where")

    if wants_separate_series:
        wants_trend = True
    if wants_detail_rows and _raw_record_wording(text):
        notes.append("raw_records_requested")

    grouping_count = _rough_requested_grouping_count(text)
    if grouping_count >= 2 and not wants_trend:
        notes.append("multiple_grouping_axes_requested")

    shape_hint = _shape_hint(
        wants_trend=wants_trend,
        wants_comparison=wants_comparison,
        wants_ranking=wants_ranking,
        wants_breakdown=wants_breakdown,
        wants_distribution=wants_distribution,
        wants_detail_rows=wants_detail_rows,
        wants_composition=wants_composition,
        wants_separate_series=wants_separate_series,
        wants_scatter=wants_scatter,
        grouping_count=grouping_count,
    )

    if requested_period_may_cross_year:
        notes.append("period_may_cross_parent_cycle")
    if requested_grain:
        notes.append(f"requested_grain={requested_grain}")

    confidence = "metadata" if previous_plan else "heuristic"
    return AnalyticalIntent(
        shape_hint=shape_hint,
        wants_trend=wants_trend,
        wants_comparison=wants_comparison,
        wants_ranking=wants_ranking,
        wants_breakdown=wants_breakdown,
        wants_distribution=wants_distribution,
        wants_detail_rows=wants_detail_rows,
        wants_composition=wants_composition,
        wants_separate_series=wants_separate_series,
        wants_scatter=wants_scatter,
        preserve_previous_breakdown_dimensions=preserve_previous_breakdown_dimensions,
        preserve_previous_breakdown_where=preserve_previous_breakdown_where,
        requested_grain=requested_grain,
        requested_period_may_cross_year=requested_period_may_cross_year,
        ranking_direction=ranking_direction,
        requested_limit=requested_limit,
        confidence=confidence,
        notes=notes,
    )


def _breakdown_changes_period_or_filter(text: str) -> bool:
    """Detect an explicit period/filter change on a temporal breakdown follow-up.

    Dimension replacement is intentionally not included here. A request such as
    ``"Break that down by year instead of account type"`` changes the grouping
    axis while retaining the prior date restriction. The ``where`` repair only
    stops when the user supplies a new period or a row-filter condition.
    """
    if _BREAKDOWN_WHERE_CHANGE_RE.search(text):
        return True
    if _BREAKDOWN_PERIOD_CHANGE_RE.search(text):
        return True
    if _BREAKDOWN_PERIOD_WINDOW_RE.search(text):
        return True

    axis_match = _TEMPORAL_BREAKDOWN_AXIS_RE.search(text)
    if not axis_match:
        return False
    tail = text[axis_match.end():]
    # ``for London`` / ``in EMEA`` are ordinary user wording for a new
    # category filter. Keep the useful additive forms ``for each ...`` and
    # ``in every ...`` as dimension wording, and keep an explicit reaffirmation
    # such as ``for the same period`` on the preservation path.
    if re.search(
        r"\b(?:for|in)\s+(?!each\b|every\b|same\b|the\s+same\b)\S+",
        tail,
        re.I,
    ):
        return True
    return bool(
        re.search(
            r"\b(?:with|using)\s+[^,.!?]*(?:=|\b(?:is|equals|between|in)\b)",
            tail,
            re.I,
        )
    )


def _detect_requested_grain(text: str) -> str | None:
    for grain, pattern in _GRAIN_PATTERNS:
        if pattern.search(text):
            return grain
    return None


def _detect_ranking_direction(text: str) -> str | None:
    if re.search(r"\b(bottom|lowest|worst|smallest)\b", text):
        return "asc"
    if re.search(r"\b(top|highest|best|largest|most(?!\s+of\b))\b", text):
        return "desc"
    return None


def _detect_requested_limit(text: str) -> int | None:
    match = re.search(r"\b(?:top|bottom|first|last)\s+(\d{1,4})\b", text)
    if match:
        value = int(match.group(1))
        if value < 1 or value > 1000:
            return None
        return value
    # An unqualified ``most`` asks for the single maximum. The planner's
    # normal ranking repair then supplies the explicit descending limit, and
    # shape narration can distinguish that requested boundary from a safety
    # truncation. ``most of`` is deliberately excluded by _RANKING_RE above.
    if re.search(r"\bmost(?!\s+of\b)\b", text):
        return 1
    return None


def _detect_detail_intent(text: str) -> bool:
    if "show more details" in text:
        return True
    if _DETAIL_STRONG_RE.search(text) and _DETAIL_COMMAND_RE.search(text):
        return True
    return bool(
        _TRANSACTION_ROWS_RE.search(text)
        and _DETAIL_COMMAND_RE.search(text)
        and not _AGGREGATE_CUE_RE.search(text)
    )


def _raw_record_wording(text: str) -> bool:
    return bool(
        _TRANSACTION_ROWS_RE.search(text)
        or re.search(r"\b(raw rows?|individual|records?|list(?: the)? rows?)\b", text)
    )


def _period_may_cross_year(text: str, previous_plan: dict[str, Any] | None) -> bool:
    months = re.search(r"\blast\s+(\d+)\s+months?\b", text)
    if months and int(months.group(1)) > 11:
        return True
    weeks = re.search(r"\blast\s+(\d+)\s+weeks?\b", text)
    if weeks and int(weeks.group(1)) > 52:
        return True
    dates = _dates_in_text(text)
    if len(dates) >= 2 and min(dates).year != max(dates).year:
        return True
    return _previous_filter_crosses_year(previous_plan)


def _dates_in_text(text: str) -> list[date]:
    dates: list[date] = []
    for yyyy, mm, dd in re.findall(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text):
        try:
            dates.append(date(int(yyyy), int(mm), int(dd)))
        except ValueError:
            continue
    return dates


def _previous_filter_crosses_year(previous_plan: dict[str, Any] | None) -> bool:
    if not previous_plan:
        return False
    for filter_item in _iter_previous_filters(previous_plan):
        value = filter_item.get("value")
        if filter_item.get("op") == "between" and isinstance(value, list) and len(value) >= 2:
            dates = _dates_in_text(" ".join(str(v) for v in value[:2]))
            if len(dates) == 2 and dates[0].year != dates[1].year:
                return True
    return False


def _iter_previous_filters(previous_plan: dict[str, Any]):
    bodies: list[dict[str, Any]] = []
    if isinstance(previous_plan.get("query"), dict):
        bodies.append(previous_plan["query"])
    compound = previous_plan.get("compound_query")
    if isinstance(compound, dict):
        bodies.extend(s for s in compound.get("steps", []) if isinstance(s, dict))
    for body in bodies:
        for key in ("where", "having"):
            for item in body.get(key) or []:
                if isinstance(item, dict):
                    yield item


def _looks_like_follow_up(text: str) -> bool:
    return bool(re.search(r"\b(that|it|same|those|these|previous|again|also)\b", text))


def _previous_has_trend(previous_plan: dict[str, Any] | None) -> bool:
    shape = _previous_shape(previous_plan)
    if shape in {
        AnalyticalShape.TIME_SERIES,
        AnalyticalShape.MULTI_SERIES_TIME,
        AnalyticalShape.MULTI_METRIC_TIME,
    }:
        return True
    if not previous_plan:
        return False
    bodies: list[dict[str, Any]] = []
    if isinstance(previous_plan.get("query"), dict):
        bodies.append(previous_plan["query"])
    compound = previous_plan.get("compound_query")
    if isinstance(compound, dict):
        bodies.extend(s for s in compound.get("steps", []) if isinstance(s, dict))
    for body in bodies:
        if body.get("chart_type") in {"line", "multi_line", "multi_line_wide"}:
            return True
        dims = body.get("dimension_exprs") or body.get("dimensions") or []
        for dim in dims:
            if isinstance(dim, dict) and dim.get("grain"):
                return True
            if isinstance(dim, str) and re.search(r"\b(date|month|week|quarter|year|period)\b", dim):
                return True
    return False


def _previous_shape(previous_plan: dict[str, Any] | None) -> AnalyticalShape | None:
    if not previous_plan:
        return None
    shape = previous_plan.get("shape")
    if isinstance(shape, dict):
        raw = shape.get("intent") or shape.get("shape")
    else:
        raw = None
    if isinstance(raw, str):
        try:
            return AnalyticalShape(raw)
        except ValueError:
            return None
    return None


def _rough_requested_grouping_count(text: str) -> int:
    match = re.search(r"\b(?:by|across|per)\s+(.+)$", text)
    if not match:
        return 0
    tail = re.split(r"\b(?:for|where|with|during|last|over)\b", match.group(1))[0]
    parts = [p.strip(" ,") for p in re.split(r"\band\b|,", tail) if p.strip(" ,")]
    return len(parts)


def _shape_hint(
    *,
    wants_trend: bool,
    wants_comparison: bool,
    wants_ranking: bool,
    wants_breakdown: bool,
    wants_distribution: bool,
    wants_detail_rows: bool,
    wants_composition: bool,
    wants_separate_series: bool,
    wants_scatter: bool,
    grouping_count: int,
) -> AnalyticalShape | None:
    if wants_scatter:
        return AnalyticalShape.UNSUPPORTED
    if wants_detail_rows:
        return AnalyticalShape.DETAIL_TABLE
    if wants_distribution:
        return AnalyticalShape.DISTRIBUTION
    if wants_separate_series and wants_trend:
        return AnalyticalShape.MULTI_SERIES_TIME
    if wants_trend:
        return AnalyticalShape.TIME_SERIES
    if wants_ranking:
        return AnalyticalShape.RANKING
    if grouping_count >= 2:
        return AnalyticalShape.MATRIX
    if wants_composition:
        return AnalyticalShape.STACKED_COMPOSITION
    if wants_comparison:
        return AnalyticalShape.GROUPED_COMPARISON
    if wants_breakdown:
        return AnalyticalShape.BREAKDOWN
    return None
