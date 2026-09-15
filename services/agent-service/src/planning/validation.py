"""Runtime tool-call validation against the assembled model bundle."""
from __future__ import annotations

from copy import deepcopy
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from src.planning.contracts import FieldRole
from src.planning.enums import AnalyticalShape, AxisRole, ValueRole
from src.planning.intent import AnalyticalIntent
from src.tools.expressions import (
    DimRef,
    ExpressionError,
    is_structured_predicate,
    normalize_dimension,
    normalize_dimensions,
    normalize_filter,
)
from src.planning.measure_metadata import MeasureRoleMetadata
from src.tools.spec import (
    CompoundQueryToolCall,
    CreateAggregateToolCall,
    EvaluateKpiToolCall,
    PreviewNamedSetToolCall,
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


# ---------------------------------------------------------------------------
# Bug-9738 — benchmark containment (stage 1)
#
# A "how does X compare with the average" plan is wrong whenever the peer
# figure it divides by was never computed. Two shapes reach execution today
# and both publish a plausible wrong ratio:
#
#   (a) a category TOTAL divided by an UNGROUPED per-row MEAN. Both steps
#       return one row so grain alignment sees nothing wrong; the units do
#       not match (an amount over an amount-per-row).
#   (b) a single-slice SCALAR divided by a step GROUPED BY the very dimension
#       the slice pins. The evaluator broadcasts the scalar across the grouped
#       rows, so each row is compared with its own total — the slice's own row
#       is ``X / (X / k) = k`` for any data.
#
# The signal is metadata that already exists (``default_agg`` per measure) plus
# the step shapes, so containment is decidable before any SQL is issued. The
# cross-row reducer that would make (b) expressible is out of scope here.
# ---------------------------------------------------------------------------

_TOTAL_AGGS = frozenset({"sum", "count", "count_distinct"})
_MEAN_AGGS = frozenset({"avg", "average", "mean"})
_DIVISION_OPS = frozenset({"div", "floordiv"})

BENCHMARK_TOTAL_OVER_UNGROUPED_MEAN = "benchmark_total_over_ungrouped_mean"
BENCHMARK_PEER_GRAIN_NEVER_REDUCED = "benchmark_peer_grain_never_reduced"
BENCHMARK_SHARE_REPLACES_REQUESTED_BREAKDOWN = (
    "benchmark_share_replaces_requested_breakdown"
)

_ABSOLUTE_TOTALS_BENCHMARK_RE = re.compile(
    r"\btotal\b.*\bfor\s+each\b.*\b(?:above|below)\b.*\baverage\b",
    re.I,
)
_COMPOSITION_WORD_RE = re.compile(
    r"\b(?:share|percentage|percent|proportion|contribution|mix)\b",
    re.I,
)
_GROUP_AVERAGE_RE = re.compile(
    r"\baverage\b.{0,120}\b(?:across|among)\b.{0,120}"
    r"\b(?:groups?|categories|types)\b",
    re.I,
)


@dataclass(frozen=True)
class BenchmarkContainment:
    """One detected benchmark whose peer figure was never computed."""

    reason: str
    slice_step: str
    peer_step: str
    slice_dimensions: tuple[str, ...]
    measure: str

    @property
    def breakdown_dimension(self) -> str | None:
        """The dimension to break down by, or None when the plan is ambiguous."""
        if len(self.slice_dimensions) == 1:
            return self.slice_dimensions[0]
        return None


def _expression_step_refs(node: Any) -> list[tuple[str, str]]:
    """Collect ``(step, measure)`` pairs from a subtree, in document order.

    Deliberately tolerant: the combine tree's own schema check runs later in
    the compound branch, and a malformed subtree must not raise here.
    """
    found: list[tuple[str, str]] = []

    def walk(current: Any) -> None:
        if isinstance(current, list):
            for item in current:
                walk(item)
            return
        if not isinstance(current, dict):
            return
        ref = current.get("ref")
        if isinstance(ref, dict):
            step = ref.get("step")
            measure = ref.get("measure")
            if isinstance(step, str) and isinstance(measure, str):
                found.append((step, measure))
        walk(current.get("args"))

    walk(node)
    return found


def _division_operands(node: Any) -> list[tuple[Any, Any]]:
    """Every ``(numerator, denominator)`` pair in the tree, outermost first."""
    pairs: list[tuple[Any, Any]] = []

    def walk(current: Any) -> None:
        if isinstance(current, list):
            for item in current:
                walk(item)
            return
        if not isinstance(current, dict):
            return
        args = current.get("args")
        if (
            current.get("op") in _DIVISION_OPS
            and isinstance(args, list)
            and len(args) == 2
        ):
            pairs.append((args[0], args[1]))
        walk(args)

    walk(node)
    return pairs


def _single_value_slice_dimensions(step: Any, index: ModelFieldIndex) -> list[str]:
    """Dimensions this step pins to exactly one value, in clause order."""
    pinned: list[str] = []
    for item in getattr(step, "where", None) or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or name not in index.dimensions:
            continue
        op = item.get("op")
        value = item.get("value")
        if op == "eq" and not isinstance(value, (list, tuple)):
            pinned.append(name)
        elif op == "in" and isinstance(value, (list, tuple)) and len(value) == 1:
            pinned.append(name)
    return pinned


def _restricted_field_names(step: Any) -> set[str]:
    return {
        item["name"]
        for item in getattr(step, "where", None) or []
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def _aggregation_family(measure: str, index: ModelFieldIndex) -> str | None:
    metadata = index.measure_metadata.get(measure)
    if metadata is None:
        return None
    agg = (metadata.default_agg or "").lower()
    if agg in _TOTAL_AGGS:
        return "total"
    if agg in _MEAN_AGGS:
        return "mean"
    return None


def _absolute_totals_benchmark_request(user_message: str | None) -> bool:
    """Recognize the narrow stage-1 totals-plus-benchmark request.

    A compound plan is needed for a real cross-row average, but that reducer
    remains outside this release. For the demonstrated request, the safe
    primary output is the grouped absolute total. The composition-word guard
    keeps an explicit share question on its existing compound path.
    """
    text = " ".join((user_message or "").lower().split())
    return bool(
        text
        and _ABSOLUTE_TOTALS_BENCHMARK_RE.search(text)
        and not _COMPOSITION_WORD_RE.search(text)
    )


def _average_across_groups_request(user_message: str | None) -> bool:
    """True only when the question establishes a cross-group benchmark.

    Scalar broadcasting and total/mean division are both valid operations for
    other explicit questions. Their expression shape alone cannot authorize a
    replacement grouped breakdown.
    """
    text = " ".join((user_message or "").lower().split())
    return bool(
        text
        and not _COMPOSITION_WORD_RE.search(text)
        and (
            _absolute_totals_benchmark_request(text)
            or _GROUP_AVERAGE_RE.search(text)
        )
    )


def is_absolute_totals_benchmark_request(user_message: str | None) -> bool:
    """Expose the stage-1 request classifier to result-shape consumers.

    The benchmark containment repair and the post-query narration facts must
    agree on the same narrow request boundary. Keeping the public wrapper here
    avoids a second, drifting regular expression in the shape layer.
    """
    return _absolute_totals_benchmark_request(user_message)


def _detect_absolute_totals_share_substitution(
    call: CompoundQueryToolCall,
    bundle: Any,
    user_message: str | None,
) -> BenchmarkContainment | None:
    """Find a share-shaped compound plan replacing requested absolute totals.

    This is intentionally user-message gated. The same grouped-over-ungrouped
    division is a valid share-of-total query when the user asks for a share;
    without the demonstrated totals-plus-benchmark intent there is no safe
    basis to reinterpret it.
    """
    if not _absolute_totals_benchmark_request(user_message):
        return None
    indexes = build_model_field_indexes(bundle)
    steps = {step.name: step for step in call.steps}
    for numerator, denominator in _division_operands(call.expression):
        numerator_refs = _expression_step_refs(numerator)
        denominator_refs = _expression_step_refs(denominator)
        numerator_steps = {step for step, _ in numerator_refs}
        denominator_steps = {step for step, _ in denominator_refs}
        if len(numerator_steps) != 1 or len(denominator_steps) != 1:
            continue
        grouped_name = next(iter(numerator_steps))
        overall_name = next(iter(denominator_steps))
        if grouped_name == overall_name:
            continue
        grouped = steps.get(grouped_name)
        overall = steps.get(overall_name)
        if grouped is None or overall is None:
            continue
        if str(grouped.model_id) != str(overall.model_id):
            continue
        if len(grouped.dimensions) != 1 or overall.dimensions:
            continue
        dimension = grouped.dimensions[0]
        if not isinstance(dimension, str):
            continue
        index = indexes.get(str(grouped.model_id))
        if index is None or dimension not in index.dimensions:
            continue
        grouped_measures = [
            measure for step, measure in numerator_refs if step == grouped_name
        ]
        overall_measures = [
            measure for step, measure in denominator_refs if step == overall_name
        ]
        if (
            len(grouped_measures) != 1
            or grouped_measures != overall_measures
            or grouped_measures[0] not in index.measures
        ):
            continue
        return BenchmarkContainment(
            reason=BENCHMARK_SHARE_REPLACES_REQUESTED_BREAKDOWN,
            slice_step=grouped_name,
            peer_step=overall_name,
            slice_dimensions=(dimension,),
            measure=grouped_measures[0],
        )
    return None


def _subtraction_operands(node: Any) -> list[tuple[Any, Any]]:
    """Return every binary subtraction pair in an expression tree."""
    pairs: list[tuple[Any, Any]] = []

    def walk(current: Any) -> None:
        if isinstance(current, list):
            for item in current:
                walk(item)
            return
        if not isinstance(current, dict):
            return
        args = current.get("args")
        if (
            current.get("op") == "sub"
            and isinstance(args, list)
            and len(args) == 2
        ):
            pairs.append((args[0], args[1]))
        walk(args)

    walk(node)
    return pairs


def _is_numeric_const(node: Any) -> bool:
    """Return whether an expression node is a positive finite numeric constant."""
    if not isinstance(node, dict) or set(node) != {"const"}:
        return False
    value = node.get("const")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return value > 0 and math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _detect_absolute_totals_difference_substitution(
    call: CompoundQueryToolCall,
    bundle: Any,
    user_message: str | None,
) -> BenchmarkContainment | None:
    """Find ``grouped_total - (overall_total / N)`` for the totals request.

    Stage 1 does not implement a cross-row average reducer. The captured
    planner shape nevertheless has enough information to identify that it
    replaced the requested absolute grouped output with a derived difference.
    It is repaired only for the exact totals-plus-benchmark request; a genuine
    difference question remains on the existing compound path.
    """
    if not _absolute_totals_benchmark_request(user_message):
        return None
    indexes = build_model_field_indexes(bundle)
    steps = {step.name: step for step in call.steps}
    for grouped_node, average_node in _subtraction_operands(call.expression):
        average_args = (
            average_node.get("args")
            if isinstance(average_node, dict)
            and average_node.get("op") in _DIVISION_OPS
            else None
        )
        if not isinstance(average_args, list) or len(average_args) != 2:
            continue
        if not _is_numeric_const(average_args[1]):
            continue
        grouped_refs = _expression_step_refs(grouped_node)
        overall_refs = _expression_step_refs(average_args[0])
        grouped_steps = {step for step, _ in grouped_refs}
        overall_steps = {step for step, _ in overall_refs}
        if len(grouped_steps) != 1 or len(overall_steps) != 1:
            continue
        grouped_name = next(iter(grouped_steps))
        overall_name = next(iter(overall_steps))
        if grouped_name == overall_name:
            continue
        grouped = steps.get(grouped_name)
        overall = steps.get(overall_name)
        if grouped is None or overall is None:
            continue
        if str(grouped.model_id) != str(overall.model_id):
            continue
        if len(grouped.dimensions) != 1 or overall.dimensions:
            continue
        dimension = grouped.dimensions[0]
        if not isinstance(dimension, str):
            continue
        index = indexes.get(str(grouped.model_id))
        if index is None or dimension not in index.dimensions:
            continue
        grouped_measures = [
            measure for step, measure in grouped_refs if step == grouped_name
        ]
        overall_measures = [
            measure for step, measure in overall_refs if step == overall_name
        ]
        if (
            len(grouped_measures) != 1
            or grouped_measures != overall_measures
            or grouped_measures[0] not in index.measures
        ):
            continue
        return BenchmarkContainment(
            reason=BENCHMARK_SHARE_REPLACES_REQUESTED_BREAKDOWN,
            slice_step=grouped_name,
            peer_step=overall_name,
            slice_dimensions=(dimension,),
            measure=grouped_measures[0],
        )
    return None


def detect_benchmark_containment(
    call: ToolCall,
    bundle: Any,
    user_message: str | None = None,
) -> BenchmarkContainment | None:
    """Return the containment record for a benchmark whose peer figure is fake.

    Every repair is question-aware. Scalar/group broadcasting and total divided
    by a row mean are valid when explicitly requested, so the expression shape
    alone never authorizes replacing the user's calculation.
    """
    if not isinstance(call, CompoundQueryToolCall):
        return None
    if not isinstance(getattr(bundle, "model_profiles", None), list):
        return None
    if not _average_across_groups_request(user_message):
        return None
    indexes = build_model_field_indexes(bundle)
    steps = {step.name: step for step in call.steps}

    for numerator, denominator in _division_operands(call.expression):
        numerator_refs = _expression_step_refs(numerator)
        denominator_refs = _expression_step_refs(denominator)
        numerator_steps = {step for step, _ in numerator_refs}
        denominator_steps = {step for step, _ in denominator_refs}
        if len(numerator_steps) != 1 or len(denominator_steps) != 1:
            continue
        slice_name = next(iter(numerator_steps))
        peer_name = next(iter(denominator_steps))
        if slice_name == peer_name:
            continue
        slice_step = steps.get(slice_name)
        peer_step = steps.get(peer_name)
        if slice_step is None or peer_step is None:
            continue
        # The benchmarked side must be one figure; a grouped numerator is a
        # row-aligned comparison, which is a different (working) shape.
        if slice_step.dimensions:
            continue
        if str(peer_step.model_id) != str(slice_step.model_id):
            continue
        index = indexes.get(str(slice_step.model_id))
        if index is None:
            continue
        pinned = _single_value_slice_dimensions(slice_step, index)
        if not pinned:
            continue
        slice_measures = [
            measure for step, measure in numerator_refs if step == slice_name
        ]
        peer_measures = [
            measure for step, measure in denominator_refs if step == peer_name
        ]
        if not slice_measures or not peer_measures:
            continue

        # (b) the peer step is grouped by the dimension the slice pins, so the
        # divisor is each group's own value rather than an aggregate over them.
        peer_dimensions = set(peer_step.dimensions or [])
        grouped_on = [name for name in pinned if name in peer_dimensions]
        if grouped_on:
            return BenchmarkContainment(
                reason=BENCHMARK_PEER_GRAIN_NEVER_REDUCED,
                slice_step=slice_name,
                peer_step=peer_name,
                slice_dimensions=tuple(grouped_on),
                measure=slice_measures[0],
            )

        # (a) a category total over a per-row mean of a wider population.
        if peer_step.dimensions:
            continue
        peer_restricted = _restricted_field_names(peer_step)
        open_dimensions = [name for name in pinned if name not in peer_restricted]
        if not open_dimensions:
            continue
        totals = [
            measure for measure in slice_measures
            if _aggregation_family(measure, index) == "total"
        ]
        if not totals:
            continue
        if any(
            _aggregation_family(measure, index) != "mean"
            for measure in peer_measures
        ):
            continue
        return BenchmarkContainment(
            reason=BENCHMARK_TOTAL_OVER_UNGROUPED_MEAN,
            slice_step=slice_name,
            peer_step=peer_name,
            slice_dimensions=tuple(open_dimensions),
            measure=totals[0],
        )
    substitution = _detect_absolute_totals_share_substitution(
        call, bundle, user_message,
    )
    if substitution is not None:
        return substitution
    return _detect_absolute_totals_difference_substitution(
        call, bundle, user_message,
    )


def repair_benchmark_to_grouped_breakdown(
    call: ToolCall,
    containment: BenchmarkContainment,
    bundle: Any,
) -> QueryToolCall | None:
    """Rewrite a contained benchmark as the grouped breakdown it asked for.

    The slice step's own restriction on the breakdown dimension is dropped —
    "against the other account types" needs every account type — and every
    other restriction (a date window, a channel filter) is preserved. Returns
    None when the safe breakdown cannot be derived, in which case validation
    refuses the plan instead.
    """
    if not isinstance(call, CompoundQueryToolCall):
        return None
    dimension = containment.breakdown_dimension
    if dimension is None:
        return None
    step = next(
        (item for item in call.steps if item.name == containment.slice_step),
        None,
    )
    peer_step = next(
        (item for item in call.steps if item.name == containment.peer_step),
        None,
    )
    if step is None or peer_step is None:
        return None
    # An aggregate threshold or a computed projection on the scalar step has no
    # single meaning once the step is grouped; refuse rather than guess.
    if step.having or step.having_refs or step.projection_refs:
        return None

    indexes = build_model_field_indexes(bundle)
    allowed = set(getattr(bundle, "allow_list_model_ids", []) or [])
    index, index_issues = _model_index_for(
        step.model_id, indexes, allowed, "compound_query",
    )
    if index is None or index_issues:
        return None
    measure = containment.measure
    if measure not in index.measures or dimension not in index.dimensions:
        return None

    retained: list[dict[str, Any]] = []
    for item in step.where or []:
        if not isinstance(item, dict):
            return None
        name = item.get("name")
        if not isinstance(name, str):
            return None
        if name == dimension:
            op = item.get("op")
            value = item.get("value")
            single_value = (
                (op == "eq" and not isinstance(value, (list, tuple)))
                or (op == "in" and isinstance(value, (list, tuple)) and len(value) == 1)
            )
            if single_value:
                continue
            return None
        if name not in index.filterable_where:
            return None
        retained.append(dict(item))

    # A replacement breakdown is faithful only when both operands cover the
    # same population after removing the scalar's single category pin. Do not
    # discard a different period or cohort merely because the expression has a
    # familiar benchmark shape.
    def filter_signature(items: list[dict[str, Any]]) -> tuple[str, ...]:
        return tuple(
            sorted(
                json.dumps(item, sort_keys=True, separators=(",", ":"), default=str)
                for item in items
            )
        )

    peer_where = list(peer_step.where or [])
    if not all(isinstance(item, dict) for item in peer_where):
        return None
    if filter_signature(retained) != filter_signature(peer_where):
        return None

    return QueryToolCall(
        model_id=step.model_id,
        measures=[measure],
        dimensions=[dimension],
        where=retained,
        having=[],
        sort=[{"name": measure, "direction": "desc"}],
        dimension_refs=normalize_dimensions([dimension]),
        where_refs=list(step.where_refs or []),
    )


def validate_tool_call_against_bundle(
    call: ToolCall,
    bundle: Any,
    user_message: str | None = None,
) -> list[PlanValidationIssue]:
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
        # Bug-9738 — a benchmark whose peer figure was never computed is a
        # wrong number, not a wrong field, so it is only worth reporting once
        # the referenced fields are known good. It is never repairable by a
        # planner retry: the containment repair above rewrites it to the
        # grouped breakdown, and anything it cannot rewrite is refused here.
        if not issues:
            containment = detect_benchmark_containment(call, bundle, user_message)
            if containment is not None:
                issues.append(PlanValidationIssue(
                    path="compound_query.expression",
                    reason=containment.reason,
                    field_name=containment.slice_dimensions[0],
                    model_id=str(
                        next(
                            (
                                step.model_id for step in call.steps
                                if step.name == containment.slice_step
                            ),
                            "",
                        )
                    ) or None,
                    repairable=False,
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
    # Bug-7347 -- validate allow-list at the pre-execution gate for
    # EvaluateKpi and PreviewNamedSet.
    if isinstance(call, (EvaluateKpiToolCall, PreviewNamedSetToolCall)):
        tool_name = (
            "evaluate_kpi" if isinstance(call, EvaluateKpiToolCall)
            else "preview_named_set"
        )
        issues_kpi: list[PlanValidationIssue] = []
        _idx, idx_issues = _model_index_for(
            call.model_id, indexes, allowed, tool_name,
        )
        issues_kpi.extend(idx_issues)
        return issues_kpi
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


_BENCHMARK_REFUSAL_MESSAGES = {
    # Bug-9738 — name the mismatch and offer the comparable form. The number
    # is what gets remembered, so a caveat under a wrong ratio is not enough.
    BENCHMARK_TOTAL_OVER_UNGROUPED_MEAN: (
        "I could not answer that as a ratio. It would divide the total for one "
        "{dimension} by an average of individual rows, which are different "
        "kinds of figure, so the result would not mean anything. Ask for the "
        "total by {dimension} and I will show where it sits against the others."
    ),
    BENCHMARK_PEER_GRAIN_NEVER_REDUCED: (
        "I could not answer that as a ratio. The comparison figure would be "
        "each {dimension}'s own total rather than the average across all of "
        "them, so every row would be compared with itself. Ask for the total "
        "by {dimension} and I will show where it sits against the others."
    ),
}


def invalid_plan_message(issues: list[PlanValidationIssue]) -> str:
    first = issues[0] if issues else None
    if first and first.reason in _BENCHMARK_REFUSAL_MESSAGES:
        return _BENCHMARK_REFUSAL_MESSAGES[first.reason].format(
            dimension=first.field_name or "that category",
        )
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
    if not isinstance(call, QueryToolCall):
        return False
    preserved_previous_dimensions = _preserve_previous_breakdown_dimensions(
        call,
        intent,
        bundle,
    )
    preserved_previous_where = _preserve_previous_breakdown_where(call, intent, bundle)
    preserved_previous = preserved_previous_dimensions or preserved_previous_where
    if not intent.wants_trend:
        return preserved_previous
    repaired_temporal = _prefer_semantic_temporal_parts(call, bundle)
    repaired_temporal = (
        _repair_temporal_sort_to_selected_parts(call, bundle) or repaired_temporal
    )
    if repaired_temporal:
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
    return repaired or preserved_previous


_TEMPORAL_DIMENSION_NAME_RE = re.compile(
    r"(?:^|[_\s-])(date|time|timestamp|day|week|month|quarter|qtr|year|period|hour)(?:$|[_\s-])",
    re.IGNORECASE,
)
_TEMPORAL_DIMENSION_KINDS = frozenset({"time", "temporal", "date"})


def _is_temporal_previous_dimension(
    dimension: str,
    metadata: Mapping[str, Any],
) -> bool:
    """Classify a prior dimension using metadata before its display name.

    An explicit ``is_time_dim`` value is authoritative. Name heuristics are a
    fallback only when the profile has no metadata entry for the dimension.
    """
    if dimension in metadata:
        details = metadata[dimension]
        if not isinstance(details, Mapping):
            return False
        if "is_time_dim" in details:
            return details["is_time_dim"] is True
        return details.get("kind") in _TEMPORAL_DIMENSION_KINDS
    return bool(_TEMPORAL_DIMENSION_NAME_RE.search(dimension))


def _preserve_previous_breakdown_dimensions(
    call: QueryToolCall,
    intent: AnalyticalIntent,
    bundle: Any | None,
) -> bool:
    """Keep the prior categorical grain for a temporal breakdown follow-up.

    Bug-9739: the planner sometimes interpreted "break that down by year"
    as replacing the prior categorical dimension and sometimes as adding the
    year grain. The referent ("that") means the existing breakdown remains the
    subject. Preserve its still-visible categorical dimensions deterministically
    while allowing the new temporal grain to replace an older temporal grain.
    """
    if not intent.preserve_previous_breakdown_dimensions or bundle is None:
        return False
    previous_plan = getattr(bundle, "previous_plan", None)
    if not isinstance(previous_plan, dict):
        return False
    previous_query = previous_plan.get("query")
    if not isinstance(previous_query, dict):
        return False
    if str(previous_query.get("model_id")) != str(call.model_id):
        return False

    profile = next(
        (
            item
            for item in (getattr(bundle, "model_profiles", None) or [])
            if str(getattr(item, "id", "")) == str(call.model_id)
        ),
        None,
    )
    if profile is None:
        return False
    visible_dimensions = set(getattr(profile, "dimension_names", None) or [])
    metadata = getattr(profile, "dimensions", None) or {}
    previous_dimensions = previous_query.get("dimensions") or []
    if not isinstance(previous_dimensions, list):
        return False

    current_dimensions = set(call.dimensions)
    preserved: list[str] = []
    for dimension in previous_dimensions:
        if not isinstance(dimension, str):
            continue
        is_temporal = _is_temporal_previous_dimension(dimension, metadata)
        if (
            dimension in visible_dimensions
            and dimension not in current_dimensions
            and not is_temporal
        ):
            preserved.append(dimension)

    if not preserved:
        return False
    existing_refs = call.dimension_refs or normalize_dimensions(call.dimensions)
    call.dimensions = [*preserved, *call.dimensions]
    call.dimension_refs = [*normalize_dimensions(preserved), *existing_refs]
    return True


def _preserve_previous_breakdown_where(
    call: QueryToolCall,
    intent: AnalyticalIntent,
    bundle: Any | None,
) -> bool:
    """Keep prior row filters for a pure temporal breakdown follow-up.

    Bug-9968: the planner correctly retained the prior grouping subject but
    could emit an empty ``where`` clause for ``"Break that down by year"``.
    Copy only a validated, same-model prior query, and only when the current
    plan has no filter of its own. The copied JSON is detached from the stored
    plan; structured predicates are restored to their typed companion list so
    the normal validation and SQL rendering paths remain authoritative.
    """
    if not intent.preserve_previous_breakdown_where or bundle is None:
        return False
    if call.where or call.where_refs:
        return False

    previous_plan = getattr(bundle, "previous_plan", None)
    if not isinstance(previous_plan, dict):
        return False
    previous_query = previous_plan.get("query")
    if not isinstance(previous_query, dict):
        return False
    if str(previous_query.get("model_id")) != str(call.model_id):
        return False

    previous_where = previous_query.get("where") or []
    if not isinstance(previous_where, list) or not previous_where:
        return False

    flat_where: list[dict[str, Any]] = []
    structured_where = []
    for item in previous_where:
        if not isinstance(item, dict):
            return False
        copied = deepcopy(item)
        if is_structured_predicate(copied):
            try:
                structured_where.append(normalize_filter(copied, clause="where"))
            except ExpressionError:
                # The previous plan is expected to have passed validation. If
                # it did not, refuse to manufacture a potentially looser
                # current query from an untrusted structured predicate.
                return False
        else:
            flat_where.append(copied)

    if not flat_where and not structured_where:
        return False
    call.where = flat_where
    call.where_refs = structured_where
    return True


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
