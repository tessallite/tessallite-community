"""Runtime tool-call validation against the assembled model bundle."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from src.planning.contracts import FieldRole
from src.planning.enums import AnalyticalShape, AxisRole, ValueRole
from src.planning.intent import AnalyticalIntent
from src.tools.expressions import DimRef, normalize_dimension, normalize_dimensions
from src.planning.measure_metadata import MeasureRoleMetadata
from src.tools.spec import (
    CompoundQueryToolCall,
    CreateAggregateToolCall,
    QueryToolCall,
    ToolCall,
)


@dataclass(frozen=True)
class PlanValidationIssue:
    path: str
    reason: str
    field_name: str | None = None
    model_id: str | None = None
    repairable: bool = False

    def as_trace(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "reason": self.reason,
            "field_name": self.field_name,
            "model_id": self.model_id,
            "repairable": self.repairable,
        }


@dataclass(frozen=True)
class ModelFieldIndex:
    model_id: str
    measures: frozenset[str]
    dimensions: frozenset[str]
    filterable_where: frozenset[str]
    sortable: frozenset[str]
    measure_metadata: dict[str, MeasureRoleMetadata] = field(default_factory=dict)

    def available_summary(self) -> str:
        return (
            f"model_id={self.model_id}\n"
            f"measures={', '.join(sorted(self.measures)) or '(none)'}\n"
            f"dimensions={', '.join(sorted(self.dimensions)) or '(none)'}\n"
            f"filterable_where={', '.join(sorted(self.filterable_where)) or '(none)'}\n"
            f"sortable={', '.join(sorted(self.sortable)) or '(none)'}"
        )


def build_model_field_indexes(bundle: Any) -> dict[str, ModelFieldIndex]:
    profiles = list(getattr(bundle, "model_profiles", []) or [])
    indexes: dict[str, ModelFieldIndex] = {}
    for profile in profiles:
        model_id = str(getattr(profile, "id"))
        measures = frozenset(getattr(profile, "measure_names", []) or [])
        dimensions = frozenset(getattr(profile, "dimension_names", []) or [])
        indexes[model_id] = ModelFieldIndex(
            model_id=model_id,
            measures=measures,
            dimensions=dimensions,
            filterable_where=frozenset(getattr(profile, "filterable_where_names", []) or []),
            sortable=frozenset(getattr(profile, "sortable_names", []) or []),
            measure_metadata=dict(getattr(profile, "measure_metadata", {}) or {}),
        )
    return indexes


def _model_index_for(
    model_id: str,
    indexes: dict[str, ModelFieldIndex],
    allowed_model_ids: set[UUID],
    path: str,
) -> tuple[ModelFieldIndex | None, list[PlanValidationIssue]]:
    issues: list[PlanValidationIssue] = []
    try:
        model_uuid = UUID(model_id)
    except (TypeError, ValueError):
        return None, [PlanValidationIssue(
            path=f"{path}.model_id",
            reason="invalid_model_id",
            model_id=model_id,
            repairable=False,
        )]
    if model_uuid not in allowed_model_ids:
        issues.append(PlanValidationIssue(
            path=f"{path}.model_id",
            reason="model_not_allow_listed",
            model_id=model_id,
            repairable=False,
        ))
    index = indexes.get(str(model_uuid))
    if index is None:
        issues.append(PlanValidationIssue(
            path=f"{path}.model_id",
            reason="model_profile_not_available",
            model_id=model_id,
            repairable=False,
        ))
    return index, issues


def _validate_query_like(
    *,
    call: QueryToolCall,
    indexes: dict[str, ModelFieldIndex],
    allowed_model_ids: set[UUID],
    path: str,
    result_labels: set[str] | None = None,
) -> list[PlanValidationIssue]:
    result_labels = result_labels or set()
    index, issues = _model_index_for(call.model_id, indexes, allowed_model_ids, path)
    if index is None:
        return issues

    for pos, measure in enumerate(call.measures):
        if measure not in index.measures:
            issues.append(PlanValidationIssue(
                path=f"{path}.measures[{pos}]",
                reason="unknown_measure",
                field_name=measure,
                model_id=call.model_id,
                repairable=True,
            ))

    selected_dimension_aliases = set(call.dimensions)
    for pos, ref in enumerate(call.dimension_refs or []):
        for field_name in ref.base_fields:
            if field_name not in index.dimensions:
                issues.append(PlanValidationIssue(
                    path=f"{path}.dimensions[{pos}]",
                    reason="unknown_dimension",
                    field_name=field_name,
                    model_id=call.model_id,
                    repairable=True,
                ))

    for pos, item in enumerate(call.where):
        name = item.get("name") if isinstance(item, dict) else None
        if isinstance(name, str) and name not in index.filterable_where:
            issues.append(PlanValidationIssue(
                path=f"{path}.where[{pos}].name",
                reason="field_not_filterable_where",
                field_name=name,
                model_id=call.model_id,
                repairable=True,
            ))

    for pos, item in enumerate(call.having):
        name = item.get("name") if isinstance(item, dict) else None
        if isinstance(name, str) and name not in index.measures:
            issues.append(PlanValidationIssue(
                path=f"{path}.having[{pos}].name",
                reason="having_requires_measure",
                field_name=name,
                model_id=call.model_id,
                repairable=True,
            ))

    # Bug-5349 Phase 2/3 — walk every base field of every STRUCTURED expression
    # (computed projection, structured WHERE/HAVING predicate). A base field is
    # valid when it is a known measure OR dimension; an unknown one is an
    # invented field that must be caught pre-execution, exactly as for bare
    # dimensions above. Bare measures/dimensions/flat filters are validated by
    # the blocks above; this only adds coverage for the expression forms.
    known_fields = index.measures | index.dimensions
    _expr_specs: list[tuple[str, Any]] = []
    for pos, proj in enumerate(getattr(call, "projection_refs", None) or []):
        _expr_specs.append((f"{path}.projections[{pos}]", proj))
    for pos, pref in enumerate(getattr(call, "where_refs", None) or []):
        _expr_specs.append((f"{path}.where[{pos}]", pref))
    for pos, href in enumerate(getattr(call, "having_refs", None) or []):
        _expr_specs.append((f"{path}.having[{pos}]", href))
    for item_path, ref in _expr_specs:
        for field_name in ref.base_fields:
            if field_name not in known_fields:
                issues.append(PlanValidationIssue(
                    path=item_path,
                    reason="unknown_field",
                    field_name=field_name,
                    model_id=call.model_id,
                    repairable=True,
                ))

    selected = set(call.measures) | selected_dimension_aliases | result_labels
    for pos, item in enumerate(call.sort):
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str):
            continue
        if name in selected:
            continue
        if name not in index.sortable:
            issues.append(PlanValidationIssue(
                path=f"{path}.sort[{pos}].name",
                reason="field_not_sortable",
                field_name=name,
                model_id=call.model_id,
                repairable=True,
            ))
    return issues


def validate_tool_call_against_bundle(call: ToolCall, bundle: Any) -> list[PlanValidationIssue]:
    """Bug-5373 — metadata-driven plan validation gate.  Every tool path
    that references semantic fields is validated BEFORE execution so that
    invented fields, wrong-model references, and analytically-unsafe plans
    never reach the query router."""
    if not isinstance(getattr(bundle, "model_profiles", None), list):
        return []
    indexes = build_model_field_indexes(bundle)
    allowed = set(getattr(bundle, "allow_list_model_ids", []) or [])
    if isinstance(call, QueryToolCall):
        return _validate_query_like(
            call=call,
            indexes=indexes,
            allowed_model_ids=allowed,
            path="query",
        )
    if isinstance(call, CompoundQueryToolCall):
        issues: list[PlanValidationIssue] = []
        result_labels = {call.result_label}
        for pos, step in enumerate(call.steps):
            step_call = QueryToolCall(
                model_id=step.model_id,
                measures=step.measures,
                dimensions=step.dimensions,
                where=step.where,
                having=step.having,
                sort=step.sort,
                limit=step.limit,
                limit_explicit=step.limit_explicit,
                dimension_refs=step.dimension_refs,
                # Bug-5349 Phase 2/3 — carry the structured refs so the
                # pre-execution bundle validator walks their base fields too
                # (an invented field hidden in a structured predicate /
                # projection must be caught here, not only downstream).
                projection_refs=step.projection_refs,
                where_refs=step.where_refs,
                having_refs=step.having_refs,
            )
            issues.extend(_validate_query_like(
                call=step_call,
                indexes=indexes,
                allowed_model_ids=allowed,
                path=f"compound_query.steps[{pos}]",
                result_labels=result_labels,
            ))
        return issues
    # Bug-5373 — validate create_aggregate measures/dimensions against
    # the model metadata so invented fields are caught before execution.
    if isinstance(call, CreateAggregateToolCall):
        issues_agg: list[PlanValidationIssue] = []
        index, idx_issues = _model_index_for(
            call.model_id, indexes, allowed, "create_aggregate",
        )
        issues_agg.extend(idx_issues)
        if index is not None:
            for pos, measure in enumerate(call.measures or []):
                if measure not in index.measures:
                    issues_agg.append(PlanValidationIssue(
                        path=f"create_aggregate.measures[{pos}]",
                        reason="unknown_measure",
                        field_name=measure,
                        model_id=call.model_id,
                        repairable=False,
                    ))
            for pos, dimension in enumerate(call.dimensions or []):
                if dimension not in index.dimensions:
                    issues_agg.append(PlanValidationIssue(
                        path=f"create_aggregate.dimensions[{pos}]",
                        reason="unknown_dimension",
                        field_name=dimension,
                        model_id=call.model_id,
                        repairable=False,
                    ))
        return issues_agg
    return []


def validation_trace(issues: list[PlanValidationIssue]) -> dict[str, Any]:
    return {
        "status": "invalid" if issues else "valid",
        "issues": [issue.as_trace() for issue in issues],
    }


def validation_feedback_for_correction(
    issues: list[PlanValidationIssue],
    bundle: Any,
) -> str:
    issue_lines = [
        f"- {i.path}: {i.reason}"
        + (f" field={i.field_name!r}" if i.field_name else "")
        + (f" model_id={i.model_id}" if i.model_id else "")
        for i in issues
    ]
    indexes = build_model_field_indexes(bundle)
    index_lines = [idx.available_summary() for idx in indexes.values()]
    shape_guidance = ""
    if any(i.reason == "cyclical_temporal_part_not_stable_trend_key" for i in issues):
        shape_guidance = (
            "\n\nShape correction guidance:\n"
            "- A month/week/quarter/hour part by itself is a cyclic label, not "
            "a stable trend axis.\n"
            "- For trend questions, use a stable period key: either include the "
            "parent year with the cyclic part, or use a raw date dimension with "
            'a grain shorthand such as {"name": "<date dimension>", "grain": "month"}.\n'
            "- Do not retry the same lone cyclic part dimension.\n"
        )
    return (
        "The tool call references fields or models that are not valid for the "
        "current runtime model bundle.\n\n"
        "Validation issues:\n"
        + "\n".join(issue_lines)
        + "\n\nAvailable runtime fields:\n"
        + "\n\n".join(index_lines)
        + shape_guidance
        + "\n\nReturn a corrected JSON tool call using only the available "
        "models, measures, dimensions, filterable where fields, and sortable "
        "fields above. Do not invent replacement names."
    )


def invalid_plan_message(issues: list[PlanValidationIssue]) -> str:
    first = issues[0] if issues else None
    if first and first.field_name:
        return (
            f"The field '{first.field_name}' is not available in the selected "
            "model for this question. Please rephrase using the fields "
            "available to you."
        )
    shape_reasons = {
        "ranking_requires_category_axis",
        "ranking_requires_value",
        "trend_requires_temporal_axis",
        "multi_series_time_requires_temporal_axis",
        "multi_series_time_requires_series_axis",
        "multi_series_time_requires_one_value",
        "cyclical_temporal_part_not_stable_trend_key",
        "composition_requires_additive_values",
        "kpi_requires_no_grouping_axes",
    }
    if first and first.reason in shape_reasons:
        return (
            "I could not shape the selected fields into the requested analytical "
            "answer. Please rephrase with the metric, grouping, and time period "
            "you want to compare."
        )
    return (
        "The selected model or fields are not available for this question. "
        "Please rephrase using the models and fields available to you."
    )


def validate_shape_contract_before_execution(
    call: ToolCall,
    intent: AnalyticalIntent,
    field_roles: list[FieldRole],
) -> list[PlanValidationIssue]:
    axes = [role for role in field_roles if isinstance(role.role, AxisRole)]
    temporal = [role for role in axes if role.role == AxisRole.TEMPORAL]
    categories = [
        role for role in axes
        if role.role in {AxisRole.CATEGORY, AxisRole.SERIES, AxisRole.ORDINAL}
    ]
    values = [
        role for role in field_roles
        if isinstance(role.role, ValueRole) and role.role != ValueRole.NONE
    ]
    issues: list[PlanValidationIssue] = []

    if "raw_records_requested" in intent.notes:
        issues.append(PlanValidationIssue(
            path="shape.intent",
            reason="raw_records_not_supported_by_aggregate_tools",
            repairable=False,
        ))

    if intent.shape_hint == AnalyticalShape.KPI and axes:
        issues.append(PlanValidationIssue(
            path="shape.axes",
            reason="kpi_requires_no_grouping_axes",
            field_name=axes[0].name,
            repairable=True,
        ))

    scalar_superlative = intent.wants_ranking and values and not categories
    if intent.wants_ranking and not scalar_superlative:
        if not categories:
            issues.append(PlanValidationIssue(
                path="shape.ranking",
                reason="ranking_requires_category_axis",
                repairable=True,
            ))
        if not values:
            issues.append(PlanValidationIssue(
                path="shape.ranking",
                reason="ranking_requires_value",
                repairable=True,
            ))
        if isinstance(call, QueryToolCall) and values and not call.sort:
            issues.append(PlanValidationIssue(
                path="query.sort",
                reason="ranking_sort_missing",
                field_name=values[0].name,
                model_id=getattr(call, "model_id", None),
                repairable=True,
            ))

    if intent.wants_trend and not temporal:
        issues.append(PlanValidationIssue(
            path="shape.temporal_axis",
            reason="trend_requires_temporal_axis",
            repairable=True,
        ))
    if intent.wants_trend and temporal:
        cyclical = [
            role for role in temporal
            if "cyclical_part_not_stable_trend_key" in role.notes
        ]
        stable = [
            role for role in temporal
            if "cyclical_part_not_stable_trend_key" not in role.notes
        ]
        if cyclical and not stable:
            issues.append(PlanValidationIssue(
                path="shape.temporal_axis",
                reason="cyclical_temporal_part_not_stable_trend_key",
                field_name=cyclical[0].name,
                repairable=True,
            ))

    if intent.wants_separate_series:
        if not temporal:
            issues.append(PlanValidationIssue(
                path="shape.multi_series_time.temporal",
                reason="multi_series_time_requires_temporal_axis",
                repairable=True,
            ))
        if not categories:
            issues.append(PlanValidationIssue(
                path="shape.multi_series_time.series",
                reason="multi_series_time_requires_series_axis",
                repairable=True,
            ))
        if len(values) != 1:
            issues.append(PlanValidationIssue(
                path="shape.multi_series_time.values",
                reason="multi_series_time_requires_one_value",
                repairable=True,
            ))

    if intent.shape_hint == AnalyticalShape.STACKED_COMPOSITION:
        non_additive = [
            role for role in field_roles
            if role.role in {ValueRole.SINGLE_METRIC, ValueRole.TIME_VARIANT_MEASURE}
            and any("non_additive" in note for note in role.notes)
        ]
        if non_additive:
            issues.append(PlanValidationIssue(
                path="shape.composition.values",
                reason="composition_requires_additive_values",
                field_name=non_additive[0].name,
                repairable=False,
            ))

    return issues


def _profile_for_call(call: QueryToolCall, bundle: Any | None) -> Any | None:
    if bundle is None:
        return None
    for profile in getattr(bundle, "model_profiles", []) or []:
        if str(getattr(profile, "id", "")) == str(call.model_id):
            return profile
    return None


def _looks_like_stable_date_dimension(name: str) -> bool:
    lowered = name.lower()
    if any(part in lowered for part in (
        "month", "week", "quarter", "qtr", "year", "weekday", "day_of_week",
    )):
        return False
    return "date" in lowered or "timestamp" in lowered or lowered.endswith("_time")


def _stable_date_dimension_for_call(
    call: QueryToolCall,
    bundle: Any | None,
) -> str | None:
    profile = _profile_for_call(call, bundle)
    names = list(getattr(profile, "dimension_names", []) or [])
    if not names:
        return None
    candidates = [name for name in names if _looks_like_stable_date_dimension(name)]
    if not candidates:
        return None

    def score(name: str) -> tuple[int, str]:
        lowered = name.lower()
        if lowered == "business_date":
            return (0, lowered)
        if lowered.endswith("_date"):
            return (1, lowered)
        if "date" in lowered:
            return (2, lowered)
        if "timestamp" in lowered:
            return (3, lowered)
        return (4, lowered)

    return sorted(candidates, key=score)[0]


def _year_dimension_for_cyclical_role(
    role_name: str,
    bundle: Any | None,
    call: QueryToolCall,
) -> str | None:
    profile = _profile_for_call(call, bundle)
    names = set(getattr(profile, "dimension_names", []) or [])
    candidates: list[str] = []
    lowered = role_name.lower()
    for marker in ("month", "week", "quarter", "qtr"):
        if marker in lowered:
            candidates.append(role_name[:lowered.index(marker)] + "year")
    candidates.extend([
        role_name.replace("_month", "_year"),
        role_name.replace("_week", "_year"),
        role_name.replace("_quarter", "_year"),
        role_name.replace("_qtr", "_year"),
    ])
    for candidate in candidates:
        if candidate != role_name and "year" in candidate.lower() and candidate in names:
            return candidate
    return None


def _grain_for_cyclical_role(
    role: FieldRole,
    intent: AnalyticalIntent,
) -> str:
    if intent.requested_grain in {"year", "quarter", "month", "week", "day", "hour"}:
        return intent.requested_grain
    notes = set(role.notes)
    if "week_part" in notes:
        return "week"
    if "quarter_part" in notes:
        return "quarter"
    if "hour_part" in notes:
        return "hour"
    return "month"


def _grain_from_generated_temporal_alias(alias: str) -> str | None:
    lowered = alias.lower()
    for suffix, grain in (
        ("_month", "month"),
        ("_week", "week"),
        ("_quarter", "quarter"),
        ("_qtr", "quarter"),
        ("_day", "day"),
        ("_hour", "hour"),
    ):
        if lowered.endswith(suffix):
            return grain
    return None


def _replace_dimension_ref(
    call: QueryToolCall,
    old_alias: str,
    replacement: DimRef,
) -> None:
    refs = list(call.dimension_refs or [])
    replaced = False
    new_refs: list[DimRef] = []
    for ref in refs:
        if ref.alias == old_alias:
            new_refs.append(replacement)
            replaced = True
        else:
            new_refs.append(ref)
    if not replaced:
        new_refs = [
            replacement if dimension == old_alias else ref
            for dimension, ref in zip(call.dimensions, refs, strict=False)
        ]
    call.dimension_refs = new_refs
    call.dimensions = [ref.alias for ref in new_refs]


def _replace_dimension_ref_with_many(
    call: QueryToolCall,
    old_alias: str,
    replacements: list[DimRef],
) -> None:
    refs = list(call.dimension_refs or [])
    new_refs: list[DimRef] = []
    replaced = False
    existing_aliases: set[str] = set()
    for ref in refs:
        if ref.alias == old_alias:
            for replacement in replacements:
                if replacement.alias not in existing_aliases:
                    new_refs.append(replacement)
                    existing_aliases.add(replacement.alias)
            replaced = True
        elif ref.alias not in existing_aliases:
            new_refs.append(ref)
            existing_aliases.add(ref.alias)
    if not replaced:
        new_refs = refs
    call.dimension_refs = new_refs
    call.dimensions = [ref.alias for ref in new_refs]


def _rename_sort_dimension(
    call: QueryToolCall,
    old_alias: str,
    new_alias: str,
) -> None:
    if old_alias == new_alias:
        return
    for item in call.sort:
        if isinstance(item, dict) and item.get("name") == old_alias:
            item["name"] = new_alias


def _replace_sort_with_temporal_parts(
    call: QueryToolCall,
    old_alias: str,
    year_alias: str,
    part_alias: str,
) -> None:
    replaced = False
    new_sort: list[dict[str, Any]] = []
    for item in call.sort:
        if isinstance(item, dict) and item.get("name") == old_alias:
            direction = item.get("direction", "asc")
            new_sort.append({"name": year_alias, "direction": direction})
            new_sort.append({"name": part_alias, "direction": direction})
            replaced = True
        else:
            new_sort.append(item)
    if replaced:
        call.sort = new_sort


def _grain_from_ref(ref: DimRef) -> str | None:
    raw = ref.raw
    if isinstance(raw, dict):
        grain = raw.get("grain")
        if isinstance(grain, str):
            return grain.lower()
    return _grain_from_generated_temporal_alias(ref.alias)


def _prefer_semantic_temporal_parts(
    call: QueryToolCall,
    bundle: Any | None,
) -> bool:
    profile = _profile_for_call(call, bundle)
    names = set(getattr(profile, "dimension_names", []) or [])
    if not names:
        return False
    repaired = False
    refs = call.dimension_refs or normalize_dimensions(call.dimensions)
    call.dimension_refs = refs
    for ref in list(refs):
        grain = _grain_from_ref(ref)
        if grain not in {"month", "week", "quarter"}:
            continue
        if ref.alias not in names:
            continue
        year_dimension = _year_dimension_for_cyclical_role(ref.alias, bundle, call)
        if not year_dimension:
            continue
        replacements = normalize_dimensions([year_dimension, ref.alias])
        _replace_dimension_ref_with_many(call, ref.alias, replacements)
        _replace_sort_with_temporal_parts(call, ref.alias, year_dimension, ref.alias)
        repaired = True
    return repaired


def _repair_temporal_sort_to_selected_parts(
    call: QueryToolCall,
    bundle: Any | None,
) -> bool:
    date_dimension = _stable_date_dimension_for_call(call, bundle)
    if not date_dimension:
        return False
    selected = set(call.dimensions)
    repaired = False
    new_sort: list[dict[str, Any]] = []
    for item in call.sort:
        if not isinstance(item, dict) or item.get("name") != date_dimension:
            new_sort.append(item)
            continue
        part_aliases = [
            candidate
            for candidate in (
                f"{date_dimension}_year",
                f"{date_dimension}_quarter",
                f"{date_dimension}_month",
                f"{date_dimension}_week",
                f"{date_dimension}_day",
            )
            if candidate in selected
        ]
        if len(part_aliases) < 2:
            new_sort.append(item)
            continue
        direction = item.get("direction", "asc")
        new_sort.extend({"name": alias, "direction": direction} for alias in part_aliases)
        repaired = True
    if repaired:
        call.sort = new_sort
    return repaired


def apply_pre_validation_repairs(
    call: ToolCall,
    intent: AnalyticalIntent,
    bundle: Any | None = None,
) -> bool:
    """Repair generated temporal aliases before bundle field validation.

    Planner retries sometimes produce a bare alias such as
    ``business_date_month`` for a trend prompt. That alias is executable only
    when represented as the grain shorthand
    ``{"name": "business_date", "grain": "month"}``; otherwise field
    validation rejects it before the shape repair layer can run.
    """
    if not isinstance(call, QueryToolCall) or not intent.wants_trend:
        return False
    repaired = _prefer_semantic_temporal_parts(call, bundle)
    repaired = _repair_temporal_sort_to_selected_parts(call, bundle) or repaired
    if repaired:
        return True
    repaired = False
    refs = call.dimension_refs or normalize_dimensions(call.dimensions)
    call.dimension_refs = refs
    for ref in list(refs):
        if not ref.is_bare:
            continue
        grain = _grain_from_generated_temporal_alias(ref.alias)
        if grain is None:
            continue
        date_dimension = _stable_date_dimension_for_call(call, bundle)
        if not date_dimension:
            continue
        expected_alias = f"{date_dimension}_{grain}"
        if ref.alias != expected_alias:
            continue
        taken = {item.alias for item in refs if item.alias != ref.alias}
        replacement = normalize_dimension(
            {"name": date_dimension, "grain": grain},
            taken,
        )
        _replace_dimension_ref(call, ref.alias, replacement)
        _rename_sort_dimension(call, ref.alias, replacement.alias)
        repaired = True
    return repaired


def _repair_cyclical_trend_axis(
    call: QueryToolCall,
    intent: AnalyticalIntent,
    field_roles: list[FieldRole],
    bundle: Any | None,
) -> bool:
    if not intent.wants_trend:
        return False
    temporal_roles = [
        role for role in field_roles
        if isinstance(role.role, AxisRole) and role.role == AxisRole.TEMPORAL
    ]
    cyclical = [
        role for role in temporal_roles
        if "cyclical_part_not_stable_trend_key" in role.notes
    ]
    stable = [
        role for role in temporal_roles
        if "cyclical_part_not_stable_trend_key" not in role.notes
    ]
    if not cyclical or stable:
        return False
    repaired = False
    for role in cyclical:
        year_dimension = _year_dimension_for_cyclical_role(role.name, bundle, call)
        if year_dimension:
            dimensions: list[str] = []
            for dimension in call.dimensions:
                if dimension == role.name and year_dimension not in dimensions:
                    dimensions.append(year_dimension)
                dimensions.append(dimension)
            call.dimensions = dimensions
            call.dimension_refs = normalize_dimensions(dimensions)
            call.sort = [
                {"name": year_dimension, "direction": "asc"},
                {"name": role.name, "direction": "asc"},
            ]
            repaired = True
            continue
        date_dimension = _stable_date_dimension_for_call(call, bundle)
        if not date_dimension:
            continue
        grain = _grain_for_cyclical_role(role, intent)
        taken = {ref.alias for ref in call.dimension_refs or [] if ref.alias != role.name}
        replacement = normalize_dimension(
            {"name": date_dimension, "grain": grain},
            taken,
        )
        _replace_dimension_ref(call, role.name, replacement)
        repaired = True
    return repaired


def _repair_ranking_shape(
    call: QueryToolCall,
    intent: AnalyticalIntent,
    field_roles: list[FieldRole],
) -> bool:
    if not isinstance(call, QueryToolCall):
        return False
    categories = [
        role for role in field_roles
        if isinstance(role.role, AxisRole)
        and role.role in {AxisRole.CATEGORY, AxisRole.SERIES, AxisRole.ORDINAL}
    ]
    if not categories:
        return False
    if not intent.wants_ranking or call.sort:
        return False
    values = [
        role for role in field_roles
        if isinstance(role.role, ValueRole) and role.role != ValueRole.NONE
    ]
    if not values:
        return False
    call.sort.append({
        "name": values[0].name,
        "direction": intent.ranking_direction or "desc",
    })
    if not call.limit_explicit:
        call.limit = intent.requested_limit or 10
        call.limit_explicit = True
    return True


def apply_shape_contract_repairs(
    call: ToolCall,
    intent: AnalyticalIntent,
    field_roles: list[FieldRole],
    bundle: Any | None = None,
) -> bool:
    if not isinstance(call, QueryToolCall):
        return False
    repaired = _repair_cyclical_trend_axis(call, intent, field_roles, bundle)
    return _repair_ranking_shape(call, intent, field_roles) or repaired
