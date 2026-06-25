"""Reusable data-quality checks for analytical shape decisions."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from src.planning.contracts import DataQualityFinding, ShapeContract, ShapeLimits
from src.planning.enums import AnalyticalShape, AxisRole


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _dict_rows(columns: list[str], rows: list[dict[str, Any]] | list[list[Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    first = rows[0]
    if isinstance(first, dict):
        return [row for row in rows if isinstance(row, dict)]
    out: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, list):
            out.append({col: row[idx] if idx < len(row) else None for idx, col in enumerate(columns)})
    return out


def _value_columns(contract: ShapeContract, columns: list[str]) -> list[str]:
    bound = contract.renderer_binding
    candidates = [
        bound.get("value"),
        bound.get("y"),
        bound.get("x") if bound.get("y") is None else None,
    ]
    return [c for c in candidates if c in columns]


def _axis_column(contract: ShapeContract, columns: list[str]) -> str | None:
    for key in ("x", "segment", "category", "temporal"):
        col = contract.renderer_binding.get(key)
        if col in columns:
            return col
    return columns[0] if columns else None


def _series_column(contract: ShapeContract, columns: list[str]) -> str | None:
    col = contract.renderer_binding.get("series")
    return col if col in columns else None


def _month_index(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value)
    match = re.match(r"^(\d{4})-(\d{2})(?:-\d{2})?$", text)
    if not match:
        return None
    year = int(match.group(1))
    month = int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return year * 12 + month


def evaluate_shape_data_quality(
    *,
    contract: ShapeContract,
    columns: list[str],
    rows: list[dict[str, Any]] | list[list[Any]],
    limits: ShapeLimits,
) -> list[DataQualityFinding]:
    dict_rows = _dict_rows(columns, rows)
    findings: list[DataQualityFinding] = []

    if not dict_rows:
        findings.append(DataQualityFinding(
            code="empty_result",
            severity="info",
            message="The query returned no rows.",
            affected_columns=[],
        ))
        return findings

    if len(dict_rows) > limits.max_chart_rows:
        findings.append(DataQualityFinding(
            code="too_many_chart_rows",
            severity="block_chart",
            message="The result row count exceeds the configured chart row limit.",
            affected_columns=columns,
        ))

    if contract.shape == AnalyticalShape.STACKED_COMPOSITION:
        if (
            "additive_values" not in contract.data_quality_checks
            and "pie_suitable" not in contract.data_quality_checks
        ):
            findings.append(DataQualityFinding(
                code="stacked_requires_additive_values",
                severity="block_chart",
                message="Stacked composition requires additive part-of-whole values.",
                affected_columns=_value_columns(contract, columns),
            ))

    if "category_limit" in contract.data_quality_checks:
        axis_col = _axis_column(contract, columns)
        if axis_col:
            category_count = len({row.get(axis_col) for row in dict_rows if row.get(axis_col) is not None})
            if category_count > limits.max_bar_categories:
                findings.append(DataQualityFinding(
                    code="too_many_bar_categories",
                    severity="block_chart",
                    message="The category count exceeds the configured bar chart limit.",
                    affected_columns=[axis_col],
                ))

    if "pie_suitable" in contract.data_quality_checks:
        axis_col = _axis_column(contract, columns)
        value_cols = _value_columns(contract, columns)
        value_col = value_cols[0] if value_cols else None
        if axis_col and axis_col in columns and AxisRole.TEMPORAL in contract.required_axes:
            findings.append(DataQualityFinding(
                code="pie_temporal_axis",
                severity="block_chart",
                message="Pie charts cannot truthfully represent temporal axes.",
                affected_columns=[axis_col],
            ))
        if axis_col and len({row.get(axis_col) for row in dict_rows}) > limits.max_pie_segments:
            findings.append(DataQualityFinding(
                code="pie_too_many_segments",
                severity="block_chart",
                message="The category count exceeds the configured pie segment limit.",
                affected_columns=[axis_col],
            ))
        if value_col:
            values = [row.get(value_col) for row in dict_rows]
            numeric = [float(v) for v in values if _is_number(v)]
            if any(v < 0 for v in numeric):
                findings.append(DataQualityFinding(
                    code="pie_negative_values",
                    severity="block_chart",
                    message="Pie charts require non-negative values.",
                    affected_columns=[value_col],
                ))
            if numeric and sum(numeric) == 0:
                findings.append(DataQualityFinding(
                    code="pie_zero_total",
                    severity="block_chart",
                    message="Pie charts require a non-zero total.",
                    affected_columns=[value_col],
                ))

    if "time_series" in contract.data_quality_checks:
        temporal_col = contract.renderer_binding.get("x") or contract.renderer_binding.get("temporal")
        if temporal_col in columns:
            null_count = sum(1 for row in dict_rows if row.get(temporal_col) is None)
            if null_count:
                findings.append(DataQualityFinding(
                    code="null_temporal_keys",
                    severity="block_chart",
                    message="Time-series charts require non-null temporal keys.",
                    affected_columns=[temporal_col],
                ))
            counts = Counter(row.get(temporal_col) for row in dict_rows)
            duplicates = [key for key, count in counts.items() if key is not None and count > 1]
            if duplicates and contract.shape == AnalyticalShape.TIME_SERIES:
                findings.append(DataQualityFinding(
                    code="duplicate_temporal_keys",
                    severity="block_chart",
                    message="Single-series time charts require one row per temporal key.",
                    affected_columns=[temporal_col],
                ))
            month_indexes = sorted({
                idx for idx in (_month_index(row.get(temporal_col)) for row in dict_rows)
                if idx is not None
            })
            if len(month_indexes) >= 2:
                expected = month_indexes[-1] - month_indexes[0] + 1
                if expected > len(month_indexes):
                    findings.append(DataQualityFinding(
                        code="missing_monthly_periods",
                        severity="warning",
                        message="The monthly series has missing periods.",
                        affected_columns=[temporal_col],
                    ))

    if "multi_series" in contract.data_quality_checks:
        series_col = _series_column(contract, columns)
        temporal_col = contract.renderer_binding.get("x") or contract.renderer_binding.get("temporal")
        if series_col:
            series_values = {row.get(series_col) for row in dict_rows if row.get(series_col) is not None}
            if len(series_values) > limits.max_line_series:
                findings.append(DataQualityFinding(
                    code="too_many_line_series",
                    severity="block_chart",
                    message="The number of series exceeds the configured multi-line limit.",
                    affected_columns=[series_col],
                ))
            if temporal_col:
                coverage: dict[Any, set[Any]] = defaultdict(set)
                for row in dict_rows:
                    coverage[row.get(series_col)].add(row.get(temporal_col))
                sizes = {len(v) for v in coverage.values()}
                if len(sizes) > 1:
                    findings.append(DataQualityFinding(
                        code="uneven_series_coverage",
                        severity="warning",
                        message="Series cover different numbers of temporal periods.",
                        affected_columns=[series_col, temporal_col],
                    ))

    for value_col in _value_columns(contract, columns):
        nulls = sum(1 for row in dict_rows if row.get(value_col) is None)
        if nulls:
            findings.append(DataQualityFinding(
                code="null_measure_values",
                severity="warning",
                message="Some value cells are null and should be described carefully.",
                affected_columns=[value_col],
            ))

    if "matrix_size" in contract.data_quality_checks:
        if len(dict_rows) > limits.max_bar_categories * limits.max_grouped_measures:
            findings.append(DataQualityFinding(
                code="large_matrix_table",
                severity="warning",
                message="The matrix-shaped result is large and should stay table-first.",
                affected_columns=columns,
            ))

    if "table_size" in contract.data_quality_checks:
        if len(dict_rows) > limits.max_bar_categories:
            findings.append(DataQualityFinding(
                code="large_table_result",
                severity="warning",
                message="The table result exceeds the compact chart category limit.",
                affected_columns=columns,
            ))

    return findings
