"""Field compatibility evaluation for semantic measures and dimensions.

This module is deliberately pure: callers provide already-loaded model rows
and the effective field visibility policy, and receive a serialisable
compatibility contract for UI, Excel, and validation callers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from shared.semantic.join_keyword import edge_cardinality
from shared.semantic.calculated_expression import (
    ExpressionValidationError,
    parse_expression,
)


NO_JOIN_PATH = "NO_JOIN_PATH"
AMBIGUOUS_JOIN_PATH = "AMBIGUOUS_JOIN_PATH"
MANY_TO_MANY_UNSUPPORTED = "MANY_TO_MANY_UNSUPPORTED"
AGGREGATE_GRAIN_MISMATCH = "AGGREGATE_GRAIN_MISMATCH"
MEASURE_DEPENDENCY_UNREACHABLE = "MEASURE_DEPENDENCY_UNREACHABLE"
PERSONA_FIELD_UNAVAILABLE = "PERSONA_FIELD_UNAVAILABLE"
HIDDEN_FIELD_UNAVAILABLE = "HIDDEN_FIELD_UNAVAILABLE"
UNKNOWN_FIELD = "UNKNOWN_FIELD"

REASON_CODES: frozenset[str] = frozenset(
    {
        NO_JOIN_PATH,
        AMBIGUOUS_JOIN_PATH,
        MANY_TO_MANY_UNSUPPORTED,
        AGGREGATE_GRAIN_MISMATCH,
        MEASURE_DEPENDENCY_UNREACHABLE,
        PERSONA_FIELD_UNAVAILABLE,
        HIDDEN_FIELD_UNAVAILABLE,
        UNKNOWN_FIELD,
    }
)


@dataclass(frozen=True)
class FieldAccessPolicy:
    """Effective field visibility for one compatibility evaluation."""

    allowed_measure_ids: set[UUID] | None = None
    allowed_dimension_ids: set[UUID] | None = None
    restricted_column_ids: set[UUID] = field(default_factory=set)
    include_hidden: bool = False
    persona_scoped: bool = False


@dataclass(frozen=True)
class CompatibilityIssue:
    code: str
    severity: str
    message: str
    measure_id: UUID
    dimension_id: UUID
    compatible_dimension_ids: list[UUID]
    compatible_dimension_names: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "measure_id": str(self.measure_id),
            "dimension_id": str(self.dimension_id),
            "compatible_dimension_ids": [
                str(dim_id) for dim_id in self.compatible_dimension_ids
            ],
            "compatible_dimension_names": self.compatible_dimension_names,
        }


@dataclass(frozen=True)
class MeasureCompatibility:
    measure_id: UUID
    name: str | None
    compatible_dimension_ids: list[UUID]
    incompatible_dimensions: dict[UUID, CompatibilityIssue]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "compatible_dimension_ids": [
                str(dim_id) for dim_id in self.compatible_dimension_ids
            ],
            "incompatible_dimensions": {
                str(dim_id): issue.to_dict()
                for dim_id, issue in self.incompatible_dimensions.items()
            },
        }


@dataclass(frozen=True)
class MultiMeasureCompatibility:
    selected_measure_ids: list[UUID]
    common_dimension_ids: list[UUID]
    common_dimension_names: list[str]
    conflicts_by_measure: list[dict[str, Any]]
    suggested_actions: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_measure_ids": [
                str(measure_id) for measure_id in self.selected_measure_ids
            ],
            "common_dimension_ids": [
                str(dim_id) for dim_id in self.common_dimension_ids
            ],
            "common_dimension_names": self.common_dimension_names,
            "conflicts_by_measure": self.conflicts_by_measure,
            "suggested_actions": self.suggested_actions,
        }


@dataclass(frozen=True)
class FieldCompatibilityResult:
    model_id: UUID
    version_id: UUID | None
    generated_at: datetime
    status: str
    measures: dict[UUID, MeasureCompatibility]
    multi_measure: MultiMeasureCompatibility | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": str(self.model_id),
            "version_id": str(self.version_id) if self.version_id else None,
            "generated_at": self.generated_at.isoformat(),
            "status": self.status,
            "measures": {
                str(measure_id): measure.to_dict()
                for measure_id, measure in self.measures.items()
            },
            "multi_measure": (
                self.multi_measure.to_dict() if self.multi_measure else None
            ),
        }


@dataclass(frozen=True)
class _ResolvedMeasure:
    table_ids: frozenset[UUID]
    code: str | None = None


def evaluate_field_compatibility(
    *,
    model_id: UUID,
    version_id: UUID | None = None,
    measures: list[Any],
    dimensions: list[Any],
    tables: list[Any],
    columns: list[Any],
    joins: list[Any],
    user_defined_attributes: list[Any] | None = None,
    aggregate_definitions: list[Any] | None = None,
    aggregate_columns: list[Any] | None = None,
    policy: FieldAccessPolicy | None = None,
    selected_measure_ids: list[UUID] | None = None,
    selected_dimension_ids: list[UUID] | None = None,
) -> FieldCompatibilityResult:
    """Evaluate measure/dimension compatibility for a model snapshot."""

    policy = policy or FieldAccessPolicy()
    measure_by_id = {_uuid(m.id): m for m in measures}
    measures_by_name = {str(m.name): m for m in measures if getattr(m, "name", None)}
    dimension_by_id = {_uuid(d.id): d for d in dimensions}
    table_by_id = {_uuid(t.id): t for t in tables}
    column_by_id = {_uuid(c.id): c for c in columns}
    uda_by_id = {
        _uuid(uda.id): uda for uda in (user_defined_attributes or [])
    }

    visible_measure_ids = [
        mid
        for mid, measure in measure_by_id.items()
        if _measure_visible(measure, column_by_id, uda_by_id, policy)
    ]
    visible_dimension_ids = [
        dim_id
        for dim_id, dim in dimension_by_id.items()
        if _dimension_visible(dim, column_by_id, uda_by_id, policy)
    ]

    requested_measure_ids = selected_measure_ids or visible_measure_ids
    requested_dimension_ids = selected_dimension_ids or visible_dimension_ids
    suggestion_dimension_ids = visible_dimension_ids

    adjacency = _build_adjacency(joins)
    aggregate_grain_by_measure = _aggregate_grain_by_measure(
        aggregate_definitions or [], aggregate_columns or []
    )

    measure_entries: dict[UUID, MeasureCompatibility] = {}
    any_blocking = False
    compatible_by_measure: dict[UUID, set[UUID]] = {}

    for measure_id in requested_measure_ids:
        measure = measure_by_id.get(measure_id)
        if measure is None:
            incompatible = {
                dimension_id: CompatibilityIssue(
                    code=UNKNOWN_FIELD,
                    severity=_severity_for_code(UNKNOWN_FIELD),
                    message=_message_for_issue(
                        code=UNKNOWN_FIELD,
                        measure=None,
                        dimension=None,
                        compatible_dimensions=[],
                        persona_scoped=policy.persona_scoped,
                    ),
                    measure_id=measure_id,
                    dimension_id=dimension_id,
                    compatible_dimension_ids=[],
                    compatible_dimension_names=[],
                )
                for dimension_id in requested_dimension_ids
            }
            measure_entries[measure_id] = MeasureCompatibility(
                measure_id=measure_id,
                name=None,
                compatible_dimension_ids=[],
                incompatible_dimensions=incompatible,
            )
            any_blocking = True
            continue

        measure_access_code = _measure_access_code(measure, column_by_id, uda_by_id, policy)
        measure_name = None if measure_access_code else _display_name(measure)
        resolved_measure = _resolve_measure_tables(
            measure,
            measure_by_id=measure_by_id,
            measures_by_name=measures_by_name,
            column_by_id=column_by_id,
            uda_by_id=uda_by_id,
        )
        compatible_all = _compatible_dimension_ids_for_measure(
            measure=measure,
            measure_access_code=measure_access_code,
            resolved_measure=resolved_measure,
            dimension_ids=suggestion_dimension_ids,
            dimension_by_id=dimension_by_id,
            column_by_id=column_by_id,
            uda_by_id=uda_by_id,
            table_by_id=table_by_id,
            adjacency=adjacency,
            joins=joins,
            policy=policy,
            aggregate_grain_by_measure=aggregate_grain_by_measure,
        )
        compatible_by_measure[measure_id] = set(compatible_all)

        incompatible: dict[UUID, CompatibilityIssue] = {}
        for dimension_id in requested_dimension_ids:
            dim = dimension_by_id.get(dimension_id)
            if dim is None:
                code = UNKNOWN_FIELD
            else:
                code = _compatibility_code(
                    measure=measure,
                    measure_access_code=measure_access_code,
                    resolved_measure=resolved_measure,
                    dimension=dim,
                    column_by_id=column_by_id,
                    uda_by_id=uda_by_id,
                    table_by_id=table_by_id,
                    adjacency=adjacency,
                    joins=joins,
                    policy=policy,
                    aggregate_grain_by_measure=aggregate_grain_by_measure,
                )
            if code is not None:
                severity = _severity_for_code(code)
                if severity == "error":
                    any_blocking = True
                incompatible[dimension_id] = CompatibilityIssue(
                    code=code,
                    severity=severity,
                    message=_message_for_issue(
                        code=code,
                        measure=measure if measure_access_code is None else None,
                        dimension=dim if dim is not None and _can_name_dimension(dim, column_by_id, uda_by_id, policy) else None,
                        compatible_dimensions=[
                            dimension_by_id[dim_id]
                            for dim_id in compatible_all
                            if dim_id in dimension_by_id
                        ],
                        persona_scoped=policy.persona_scoped,
                    ),
                    measure_id=measure_id,
                    dimension_id=dimension_id,
                    compatible_dimension_ids=compatible_all,
                    compatible_dimension_names=[
                        _display_name(dimension_by_id[dim_id])
                        for dim_id in compatible_all[:8]
                        if dim_id in dimension_by_id
                    ],
                )

        measure_entries[measure_id] = MeasureCompatibility(
            measure_id=measure_id,
            name=measure_name,
            compatible_dimension_ids=compatible_all,
            incompatible_dimensions=incompatible,
        )

    multi_measure = None
    if selected_measure_ids and len(selected_measure_ids) > 1:
        multi_measure = _build_multi_measure(
            selected_measure_ids=selected_measure_ids,
            selected_dimension_ids=requested_dimension_ids,
            measure_entries=measure_entries,
            measure_by_id=measure_by_id,
            dimension_by_id=dimension_by_id,
            compatible_by_measure=compatible_by_measure,
        )

    return FieldCompatibilityResult(
        model_id=model_id,
        version_id=version_id,
        generated_at=datetime.now(timezone.utc),
        status="incompatible" if any_blocking else "compatible",
        measures=measure_entries,
        multi_measure=multi_measure,
    )


def _build_multi_measure(
    *,
    selected_measure_ids: list[UUID],
    selected_dimension_ids: list[UUID],
    measure_entries: dict[UUID, MeasureCompatibility],
    measure_by_id: dict[UUID, Any],
    dimension_by_id: dict[UUID, Any],
    compatible_by_measure: dict[UUID, set[UUID]],
) -> MultiMeasureCompatibility:
    selected_sets = [
        compatible_by_measure.get(measure_id, set())
        for measure_id in selected_measure_ids
    ]
    common_ids = set.intersection(*selected_sets) if selected_sets else set()
    ordered_common = [
        dim_id for dim_id in selected_dimension_ids if dim_id in common_ids
    ] + [
        dim_id
        for dim_id in sorted(common_ids, key=lambda did: _display_name(dimension_by_id[did]))
        if dim_id not in selected_dimension_ids
    ]

    conflicts: list[dict[str, Any]] = []
    for measure_id in selected_measure_ids:
        entry = measure_entries.get(measure_id)
        measure = measure_by_id.get(measure_id)
        if entry is None or measure is None:
            continue
        incompatible_names = [
            _display_name(dimension_by_id[dim_id])
            for dim_id in selected_dimension_ids
            if (
                dim_id in entry.incompatible_dimensions
                and dim_id in dimension_by_id
                and entry.incompatible_dimensions[dim_id].code
                not in {PERSONA_FIELD_UNAVAILABLE, HIDDEN_FIELD_UNAVAILABLE}
            )
        ]
        if incompatible_names:
            conflicts.append(
                {
                    "measure_id": str(measure_id),
                    "measure_name": entry.name or "Unavailable measure",
                    "incompatible_dimension_names": incompatible_names,
                    "compatible_dimension_names": [
                        _display_name(dimension_by_id[dim_id])
                        for dim_id in entry.compatible_dimension_ids[:8]
                        if dim_id in dimension_by_id
                    ],
                }
            )

    return MultiMeasureCompatibility(
        selected_measure_ids=selected_measure_ids,
        common_dimension_ids=ordered_common,
        common_dimension_names=[
            _display_name(dimension_by_id[dim_id])
            for dim_id in ordered_common[:8]
            if dim_id in dimension_by_id
        ],
        conflicts_by_measure=conflicts,
        suggested_actions=[
            "keep_common_dimensions",
            "split_pivot",
            "remove_incompatible_dimensions",
        ],
    )


def _compatible_dimension_ids_for_measure(
    *,
    measure: Any,
    measure_access_code: str | None,
    resolved_measure: _ResolvedMeasure,
    dimension_ids: list[UUID],
    dimension_by_id: dict[UUID, Any],
    column_by_id: dict[UUID, Any],
    uda_by_id: dict[UUID, Any],
    table_by_id: dict[UUID, Any],
    adjacency: dict[UUID, list[tuple[UUID, Any]]],
    joins: list[Any],
    policy: FieldAccessPolicy,
    aggregate_grain_by_measure: dict[UUID, list[set[str]]],
) -> list[UUID]:
    out: list[UUID] = []
    for dimension_id in dimension_ids:
        dim = dimension_by_id.get(dimension_id)
        if dim is None:
            continue
        code = _compatibility_code(
            measure=measure,
            measure_access_code=measure_access_code,
            resolved_measure=resolved_measure,
            dimension=dim,
            column_by_id=column_by_id,
            uda_by_id=uda_by_id,
            table_by_id=table_by_id,
            adjacency=adjacency,
            joins=joins,
            policy=policy,
            aggregate_grain_by_measure=aggregate_grain_by_measure,
        )
        if code is None:
            out.append(dimension_id)
    return out


def _compatibility_code(
    *,
    measure: Any,
    measure_access_code: str | None,
    resolved_measure: _ResolvedMeasure,
    dimension: Any,
    column_by_id: dict[UUID, Any],
    uda_by_id: dict[UUID, Any],
    table_by_id: dict[UUID, Any],
    adjacency: dict[UUID, list[tuple[UUID, Any]]],
    joins: list[Any],
    policy: FieldAccessPolicy,
    aggregate_grain_by_measure: dict[UUID, list[set[str]]],
) -> str | None:
    if measure_access_code is not None:
        return measure_access_code
    dim_access_code = _dimension_access_code(dimension, column_by_id, uda_by_id, policy)
    if dim_access_code is not None:
        return dim_access_code
    if resolved_measure.code is not None:
        return resolved_measure.code
    dim_table_id = _field_table_id(dimension, column_by_id, uda_by_id)
    if dim_table_id is None or dim_table_id not in table_by_id:
        return UNKNOWN_FIELD
    if _aggregate_grain_mismatch(measure, dimension, aggregate_grain_by_measure):
        return AGGREGATE_GRAIN_MISMATCH

    saw_many_to_many = False
    usable_paths = 0
    for measure_table_id in resolved_measure.table_ids:
        if measure_table_id not in table_by_id:
            return MEASURE_DEPENDENCY_UNREACHABLE
        paths = _enumerate_paths(measure_table_id, dim_table_id, adjacency)
        if not paths:
            return NO_JOIN_PATH
        # Many-to-many is a CARDINALITY, and cardinality lives in its own
        # field (join-orientation contract, invariant 3). This used to read
        # ``join_type``, which only ever held the token while the two
        # properties shared one column: a many-to-many declared the way the
        # Joins panel, ``JoinCreate`` and the YAML importer now write it
        # (``join_type="left"`` + ``cardinality="many_to_many"``) sailed past
        # the guard and the pair was offered as compatible, so querying it
        # fanned out and double-counted with no warning.
        # ``edge_cardinality`` reads the declared field and falls back to a
        # legacy token still parked in ``join_type``, so pre-split rows keep
        # being caught exactly as before.
        safe_paths = [
            path
            for path in paths
            if not any(edge_cardinality(edge) == "many_to_many" for edge in path)
        ]
        if not safe_paths:
            saw_many_to_many = True
            continue
        usable_paths += len(safe_paths)

    if saw_many_to_many and usable_paths == 0:
        return MANY_TO_MANY_UNSUPPORTED
    if usable_paths > len(resolved_measure.table_ids):
        return AMBIGUOUS_JOIN_PATH
    return None


def _resolve_measure_tables(
    measure: Any,
    *,
    measure_by_id: dict[UUID, Any],
    measures_by_name: dict[str, Any],
    column_by_id: dict[UUID, Any],
    uda_by_id: dict[UUID, Any],
    seen: set[UUID] | None = None,
) -> _ResolvedMeasure:
    measure_id = _uuid(measure.id)
    seen = set(seen or set())
    if measure_id in seen:
        return _ResolvedMeasure(frozenset(), MEASURE_DEPENDENCY_UNREACHABLE)
    seen.add(measure_id)

    if getattr(measure, "measure_type", "standard") == "calculated":
        expression = getattr(measure, "expression", None)
        if not expression:
            return _ResolvedMeasure(frozenset(), MEASURE_DEPENDENCY_UNREACHABLE)
        try:
            parsed = parse_expression(expression)
        except ExpressionValidationError:
            return _ResolvedMeasure(frozenset(), MEASURE_DEPENDENCY_UNREACHABLE)
        table_ids: set[UUID] = set()
        for ref_name in parsed.referenced_names:
            ref = measures_by_name.get(ref_name)
            if ref is None:
                return _ResolvedMeasure(frozenset(), MEASURE_DEPENDENCY_UNREACHABLE)
            resolved = _resolve_measure_tables(
                ref,
                measure_by_id=measure_by_id,
                measures_by_name=measures_by_name,
                column_by_id=column_by_id,
                uda_by_id=uda_by_id,
                seen=set(seen),
            )
            if resolved.code is not None:
                return resolved
            table_ids.update(resolved.table_ids)
        if not table_ids:
            return _ResolvedMeasure(frozenset(), MEASURE_DEPENDENCY_UNREACHABLE)
        return _ResolvedMeasure(frozenset(table_ids))

    table_id = _field_table_id(measure, column_by_id, uda_by_id)
    if table_id is None:
        return _ResolvedMeasure(frozenset(), MEASURE_DEPENDENCY_UNREACHABLE)
    return _ResolvedMeasure(frozenset({table_id}))


def _build_adjacency(joins: list[Any]) -> dict[UUID, list[tuple[UUID, Any]]]:
    adjacency: dict[UUID, list[tuple[UUID, Any]]] = {}
    for join in joins:
        left_id = _uuid(join.left_table_id)
        right_id = _uuid(join.right_table_id)
        adjacency.setdefault(left_id, []).append((right_id, join))
        adjacency.setdefault(right_id, []).append((left_id, join))
    return adjacency


def _enumerate_paths(
    start_table_id: UUID,
    end_table_id: UUID,
    adjacency: dict[UUID, list[tuple[UUID, Any]]],
) -> list[list[Any]]:
    if start_table_id == end_table_id:
        return [[]]

    paths: list[list[Any]] = []

    def _dfs(node: UUID, visited: set[UUID], trail: list[Any]) -> None:
        if len(paths) > 1:
            return
        for next_node, join in adjacency.get(node, []):
            if next_node in visited:
                continue
            next_trail = [*trail, join]
            if next_node == end_table_id:
                paths.append(next_trail)
                if len(paths) > 1:
                    return
                continue
            visited.add(next_node)
            _dfs(next_node, visited, next_trail)
            visited.remove(next_node)

    _dfs(start_table_id, {start_table_id}, [])
    return paths


def _aggregate_grain_by_measure(
    aggregate_definitions: list[Any],
    aggregate_columns: list[Any],
) -> dict[UUID, list[set[str]]]:
    active_grain_by_agg: dict[UUID, set[str]] = {}
    for aggregate in aggregate_definitions:
        if getattr(aggregate, "status", "active") not in {"active", "ready"}:
            continue
        active_grain_by_agg[_uuid(aggregate.id)] = {
            str(grain) for grain in (getattr(aggregate, "grain", None) or [])
        }
    out: dict[UUID, list[set[str]]] = {}
    for column in aggregate_columns:
        measure_id = getattr(column, "measure_id", None)
        if measure_id is None:
            continue
        grain = active_grain_by_agg.get(_uuid(column.aggregate_definition_id))
        if grain is not None:
            out.setdefault(_uuid(measure_id), []).append(grain)
    return out


def _aggregate_grain_mismatch(
    measure: Any,
    dimension: Any,
    aggregate_grain_by_measure: dict[UUID, list[set[str]]],
) -> bool:
    if not _measure_requires_exact_aggregate_grain(measure):
        return False
    grains = aggregate_grain_by_measure.get(_uuid(measure.id), [])
    if not grains:
        return False
    names = {str(getattr(dimension, "name", "")), str(_uuid(dimension.id))}
    return not any(names & grain for grain in grains)


def _measure_requires_exact_aggregate_grain(measure: Any) -> bool:
    if not getattr(measure, "is_additive", True):
        return True
    if getattr(measure, "semi_additive_behavior", None):
        return True
    if getattr(measure, "variant_kind", None) is not None:
        return True
    if (
        getattr(measure, "measure_type", None) == "calculated"
        and (getattr(measure, "calc_agg_mode", None) or "expression_as_written")
        == "expression_as_written"
    ):
        return True
    return str(getattr(measure, "default_agg", "") or "").lower() in {
        "count_distinct",
    }


def _measure_visible(
    measure: Any,
    column_by_id: dict[UUID, Any],
    uda_by_id: dict[UUID, Any],
    policy: FieldAccessPolicy,
) -> bool:
    return _measure_access_code(measure, column_by_id, uda_by_id, policy) is None


def _dimension_visible(
    dimension: Any,
    column_by_id: dict[UUID, Any],
    uda_by_id: dict[UUID, Any],
    policy: FieldAccessPolicy,
) -> bool:
    return _dimension_access_code(dimension, column_by_id, uda_by_id, policy) is None


def _measure_access_code(
    measure: Any,
    column_by_id: dict[UUID, Any],
    uda_by_id: dict[UUID, Any],
    policy: FieldAccessPolicy,
) -> str | None:
    measure_id = _uuid(measure.id)
    if policy.allowed_measure_ids is not None and measure_id not in policy.allowed_measure_ids:
        return PERSONA_FIELD_UNAVAILABLE
    column_id = _field_column_id(measure)
    if column_id is not None and column_id in policy.restricted_column_ids:
        return PERSONA_FIELD_UNAVAILABLE
    return _hidden_access_code(measure, column_by_id, uda_by_id, policy)


def _dimension_access_code(
    dimension: Any,
    column_by_id: dict[UUID, Any],
    uda_by_id: dict[UUID, Any],
    policy: FieldAccessPolicy,
) -> str | None:
    dimension_id = _uuid(dimension.id)
    if policy.allowed_dimension_ids is not None and dimension_id not in policy.allowed_dimension_ids:
        return PERSONA_FIELD_UNAVAILABLE
    column_id = _field_column_id(dimension)
    if column_id is not None and column_id in policy.restricted_column_ids:
        return PERSONA_FIELD_UNAVAILABLE
    return _hidden_access_code(dimension, column_by_id, uda_by_id, policy)


def _hidden_access_code(
    field_obj: Any,
    column_by_id: dict[UUID, Any],
    uda_by_id: dict[UUID, Any],
    policy: FieldAccessPolicy,
) -> str | None:
    if policy.include_hidden:
        return None
    column_id = _field_column_id(field_obj)
    if column_id is not None:
        column = column_by_id.get(column_id)
        if column is not None and bool(getattr(column, "is_hidden", False)):
            return HIDDEN_FIELD_UNAVAILABLE
    return None


def _can_name_dimension(
    dimension: Any,
    column_by_id: dict[UUID, Any],
    uda_by_id: dict[UUID, Any],
    policy: FieldAccessPolicy,
) -> bool:
    return _dimension_access_code(dimension, column_by_id, uda_by_id, policy) is None


def _field_table_id(
    field_obj: Any,
    column_by_id: dict[UUID, Any],
    uda_by_id: dict[UUID, Any],
) -> UUID | None:
    column_id = _field_column_id(field_obj)
    if column_id is not None:
        column = column_by_id.get(column_id)
        if column is not None and getattr(column, "model_table_id", None) is not None:
            return _uuid(column.model_table_id)
    uda_id = getattr(field_obj, "user_defined_attribute_id", None)
    if uda_id is not None:
        uda = uda_by_id.get(_uuid(uda_id))
        if uda is not None and getattr(uda, "table_id", None) is not None:
            return _uuid(uda.table_id)
    return None


def _field_column_id(field_obj: Any) -> UUID | None:
    column_id = getattr(field_obj, "source_column_id", None)
    return _uuid(column_id) if column_id is not None else None


def _message_for_issue(
    *,
    code: str,
    measure: Any | None,
    dimension: Any | None,
    compatible_dimensions: list[Any],
    persona_scoped: bool,
) -> str:
    measure_name = _display_name(measure) if measure is not None else "This measure"
    dimension_name = (
        _display_name(dimension) if dimension is not None else "this dimension"
    )
    suggestions = _suggestion_text(measure_name, compatible_dimensions, persona_scoped)
    if code == NO_JOIN_PATH:
        return (
            f"There is no aggregation path between {measure_name} and "
            f"{dimension_name}. {suggestions}"
        )
    if code == AMBIGUOUS_JOIN_PATH:
        return (
            f"{measure_name} and {dimension_name} have more than one possible "
            f"aggregation path. Ask a model owner to mark the intended path. "
            f"{suggestions}"
        )
    if code == MANY_TO_MANY_UNSUPPORTED:
        return (
            f"{measure_name} and {dimension_name} need an unsupported "
            f"many-to-many aggregation path. {suggestions}"
        )
    if code == AGGREGATE_GRAIN_MISMATCH:
        return (
            f"There is no aggregation path between {measure_name} and "
            f"{dimension_name} at the level of detail available for this "
            f"measure. {suggestions}"
        )
    if code == MEASURE_DEPENDENCY_UNREACHABLE:
        return (
            f"{measure_name} depends on fields that are not reachable in this "
            f"model. {suggestions}"
        )
    if code == PERSONA_FIELD_UNAVAILABLE:
        return (
            "Some fields are unavailable to your current persona. "
            f"{suggestions}"
        )
    if code == HIDDEN_FIELD_UNAVAILABLE:
        return f"This field is hidden in the current field view. {suggestions}"
    return "One or more selected fields cannot be found in this model."


def _suggestion_text(
    measure_name: str,
    compatible_dimensions: list[Any],
    persona_scoped: bool,
) -> str:
    if not compatible_dimensions:
        return (
            f"{measure_name} has no compatible dimensions in this model. "
            "Use it by itself or update the model relationships."
        )
    names = [_display_name(dim) for dim in compatible_dimensions[:8]]
    suffix = ""
    if len(compatible_dimensions) > 8:
        suffix = f", and {len(compatible_dimensions) - 8} more"
    scope = (
        "the dimensions available to your current persona"
        if persona_scoped
        else ""
    )
    if scope:
        return f"{measure_name} can be used with {scope}: {', '.join(names)}{suffix}."
    return f"{measure_name} can be used with: {', '.join(names)}{suffix}."


def _display_name(obj: Any) -> str:
    return str(getattr(obj, "display_name", None) or getattr(obj, "name", None) or obj.id)


def _severity_for_code(code: str) -> str:
    return "warning" if code == AMBIGUOUS_JOIN_PATH else "error"


def _uuid(value: Any) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))
