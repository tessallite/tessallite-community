"""Enums shared by analytical-shape planning modules."""
from __future__ import annotations

from enum import Enum


class AxisRole(str, Enum):
    TEMPORAL = "temporal"
    CATEGORY = "category"
    ORDINAL = "ordinal"
    SERIES = "series"
    MATRIX_ROW = "matrix_row"
    MATRIX_COLUMN = "matrix_column"
    DETAIL = "detail"
    UNKNOWN = "unknown"


class TemporalAxisKind(str, Enum):
    INSTANT = "instant"
    GRAIN = "grain"
    PART = "part"
    COMPOSITE_PERIOD = "composite_period"


class TemporalOrderKind(str, Enum):
    CHRONOLOGICAL = "chronological"
    CALENDAR_CYCLE = "calendar_cycle"
    NUMERIC_PART = "numeric_part"
    EXPLICIT_MAP = "explicit_map"


class ValueRole(str, Enum):
    SINGLE_METRIC = "single_metric"
    MULTIPLE_METRICS = "multiple_metrics"
    COMPOUND_METRIC = "compound_metric"
    TIME_VARIANT_MEASURE = "time_variant_measure"
    RECORD_FIELDS = "record_fields"
    NONE = "none"


class AnalyticalShape(str, Enum):
    KPI = "kpi"
    KPI_SET = "kpi_set"
    BREAKDOWN = "breakdown"
    RANKING = "ranking"
    TIME_SERIES = "time_series"
    MULTI_SERIES_TIME = "multi_series_time"
    MULTI_METRIC_TIME = "multi_metric_time"
    GROUPED_COMPARISON = "grouped_comparison"
    STACKED_COMPOSITION = "stacked_composition"
    MATRIX = "matrix"
    DETAIL_TABLE = "detail_table"
    DISTRIBUTION = "distribution"
    UNSUPPORTED = "unsupported"


ROLE_CONFIDENCE_VALUES = ("metadata", "expression", "result", "heuristic")
FIELD_SOURCE_VALUES = ("measure", "dimension", "expression", "computed")
QUALITY_SEVERITY_VALUES = ("info", "warning", "block_chart", "block_answer")

