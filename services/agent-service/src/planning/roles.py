"""Field-role inference for analytical shape planning."""
from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Iterable
from typing import Any

from src.planning.contracts import FieldRole
from src.planning.enums import AxisRole, ValueRole
from src.planning.measure_metadata import (
    CompoundFormulaMetadata,
    MeasureRoleMetadata,
)
from src.tools.expressions import DimRef, FuncCall, Literal, base_field_names

_MONTH_NAMES = {
    "jan", "january", "feb", "february", "mar", "march", "apr", "april",
    "may", "jun", "june", "jul", "july", "aug", "august", "sep",
    "sept", "september", "oct", "october", "nov", "november", "dec",
    "december",
}
_WEEKDAY_NAMES = {
    "mon", "monday", "tue", "tues", "tuesday", "wed", "wednesday",
    "thu", "thur", "thurs", "thursday", "fri", "friday", "sat",
    "saturday", "sun", "sunday",
}
_TEMPORAL_NAME_RE = re.compile(
    r"(?:^|[_\s-])(date|time|timestamp|day|week|month|quarter|qtr|year|period|hour)(?:$|[_\s-])",
    re.IGNORECASE,
)


def _row_values(rows: list[dict[str, Any]] | None, column: str) -> list[Any]:
    if not rows:
        return []
    return [row.get(column) for row in rows[:50] if isinstance(row, dict)]


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


def _non_null(values: Iterable[Any]) -> list[Any]:
    return [v for v in values if v is not None]


def _all_numeric(values: list[Any]) -> bool:
    sample = _non_null(values)
    return bool(sample) and all(_is_number(v) for v in sample)


def _looks_like_date_values(values: list[Any]) -> bool:
    sample = _non_null(values)[:20]
    if not sample:
        return False
    if any(isinstance(v, (_dt.date, _dt.datetime)) for v in sample):
        return True
    for value in sample:
        text = str(value)
        if not re.match(r"^\d{4}[-/]\d{2}([-/]\d{2})?([T ]\d{2}:\d{2}(:\d{2})?)?$", text):
            return False
    return True


def _numeric_part_kind(name: str, values: list[Any]) -> tuple[bool, str | None]:
    sample = _non_null(values)[:20]
    if not sample or not all(_is_number(v) for v in sample):
        return False, None
    nums = [int(float(v)) for v in sample]
    lowered = name.lower()
    if "month" in lowered and all(1 <= n <= 12 for n in nums):
        return True, "month_part"
    if "week" in lowered and all(1 <= n <= 53 for n in nums):
        return True, "week_part"
    if "hour" in lowered and all(0 <= n <= 23 for n in nums):
        return True, "hour_part"
    if "quarter" in lowered or "qtr" in lowered:
        if all(1 <= n <= 4 for n in nums):
            return True, "quarter_part"
    if "year" in lowered and all(1900 <= n <= 2199 for n in nums):
        return True, "year"
    return False, None


def _text_part_kind(values: list[Any]) -> str | None:
    sample = [str(v).strip().lower() for v in _non_null(values)[:20]]
    if not sample:
        return None
    if all(v in _MONTH_NAMES for v in sample):
        return "month_name"
    if all(v in _WEEKDAY_NAMES for v in sample):
        return "weekday_name"
    return None


def _temporal_name_part_notes(name: str) -> list[str]:
    lowered = name.lower()
    if "year" in lowered:
        return ["year"]
    if "month" in lowered and "year" not in lowered:
        return ["month_part", "cyclical_part_not_stable_trend_key"]
    if "week" in lowered and "year" not in lowered:
        return ["week_part", "cyclical_part_not_stable_trend_key"]
    if "weekday" in lowered or "day_of_week" in lowered:
        return ["weekday_name", "calendar_order_required", "cyclical_part_not_stable_trend_key"]
    if "hour" in lowered:
        return ["hour_part", "cyclical_part_not_stable_trend_key"]
    if ("quarter" in lowered or "qtr" in lowered) and "year" not in lowered:
        return ["quarter_part", "cyclical_part_not_stable_trend_key"]
    return []


def _expression_role(ref: DimRef) -> FieldRole | None:
    if not isinstance(ref.node, FuncCall):
        return None
    notes = [f"expression_function={ref.node.fn}"]
    if ref.node.fn == "date_trunc":
        grain = None
        first = ref.node.args[0] if ref.node.args else None
        if isinstance(first, Literal):
            grain = str(first.value).lower()
        notes.append(f"grain={grain}" if grain else "grain=unknown")
        return FieldRole(
            name=ref.alias,
            role=AxisRole.TEMPORAL,
            confidence="expression",
            source="expression",
            notes=notes + [f"base_fields={','.join(sorted(base_field_names(ref.node)))}"],
        )
    if ref.node.fn in {"extract", "date_part"}:
        part = None
        first = ref.node.args[0] if ref.node.args else None
        if isinstance(first, Literal):
            part = str(first.value).lower()
        return FieldRole(
            name=ref.alias,
            role=AxisRole.TEMPORAL,
            confidence="expression",
            source="expression",
            notes=notes + [f"temporal_part={part or 'unknown'}", "stable_period_key_required"],
        )
    return FieldRole(
        name=ref.alias,
        role=AxisRole.CATEGORY,
        confidence="expression",
        source="expression",
        notes=notes + ["scalar_expression_grouping"],
    )


def _dimension_role(
    name: str,
    *,
    values: list[Any],
    model_profiles: Any,
) -> FieldRole:
    profile_role = _profile_dimension_role(model_profiles, name)
    if profile_role:
        return profile_role
    if _looks_like_date_values(values):
        return FieldRole(
            name=name,
            role=AxisRole.TEMPORAL,
            confidence="result",
            source="dimension",
            notes=["result_values_parse_as_date"],
        )
    is_temporal_part, part_kind = _numeric_part_kind(name, values)
    if is_temporal_part:
        notes = [part_kind or "temporal_numeric_part"]
        if part_kind != "year":
            notes.append("cyclical_part_not_stable_trend_key")
        return FieldRole(
            name=name,
            role=AxisRole.TEMPORAL,
            confidence="result" if values else "heuristic",
            source="dimension",
            notes=notes,
        )
    text_part = _text_part_kind(values)
    if text_part:
        return FieldRole(
            name=name,
            role=AxisRole.TEMPORAL,
            confidence="result",
            source="dimension",
            notes=[text_part, "calendar_order_required", "cyclical_part_not_stable_trend_key"],
        )
    if _TEMPORAL_NAME_RE.search(name):
        part_notes = _temporal_name_part_notes(name)
        return FieldRole(
            name=name,
            role=AxisRole.TEMPORAL,
            confidence="heuristic",
            source="dimension",
            notes=["temporal_name_pattern", *part_notes],
        )
    return FieldRole(
        name=name,
        role=AxisRole.CATEGORY,
        confidence="metadata",
        source="dimension",
        notes=["dimension_default_category"],
    )


def _profile_dimension_role(model_profiles: Any, name: str) -> FieldRole | None:
    if not model_profiles:
        return None
    profile = None
    if isinstance(model_profiles, dict):
        dimensions = model_profiles.get("dimensions") or {}
        profile = dimensions.get(name) if isinstance(dimensions, dict) else None
    else:
        dimensions = getattr(model_profiles, "dimensions", None)
        profile = dimensions.get(name) if isinstance(dimensions, dict) else None
    if not profile:
        return None
    kind = profile.get("kind") if isinstance(profile, dict) else getattr(profile, "kind", None)
    is_time_dim = (
        profile.get("is_time_dim") if isinstance(profile, dict) else getattr(profile, "is_time_dim", None)
    )
    time_grain = (
        profile.get("time_grain") if isinstance(profile, dict) else getattr(profile, "time_grain", None)
    )
    if is_time_dim or kind in {"time", "temporal", "date"}:
        notes = [f"profile_kind={kind or 'time'}", *_temporal_name_part_notes(name)]
        if time_grain:
            notes.append(f"time_grain={time_grain}")
        return FieldRole(
            name=name,
            role=AxisRole.TEMPORAL,
            confidence="metadata",
            source="dimension",
            notes=notes,
        )
    if is_time_dim is False or kind in {"category", "categorical", "entity", "geo"}:
        return FieldRole(
            name=name,
            role=AxisRole.CATEGORY,
            confidence="metadata",
            source="dimension",
            notes=["profile_dimension_category"],
        )
    return None


def infer_field_roles(
    *,
    selected_measures: list[str],
    selected_dimensions: list[str],
    dimension_refs: list[DimRef],
    result_columns: list[str] | None,
    result_rows: list[dict[str, Any]] | None,
    model_profiles: Any,
    measure_metadata: dict[str, MeasureRoleMetadata],
    computed_label: str | None = None,
    compound_formula: CompoundFormulaMetadata | None = None,
) -> list[FieldRole]:
    roles: list[FieldRole] = []
    seen: set[str] = set()

    for measure in selected_measures:
        metadata = measure_metadata.get(measure)
        if metadata and metadata.is_time_variant:
            notes = metadata.as_trace()["notes"]
            roles.append(FieldRole(
                name=measure,
                role=ValueRole.TIME_VARIANT_MEASURE,
                confidence="metadata",
                source="measure",
                notes=[
                    f"variant_kind={metadata.variant_kind}",
                    f"canonical_variant_kind={metadata.canonical_variant_kind}",
                    f"variant_family={metadata.time_variant_family}",
                    *notes,
                ],
            ))
        else:
            notes = []
            if metadata:
                notes.extend([
                    f"measure_type={metadata.measure_type}",
                    f"source_kind={metadata.source_kind}",
                    f"default_agg={metadata.default_agg}",
                ])
                notes.extend(metadata.additivity_notes)
            roles.append(FieldRole(
                name=measure,
                role=ValueRole.SINGLE_METRIC,
                confidence="metadata",
                source="measure",
                notes=notes or ["measure_default_metric"],
            ))
        seen.add(measure)

    refs_by_alias = {ref.alias: ref for ref in dimension_refs}
    for dimension in selected_dimensions:
        ref = refs_by_alias.get(dimension)
        role = _expression_role(ref) if ref is not None and not ref.is_bare else None
        if role is None:
            role = _dimension_role(
                dimension,
                values=_row_values(result_rows, dimension),
                model_profiles=model_profiles,
            )
        roles.append(role)
        seen.add(dimension)

    if computed_label:
        notes = ["computed_compound_result"]
        if compound_formula:
            notes.extend([
                f"operations={','.join(compound_formula.operation_kinds)}",
                f"denominators={','.join(compound_formula.denominator_refs)}",
                f"alignment_mode={compound_formula.alignment_mode or 'unknown'}",
                "multi_row" if compound_formula.is_multi_row else "scalar",
            ])
        roles.append(FieldRole(
            name=computed_label,
            role=ValueRole.COMPOUND_METRIC,
            confidence="expression",
            source="computed",
            notes=notes,
        ))
        seen.add(computed_label)

    for column in result_columns or []:
        if column in seen:
            continue
        values = _row_values(result_rows, column)
        if _looks_like_date_values(values):
            roles.append(FieldRole(
                name=column,
                role=AxisRole.TEMPORAL,
                confidence="result",
                source="dimension",
                notes=["unselected_result_column_parse_as_date"],
            ))
        elif _all_numeric(values):
            roles.append(FieldRole(
                name=column,
                role=ValueRole.SINGLE_METRIC,
                confidence="result",
                source="computed",
                notes=["unselected_numeric_result_column"],
            ))
        else:
            roles.append(FieldRole(
                name=column,
                role=AxisRole.CATEGORY,
                confidence="result",
                source="dimension",
                notes=["unselected_result_column_category"],
            ))

    return roles
