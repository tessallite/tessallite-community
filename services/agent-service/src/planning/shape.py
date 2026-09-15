"""Result-shape normalization for analytical contracts."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Literal

from src.planning.contracts import (
    DataQualityFinding,
    FieldRole,
    ShapeContract,
    ShapeLimits,
    ShapeNarrationFacts,
)
from src.planning.enums import AnalyticalShape, AxisRole
from src.planning.intent import AnalyticalIntent
from src.planning.quality import evaluate_shape_data_quality
from src.planning.temporal import temporal_part_sort_value


_TEMPORAL_NAME_RE = re.compile(
    r"(?:date|time|timestamp|year|month|quarter|week|day|hour|period)",
    re.IGNORECASE,
)


@dataclass
class ShapedResult:
    columns: list[str]
    rows: list[list[Any]]
    chart_type: str | None
    output_mode: Literal["kpi", "chart", "table", "chart_table"]
    shape: AnalyticalShape
    contract: ShapeContract
    quality_findings: list[DataQualityFinding]
    narration_facts: ShapeNarrationFacts
    notes: list[str] = field(default_factory=list)

    def as_trace(self) -> dict[str, Any]:
        return {
            "columns": list(self.columns),
            "chart_type": self.chart_type,
            "output_mode": self.output_mode,
            "shape": self.shape.value,
            "contract": self.contract.as_trace(),
            "quality_findings": [finding.as_trace() for finding in self.quality_findings],
            "narration_facts": self.narration_facts.as_trace(),
            "notes": list(self.notes),
            "row_sample": self.rows[:5],
        }


def normalize_result_shape(
    result_columns: list[str],
    result_rows: list[dict[str, Any]] | list[list[Any]],
    intent: AnalyticalIntent,
    field_roles: list[FieldRole],
    contract: ShapeContract,
    limits: ShapeLimits,
    filter_where: list[dict[str, Any]] | None = None,
) -> ShapedResult:
    dict_rows = _dict_rows(result_columns, result_rows)
    working_contract = replace(
        contract,
        renderer_binding=dict(contract.renderer_binding),
        notes=list(contract.notes),
        data_quality_checks=list(contract.data_quality_checks),
    )
    chart_rows = _shape_rows(dict_rows, field_roles, working_contract)
    columns = _columns_for_rows(result_columns, chart_rows)
    quality = evaluate_shape_data_quality(
        contract=working_contract,
        columns=columns,
        rows=chart_rows,
        limits=limits,
    )
    notes = list(working_contract.notes)
    chart_type = working_contract.chart_preference
    if working_contract.table_required:
        chart_type = None
    if any(finding.severity == "block_chart" for finding in quality):
        chart_type = None
        notes.append("chart_blocked_by_quality")

    output_mode = _output_mode(contract.shape, chart_type)
    facts = _narration_facts(
        shape=contract.shape,
        rows=chart_rows,
        columns=columns,
        contract=working_contract,
        quality_findings=quality,
        intent=intent,
        output_mode=output_mode,
        filter_where=filter_where,
    )
    return ShapedResult(
        columns=columns,
        rows=[[row.get(col) for col in columns] for row in chart_rows],
        chart_type=chart_type,
        output_mode=output_mode,
        shape=contract.shape,
        contract=working_contract,
        quality_findings=quality,
        narration_facts=facts,
        notes=notes,
    )


def _dict_rows(
    columns: list[str],
    rows: list[dict[str, Any]] | list[list[Any]],
) -> list[dict[str, Any]]:
    if not rows:
        return []
    first = rows[0]
    if isinstance(first, dict):
        return [dict(row) for row in rows if isinstance(row, dict)]
    out: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, list):
            out.append({col: row[idx] if idx < len(row) else None for idx, col in enumerate(columns)})
    return out


def _shape_rows(
    rows: list[dict[str, Any]],
    roles: list[FieldRole],
    contract: ShapeContract,
) -> list[dict[str, Any]]:
    shaped = [dict(row) for row in rows]
    year_col = _role_name_with_note(roles, "year")
    month_col = _role_name_with_note(roles, "month_part") or _role_name_with_note(roles, "month_name")
    if year_col and month_col:
        for row in shaped:
            year = row.get(year_col)
            month_sort = temporal_part_sort_value(row.get(month_col), part="month")
            if year is not None and month_sort is not None:
                row["period"] = f"{int(year):04d}-{month_sort:02d}"
        if "period" in {key for row in shaped for key in row}:
            shaped.sort(key=lambda row: row.get("period") or "")
            contract.renderer_binding["x"] = "period"
    else:
        x_col = contract.renderer_binding.get("x")
        temporal_role = _role_by_name(roles, x_col) if x_col else None
        if x_col and temporal_role and any("month_name" in note for note in temporal_role.notes):
            shaped.sort(key=lambda row: temporal_part_sort_value(row.get(x_col), part="month") or 999)
        elif x_col and temporal_role and any("hour_part" in note for note in temporal_role.notes):
            shaped.sort(key=lambda row: temporal_part_sort_value(row.get(x_col), part="hour") or 999)
    if contract.shape == AnalyticalShape.STACKED_COMPOSITION:
        shaped = _composition_share_rows(shaped, contract)
    return shaped


def _columns_for_rows(original_columns: list[str], rows: list[dict[str, Any]]) -> list[str]:
    columns = list(original_columns)
    if rows and "period" in rows[0] and "period" not in columns:
        columns = ["period", *columns]
    if rows:
        for column in rows[0]:
            if column not in columns:
                columns.append(column)
        columns = [column for column in columns if column in rows[0]]
    if rows and "period" in rows[0]:
        columns = _drop_period_helper_columns(columns, rows)
    return columns


def _drop_period_helper_columns(
    columns: list[str],
    rows: list[dict[str, Any]],
) -> list[str]:
    helper_columns: set[str] = set()
    for column in columns:
        if column == "period":
            continue
        values = [row.get(column) for row in rows if column in row]
        if not values:
            continue
        lowered = column.lower()
        if "year" in lowered and all(_bounded_int(value, 1000, 9999) is not None for value in values):
            helper_columns.add(column)
            continue
        if (
            ("month" in lowered or lowered.endswith("_mo"))
            and all(temporal_part_sort_value(value, part="month") is not None for value in values)
        ):
            helper_columns.add(column)
    if not helper_columns:
        return columns
    return [column for column in columns if column not in helper_columns]


def _bounded_int(value: Any, low: int, high: int) -> int | None:
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    if low <= parsed <= high:
        return parsed
    return None


def _to_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _numeric_column(rows: list[dict[str, Any]], columns: list[str]) -> str | None:
    for column in columns:
        if any(_to_decimal(row.get(column)) is not None for row in rows):
            return column
    return None


def _composition_share_rows(
    rows: list[dict[str, Any]],
    contract: ShapeContract,
) -> list[dict[str, Any]]:
    if not rows:
        return rows
    columns = list(rows[0].keys())
    value_col = (
        contract.renderer_binding.get("value")
        or contract.renderer_binding.get("y")
        or _numeric_column(rows, columns)
    )
    if not value_col:
        return rows
    values = [_to_decimal(row.get(value_col)) for row in rows]
    numeric = [value for value in values if value is not None]
    total = sum(numeric, Decimal("0"))
    if total <= 0:
        return rows
    lowered_value_col = value_col.lower()
    if (
        "%" in value_col
        or "share" in lowered_value_col
        or "percent" in lowered_value_col
        or Decimal("99.9") <= total <= Decimal("100.1")
    ):
        return rows
    share_col = f"{value_col} Share (%)"
    contract.renderer_binding["value"] = share_col
    if contract.renderer_binding.get("y") == value_col:
        contract.renderer_binding["y"] = share_col
    shaped: list[dict[str, Any]] = []
    for row, value in zip(rows, values, strict=True):
        share = Decimal("0") if value is None else value / total * Decimal("100")
        shaped_row = {key: row.get(key) for key in columns if key != value_col}
        shaped_row[share_col] = float(share.quantize(Decimal("0.01")))
        shaped.append(shaped_row)
    return shaped


def _output_mode(shape: AnalyticalShape, chart_type: str | None) -> Literal["kpi", "chart", "table", "chart_table"]:
    if shape in {AnalyticalShape.KPI, AnalyticalShape.KPI_SET} and chart_type == "kpi":
        return "kpi"
    if chart_type:
        return "chart_table"
    return "table"


def _narration_facts(
    *,
    shape: AnalyticalShape,
    rows: list[dict[str, Any]],
    columns: list[str],
    contract: ShapeContract,
    quality_findings: list[DataQualityFinding],
    intent: AnalyticalIntent,
    output_mode: str,
    filter_where: list[dict[str, Any]] | None = None,
) -> ShapeNarrationFacts:
    x_col = contract.renderer_binding.get("x") or contract.renderer_binding.get("temporal")
    series_col = contract.renderer_binding.get("series")
    value_col = (
        contract.renderer_binding.get("y")
        or contract.renderer_binding.get("value")
        or _first_numeric_column(rows, columns)
    )
    if value_col and not _column_has_numeric_values(rows, value_col):
        value_col = _first_numeric_column(rows, columns)
    facts = ShapeNarrationFacts(
        shape=shape,
        row_count=len(rows),
        output_mode=output_mode,
        value_label=value_col,
        value=rows[0].get(value_col) if value_col and len(rows) == 1 else None,
        chart_notes=[finding.message for finding in quality_findings],
        data_quality_findings=[finding.as_trace() for finding in quality_findings],
        unsupported_notes=list(contract.notes),
        separate_series=bool(series_col),
    )
    if x_col and rows:
        values = [row.get(x_col) for row in rows if row.get(x_col) is not None]
        if values:
            facts.date_range[x_col] = _ordered_bounds(values)
            filtered_range = _temporal_filter_range_for_axis(x_col, filter_where)
            if filtered_range is not None:
                # A grouped temporal label (for example, 2026-01-01 for a
                # year bucket) is a key, not the source period's coverage.
                # Use the validated source filter when it is explicitly
                # related to that derived axis.
                facts.date_range[x_col] = filtered_range
    if series_col and rows:
        for row in rows:
            series = row.get(series_col)
            if series is None:
                continue
            bucket = facts.series_coverage.setdefault(str(series), {"row_count": 0})
            bucket["row_count"] += 1
            period = row.get(x_col) if x_col else None
            if period is not None:
                bucket.setdefault("first_period", period)
                bucket["last_period"] = period
            parsed = _to_decimal(row.get(value_col)) if value_col else None
            if parsed is not None:
                value = float(parsed)
                if "min_value" not in bucket or value < bucket["min_value"]:
                    bucket["min_value"] = value
                    if period is not None:
                        bucket["min_period"] = period
                if "max_value" not in bucket or value > bucket["max_value"]:
                    bucket["max_value"] = value
                    if period is not None:
                        bucket["max_period"] = period
    if value_col and rows:
        numeric = [
            (row, float(parsed))
            for row in rows
            if (parsed := _to_decimal(row.get(value_col))) is not None
        ]
        if numeric:
            low = min(numeric, key=lambda item: item[1])
            high = max(numeric, key=lambda item: item[1])
            facts.extrema[value_col] = {
                "min": low[1],
                "max": high[1],
                "min_row": low[0],
                "max_row": high[0],
            }
            if x_col:
                facts.extrema[value_col]["min_period"] = low[0].get(x_col)
                facts.extrema[value_col]["max_period"] = high[0].get(x_col)
    if rows and value_col and x_col:
        null_count = sum(1 for row in rows if row.get(value_col) is None)
        if null_count:
            facts.gaps.append({"code": "null_value_cells", "column": value_col, "count": null_count})
    if intent.wants_ranking and rows:
        facts.ranking = {
            "sort_metric": value_col,
            "direction": intent.ranking_direction,
            "limit": intent.requested_limit,
            "limit_explicit": intent.requested_limit is not None,
            "top_boundary": rows[0],
            "bottom_boundary": rows[-1],
            "row_count": len(rows),
        }
    if shape in {AnalyticalShape.BREAKDOWN, AnalyticalShape.DISTRIBUTION, AnalyticalShape.GROUPED_COMPARISON}:
        category_col = x_col or _first_non_numeric_column(rows, columns)
        facts.breakdown = _breakdown_facts(rows, category_col, value_col)
    if shape == AnalyticalShape.STACKED_COMPOSITION:
        facts.composition = _composition_facts(rows, value_col)
    if shape == AnalyticalShape.MATRIX:
        facts.matrix = _matrix_facts(rows, columns, contract, output_mode)
    if output_mode == "table":
        facts.table = {
            "row_count": len(rows),
            "shown_row_count": len(rows),
            "table_only_reason": "chart_not_selected" if contract.chart_preference is None else "chart_rejected_or_disabled",
        }
    if any(finding.code == "missing_monthly_periods" for finding in quality_findings):
        facts.gaps.append({"code": "missing_monthly_periods"})
    return facts


def _temporal_filter_range_for_axis(
    axis_name: str,
    filter_where: list[dict[str, Any]] | None,
) -> tuple[Any, Any] | None:
    """Return a related ``between`` filter for a grouped temporal axis.

    A raw date axis already reports the observed row bounds. This helper only
    supplies a filter range when the rendered axis is a derived temporal name
    such as ``business_date_year`` (or the generic ``period`` helper), so a
    bucket label cannot be mistaken for the filtered source coverage.
    """
    if not isinstance(axis_name, str):
        return None
    axis = axis_name.casefold()
    if axis != "period" and not _TEMPORAL_NAME_RE.search(axis):
        return None
    for item in filter_where or []:
        if not isinstance(item, dict) or item.get("op") != "between":
            continue
        filter_name = item.get("name")
        value = item.get("value")
        if (
            not isinstance(filter_name, str)
            or not _TEMPORAL_NAME_RE.search(filter_name)
            or not isinstance(value, (list, tuple))
            or len(value) != 2
            or any(bound is None for bound in value)
        ):
            continue
        source = filter_name.casefold()
        if axis == source:
            continue
        if axis == "period" or axis.startswith(f"{source}_") or source.startswith(f"{axis}_"):
            return value[0], value[1]
    return None


def _comparison_target_category(
    user_message: str | None,
    categories: list[Any],
) -> Any | None:
    """Find one explicitly named category in a benchmark question."""
    text = " ".join((user_message or "").split())
    matches: list[Any] = []
    for category in sorted(categories, key=lambda value: len(str(value)), reverse=True):
        token = str(category).strip()
        if not token:
            continue
        if re.search(rf"(?<!\w){re.escape(token)}(?!\w)", text, re.IGNORECASE):
            matches.append(category)
    if len(matches) != 1:
        return None
    return matches[0]


def add_average_comparison_fact(
    facts: ShapeNarrationFacts,
    rows: list[dict[str, Any]],
    *,
    user_message: str | None,
    complete: bool,
    requested: bool,
) -> None:
    """Attach a grounded category-versus-average fact to a complete result.

    Stage 1 serves the absolute grouped totals after benchmark containment. It
    does not run a new reducer for the average. When the returned breakdown is
    complete and has exactly one numeric value per category, the average and
    requested category relation can be computed from those returned rows. Any
    incomplete, duplicated, malformed, or unaddressed result receives an
    explicit unavailable status so narration cannot infer the answer.
    """
    if not requested:
        return

    def unavailable(reason: str) -> None:
        facts.average_comparison = {
            "status": "unavailable",
            "reason": reason,
        }

    if not complete:
        unavailable("complete_grouped_rows_required")
        return
    breakdown = facts.breakdown
    category_field = breakdown.get("category_field") if isinstance(breakdown, dict) else None
    value_field = facts.value_label
    if (
        not isinstance(category_field, str)
        or not isinstance(value_field, str)
        or not isinstance(breakdown, dict)
        or breakdown.get("omitted_rows", 0) != 0
        or not rows
    ):
        unavailable("complete_grouped_breakdown_required")
        return

    values: list[tuple[Any, Decimal]] = []
    seen_categories: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            unavailable("complete_grouped_breakdown_required")
            return
        category = row.get(category_field)
        amount = _to_decimal(row.get(value_field))
        category_key = str(category)
        if (
            category is None
            or category_key in seen_categories
            or amount is None
            or not amount.is_finite()
        ):
            unavailable("one_numeric_value_per_category_required")
            return
        seen_categories.add(category_key)
        values.append((category, amount))

    if len(values) < 2 or len(values) != breakdown.get("category_count"):
        unavailable("complete_grouped_breakdown_required")
        return
    target = _comparison_target_category(user_message, [category for category, _ in values])
    if target is None:
        unavailable("target_category_not_in_complete_grouped_rows")
        return

    average = sum((amount for _, amount in values), Decimal("0")) / Decimal(len(values))
    comparison_rows: dict[str, dict[str, Any]] = {}
    target_value: Decimal | None = None
    target_key = str(target)
    for category, amount in values:
        relation = "above" if amount > average else "below" if amount < average else "equal"
        comparison_rows[str(category)] = {
            "value": float(amount),
            "relation": relation,
        }
        if str(category) == target_key:
            target_value = amount
    if target_value is None:
        unavailable("target_category_not_in_complete_grouped_rows")
        return
    facts.average_comparison = {
        "status": "available",
        "category_field": category_field,
        "value_field": value_field,
        "category_count": len(values),
        "average_value": float(average),
        "target_category": target,
        "target_value": float(target_value),
        "target_relation": (
            "above" if target_value > average
            else "below" if target_value < average
            else "equal"
        ),
        "comparisons": comparison_rows,
    }


def _ordered_bounds(values: list[Any]) -> tuple[Any, Any]:
    numeric: list[tuple[Decimal, Any]] = []
    for value in values:
        if isinstance(value, bool):
            break
        try:
            numeric.append((Decimal(str(value)), value))
        except (InvalidOperation, ValueError):
            break
    else:
        if numeric:
            return (
                min(numeric, key=lambda item: item[0])[1],
                max(numeric, key=lambda item: item[0])[1],
            )
    return min(values), max(values)


def _first_numeric_column(rows: list[dict[str, Any]], columns: list[str]) -> str | None:
    return _numeric_column(rows, columns) or (columns[-1] if columns else None)


def _column_has_numeric_values(rows: list[dict[str, Any]], column: str) -> bool:
    return any(_to_decimal(row.get(column)) is not None for row in rows)


def _first_non_numeric_column(rows: list[dict[str, Any]], columns: list[str]) -> str | None:
    for column in columns:
        if any(row.get(column) is not None and not isinstance(row.get(column), (int, float, bool)) for row in rows):
            return column
    return columns[0] if columns else None


def _breakdown_facts(
    rows: list[dict[str, Any]],
    category_col: str | None,
    value_col: str | None,
) -> dict[str, Any] | None:
    if not rows or not category_col:
        return None
    facts: dict[str, Any] = {
        "category_field": category_col,
        "category_count": len({row.get(category_col) for row in rows}),
        "omitted_rows": 0,
    }
    if value_col:
        numeric = [
            (row.get(category_col), float(parsed))
            for row in rows
            if (parsed := _to_decimal(row.get(value_col))) is not None
        ]
        if numeric:
            largest = max(numeric, key=lambda item: item[1])
            smallest = min(numeric, key=lambda item: item[1])
            facts["largest_category"] = {"category": largest[0], "value": largest[1]}
            facts["smallest_category"] = {"category": smallest[0], "value": smallest[1]}
    return facts


def _composition_facts(rows: list[dict[str, Any]], value_col: str | None) -> dict[str, Any] | None:
    if not rows or not value_col:
        return None
    values = [
        float(parsed)
        for row in rows
        if (parsed := _to_decimal(row.get(value_col))) is not None
    ]
    if not values:
        return {"denominator_scope": "result_rows", "total": None, "all_parts_positive": False}
    total = sum(values)
    return {
        "denominator_scope": "result_rows",
        "total": total,
        "largest_part": max(values),
        "all_parts_positive": all(value > 0 for value in values),
        "pie_rejection_reason": "non_positive_or_zero_total" if total <= 0 or any(value <= 0 for value in values) else None,
    }


def _matrix_facts(
    rows: list[dict[str, Any]],
    columns: list[str],
    contract: ShapeContract,
    output_mode: str,
) -> dict[str, Any]:
    row_axis = contract.renderer_binding.get("x") or (columns[0] if columns else None)
    col_axis = contract.renderer_binding.get("series") or (columns[1] if len(columns) > 1 else None)
    visual_mode = contract.chart_preference or "table_only"
    facts = {
        "row_axis": row_axis,
        "column_axis": col_axis,
        "primary_axis": row_axis,
        "secondary_axis": col_axis,
        "row_axis_count": len({row.get(row_axis) for row in rows}) if row_axis else 0,
        "column_axis_count": len({row.get(col_axis) for row in rows}) if col_axis else 0,
        "populated_cell_count": len(rows),
        "visual_mode": visual_mode,
    }
    if output_mode == "table":
        facts["table_only_note"] = "matrix rendered as table"
    return facts


def _role_name_with_note(roles: list[FieldRole], note: str) -> str | None:
    for role in roles:
        if role.role == AxisRole.TEMPORAL and note in role.notes:
            return role.name
    return None


def _role_by_name(roles: list[FieldRole], name: str | None) -> FieldRole | None:
    if not name:
        return None
    for role in roles:
        if role.name == name:
            return role
    return None
