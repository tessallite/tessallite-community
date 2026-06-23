"""Config-backed analytical shape matcher."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from src.planning.contracts import FieldRole, ShapeContract, ShapeLimits
from src.planning.enums import AnalyticalShape, AxisRole, ValueRole
from src.planning.intent import AnalyticalIntent


_DEFAULT_REGISTRY_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "shape_registry.json"
)


@dataclass(frozen=True)
class ShapeRegistryEntry:
    id: str
    family: str
    shape: AnalyticalShape
    match: dict[str, Any]
    required_constraints: list[str]
    renderer_binding: dict[str, str]
    data_quality_checks: list[str]
    fallback: Literal["table", "clarify", "refuse"]
    narration_fact_contract: str
    insight_fact_contract: str | None = None


def load_shape_registry(path: str | Path | None = None) -> list[ShapeRegistryEntry]:
    registry_path = Path(path) if path is not None else _DEFAULT_REGISTRY_PATH
    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    families = raw.get("families")
    if not isinstance(families, dict) or not families:
        raise ValueError("shape registry must contain grouped families")

    entries: list[ShapeRegistryEntry] = []
    for family, items in families.items():
        if not isinstance(family, str) or not family:
            raise ValueError("shape registry family names must be non-empty strings")
        if not isinstance(items, list) or not items:
            raise ValueError(f"shape registry family {family!r} must contain entries")
        for item in items:
            entries.append(_parse_registry_entry(family, item))
    return entries


def match_shape_contract(
    *,
    intent: AnalyticalIntent,
    field_roles: list[FieldRole],
    call: Any,
    chart_type_selector: str | None,
    limits: ShapeLimits,
    registry: list[ShapeRegistryEntry] | None = None,
) -> ShapeContract:
    entries = registry if registry is not None else load_shape_registry()
    counts = _role_counts(field_roles)
    selected = _select_entry(entries, intent, counts)
    binding = _bind_renderer(selected, field_roles)
    chart_preference = binding.pop("chart", None)
    table_required = (
        not chart_preference
        or (selected.fallback == "table" and selected.shape == AnalyticalShape.UNSUPPORTED)
    )
    notes = [f"registry_entry={selected.id}", f"registry_family={selected.family}"]

    requested_chart = (
        chart_type_selector
        if chart_type_selector not in {None, "auto", "llm"}
        else None
    )
    if requested_chart == "none":
        chart_preference = None
        table_required = True
        notes.append("chart_disabled")
    elif (
        requested_chart
        and chart_preference
        and not _chart_request_compatible(requested_chart, chart_preference)
    ):
        chart_preference = None
        table_required = True
        notes.append(f"chart_request_rejected={requested_chart}")

    return ShapeContract(
        shape=selected.shape,
        required_axes=_required_axes(selected.shape, counts),
        optional_axes=[],
        value_role=_value_role(field_roles),
        chart_preference=chart_preference,
        table_required=table_required,
        renderer_binding=binding,
        data_quality_checks=list(selected.data_quality_checks),
        narration_fact_contract=selected.narration_fact_contract,
        insight_fact_contract=selected.insight_fact_contract,
        max_chart_rows=limits.max_chart_rows,
        max_categories=limits.max_bar_categories,
        max_series=limits.max_line_series,
        notes=notes,
    )


def _parse_registry_entry(family: str, raw: Any) -> ShapeRegistryEntry:
    if not isinstance(raw, dict):
        raise ValueError(f"shape registry entry in {family!r} must be an object")
    required = {
        "id",
        "shape",
        "match",
        "required_constraints",
        "renderer_binding",
        "data_quality_checks",
        "fallback",
        "narration_fact_contract",
    }
    missing = sorted(required - set(raw))
    if missing:
        raise ValueError(f"shape registry entry missing keys: {', '.join(missing)}")
    try:
        shape = AnalyticalShape(raw["shape"])
    except ValueError as exc:
        raise ValueError(f"invalid registry shape {raw.get('shape')!r}") from exc
    fallback = raw["fallback"]
    if fallback not in {"table", "clarify", "refuse"}:
        raise ValueError(f"invalid fallback {fallback!r}")
    for key in ("match", "renderer_binding"):
        if not isinstance(raw[key], dict):
            raise ValueError(f"shape registry {raw.get('id')} {key} must be an object")
    for key in ("required_constraints", "data_quality_checks"):
        if not isinstance(raw[key], list) or not all(isinstance(v, str) for v in raw[key]):
            raise ValueError(f"shape registry {raw.get('id')} {key} must be list[str]")
    if not isinstance(raw["narration_fact_contract"], str) or not raw["narration_fact_contract"]:
        raise ValueError(f"shape registry {raw.get('id')} requires narration_fact_contract")
    return ShapeRegistryEntry(
        id=str(raw["id"]),
        family=family,
        shape=shape,
        match=dict(raw["match"]),
        required_constraints=list(raw["required_constraints"]),
        renderer_binding={str(k): str(v) for k, v in raw["renderer_binding"].items()},
        data_quality_checks=list(raw["data_quality_checks"]),
        fallback=fallback,
        narration_fact_contract=raw["narration_fact_contract"],
        insight_fact_contract=raw.get("insight_fact_contract"),
    )


def _chart_request_compatible(requested_chart: str, contract_chart: str) -> bool:
    if requested_chart == contract_chart:
        return True
    compatible: dict[str, set[str]] = {
        "line": {"line", "multi_line", "multi_line_wide"},
        "bar": {"bar", "h_bar", "grouped_bar", "stacked_bar"},
        "h_bar": {"h_bar"},
        "grouped_bar": {"grouped_bar"},
        "stacked_bar": {"stacked_bar"},
        "kpi": {"kpi"},
        "pie": {"pie"},
    }
    return contract_chart in compatible.get(requested_chart, {requested_chart})


def _role_counts(field_roles: list[FieldRole]) -> dict[str, int]:
    counts = {
        "axes": 0,
        "temporal": 0,
        "categories": 0,
        "values": 0,
    }
    for role in field_roles:
        if isinstance(role.role, AxisRole):
            counts["axes"] += 1
            if role.role == AxisRole.TEMPORAL:
                counts["temporal"] += 1
            elif role.role in {AxisRole.CATEGORY, AxisRole.SERIES, AxisRole.ORDINAL}:
                counts["categories"] += 1
        elif isinstance(role.role, ValueRole) and role.role != ValueRole.NONE:
            counts["values"] += 1
    return counts


def _select_entry(
    entries: list[ShapeRegistryEntry],
    intent: AnalyticalIntent,
    counts: dict[str, int],
) -> ShapeRegistryEntry:
    intent_entries = [entry for entry in entries if "intent" in entry.match]
    generic_entries = [entry for entry in entries if "intent" not in entry.match]
    matches = [
        entry
        for entry in [*intent_entries, *generic_entries]
        if _entry_matches(entry, intent, counts)
    ]
    if intent.shape_hint is not None:
        for entry in matches:
            if entry.shape == intent.shape_hint:
                return entry
    for entry in matches:
        if _entry_matches(entry, intent, counts):
            return entry
    fallback = next((entry for entry in entries if entry.id == "unsupported_no_values"), None)
    if fallback is None:
        raise ValueError("shape registry has no unsupported fallback entry")
    return fallback


def _entry_matches(
    entry: ShapeRegistryEntry,
    intent: AnalyticalIntent,
    counts: dict[str, int],
) -> bool:
    match = entry.match
    intent_name = match.get("intent")
    if intent_name and not _intent_matches(intent_name, intent):
        return False
    for key in ("axes", "temporal", "categories", "values"):
        if key in match and counts[key] != match[key]:
            return False
        if key != "axes" and key not in match and not _has_range_constraint(match, key):
            if counts[key] != 0 and not _unspecified_counts_are_wildcards(match):
                return False
    for key, count_key in (("min_values", "values"), ("max_values", "values")):
        if key in match:
            value = match[key]
            if key.startswith("min") and counts[count_key] < value:
                return False
            if key.startswith("max") and counts[count_key] > value:
                return False
    if "min_values" in match and counts["values"] < match["min_values"]:
        return False
    if "min_categories" in match and counts["categories"] < match["min_categories"]:
        return False
    if "max_categories" in match and counts["categories"] > match["max_categories"]:
        return False
    return True


def _has_range_constraint(match: dict[str, Any], key: str) -> bool:
    singular = key[:-1] if key.endswith("s") else key
    return f"min_{key}" in match or f"max_{key}" in match or f"min_{singular}" in match or f"max_{singular}" in match


def _unspecified_counts_are_wildcards(match: dict[str, Any]) -> bool:
    return set(match).issubset({"intent", "min_values", "max_values"})


def _intent_matches(name: str, intent: AnalyticalIntent) -> bool:
    raw_records_requested = "raw_records_requested" in intent.notes
    return {
        "ranking": intent.wants_ranking,
        "composition": intent.wants_composition,
        "separate_series": intent.wants_separate_series,
        "detail": intent.wants_detail_rows and not raw_records_requested,
        "raw_records": intent.wants_detail_rows and raw_records_requested,
        "distribution": intent.wants_distribution,
        "scatter": intent.wants_scatter,
    }.get(name, False)


def _bind_renderer(entry: ShapeRegistryEntry, roles: list[FieldRole]) -> dict[str, str]:
    binding = dict(entry.renderer_binding)
    if not binding:
        return binding
    temporal = _first_role_name(roles, AxisRole.TEMPORAL)
    category_names = [
        role.name
        for role in roles
        if role.role in {AxisRole.CATEGORY, AxisRole.SERIES, AxisRole.ORDINAL}
    ]
    category = category_names[0] if category_names else None
    values = [role.name for role in roles if isinstance(role.role, ValueRole) and role.role != ValueRole.NONE]
    replacements = {
        "temporal": temporal,
        "category": category,
        "primary_category": category_names[0] if category_names else None,
        "secondary_category": category_names[1] if len(category_names) > 1 else None,
        "bucket": category,
        "value": values[0] if values else None,
        "values": ",".join(values) if values else None,
    }
    out: dict[str, str] = {}
    for key, value in binding.items():
        replacement = replacements.get(value, value)
        if replacement:
            out[key] = replacement
    return out


def _first_role_name(roles: list[FieldRole], role: AxisRole) -> str | None:
    for item in roles:
        if item.role == role:
            return item.name
    return None


def _required_axes(shape: AnalyticalShape, counts: dict[str, int]) -> list[AxisRole]:
    axes: list[AxisRole] = []
    if counts["temporal"]:
        axes.append(AxisRole.TEMPORAL)
    if shape == AnalyticalShape.MULTI_SERIES_TIME:
        axes.append(AxisRole.SERIES)
    elif counts["categories"]:
        axes.extend([AxisRole.CATEGORY] * counts["categories"])
    return axes


def _value_role(roles: list[FieldRole]) -> ValueRole:
    values = [role.role for role in roles if isinstance(role.role, ValueRole) and role.role != ValueRole.NONE]
    if not values:
        return ValueRole.NONE
    if ValueRole.COMPOUND_METRIC in values:
        return ValueRole.COMPOUND_METRIC
    if ValueRole.TIME_VARIANT_MEASURE in values:
        return ValueRole.TIME_VARIANT_MEASURE
    if len(values) > 1:
        return ValueRole.MULTIPLE_METRICS
    return ValueRole.SINGLE_METRIC
