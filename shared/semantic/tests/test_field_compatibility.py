from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from shared.semantic.field_compatibility import (
    AGGREGATE_GRAIN_MISMATCH,
    AMBIGUOUS_JOIN_PATH,
    HIDDEN_FIELD_UNAVAILABLE,
    MEASURE_DEPENDENCY_UNREACHABLE,
    NO_JOIN_PATH,
    PERSONA_FIELD_UNAVAILABLE,
    UNKNOWN_FIELD,
    FieldAccessPolicy,
    evaluate_field_compatibility,
)


@dataclass
class _Table:
    id: UUID
    name: str
    table_type: str = "dim_aggregate"


@dataclass
class _Column:
    id: UUID
    model_table_id: UUID
    column_name: str
    is_hidden: bool = False


@dataclass
class _Join:
    id: UUID
    left_table_id: UUID
    right_table_id: UUID
    join_type: str = "many_to_one"


@dataclass
class _Field:
    id: UUID
    name: str
    display_name: str
    source_column_id: UUID | None = None
    user_defined_attribute_id: UUID | None = None
    measure_type: str = "standard"
    expression: str | None = None
    is_additive: bool = True
    semi_additive_behavior: str | None = None
    variant_kind: str | None = None
    calc_agg_mode: str | None = None
    default_agg: str = "sum"


@dataclass
class _Aggregate:
    id: UUID
    grain: list[str]
    status: str = "active"


@dataclass
class _AggregateColumn:
    aggregate_definition_id: UUID
    measure_id: UUID


def _base_model():
    fact = _Table(uuid4(), "fact", "fact")
    school = _Table(uuid4(), "school")
    teacher = _Table(uuid4(), "teacher")
    fact_measure_col = _Column(uuid4(), fact.id, "age")
    school_col = _Column(uuid4(), school.id, "school_name")
    teacher_col = _Column(uuid4(), teacher.id, "teacher_name")
    measure = _Field(uuid4(), "average_student_age", "Average Student Age", fact_measure_col.id)
    school_dim = _Field(uuid4(), "school", "School", school_col.id)
    teacher_dim = _Field(uuid4(), "teacher_name", "Teacher Name", teacher_col.id)
    return {
        "tables": [fact, school, teacher],
        "columns": [fact_measure_col, school_col, teacher_col],
        "joins": [_Join(uuid4(), fact.id, school.id)],
        "measures": [measure],
        "dimensions": [school_dim, teacher_dim],
        "measure": measure,
        "school_dim": school_dim,
        "teacher_dim": teacher_dim,
        "fact": fact,
        "school": school,
        "teacher": teacher,
    }


def _evaluate(fixture, **kwargs):
    selected_dimension_ids = kwargs.pop(
        "selected_dimension_ids",
        [fixture["school_dim"].id, fixture["teacher_dim"].id],
    )
    return evaluate_field_compatibility(
        model_id=uuid4(),
        measures=fixture["measures"],
        dimensions=fixture["dimensions"],
        tables=fixture["tables"],
        columns=fixture["columns"],
        joins=fixture["joins"],
        selected_measure_ids=[fixture["measure"].id],
        selected_dimension_ids=selected_dimension_ids,
        **kwargs,
    )


def test_direct_path_compatible_and_missing_path_incompatible():
    fixture = _base_model()

    result = _evaluate(fixture)
    entry = result.measures[fixture["measure"].id]

    assert fixture["school_dim"].id in entry.compatible_dimension_ids
    issue = entry.incompatible_dimensions[fixture["teacher_dim"].id]
    assert issue.code == NO_JOIN_PATH
    assert "aggregation path" in issue.message
    assert "join path" not in issue.message.lower()
    assert issue.compatible_dimension_names == ["School"]


def test_ambiguous_path_returns_stable_reason_code():
    fixture = _base_model()
    bridge = _Table(uuid4(), "bridge")
    fixture["tables"].append(bridge)
    fixture["joins"].extend(
        [
            _Join(uuid4(), fixture["fact"].id, bridge.id),
            _Join(uuid4(), bridge.id, fixture["school"].id),
        ]
    )

    result = _evaluate(fixture, selected_dimension_ids=[fixture["school_dim"].id])
    issue = result.measures[fixture["measure"].id].incompatible_dimensions[
        fixture["school_dim"].id
    ]

    assert result.status == "compatible"
    assert issue.code == AMBIGUOUS_JOIN_PATH
    assert issue.severity == "warning"
    assert "aggregation path" in issue.message
    assert "join path" not in issue.message.lower()


def test_calculated_measure_unresolved_dependency_is_not_guessed():
    fixture = _base_model()
    calc = _Field(
        uuid4(),
        "age_ratio",
        "Age Ratio",
        measure_type="calculated",
        expression='measure("missing_measure") / measure("average_student_age")',
    )
    fixture["measures"] = [fixture["measure"], calc]
    fixture["measure"] = calc

    result = _evaluate(fixture)
    issue = result.measures[calc.id].incompatible_dimensions[fixture["school_dim"].id]

    assert issue.code == MEASURE_DEPENDENCY_UNREACHABLE


def test_calculated_measure_can_reference_same_base_measure_more_than_once():
    fixture = _base_model()
    calc = _Field(
        uuid4(),
        "age_double",
        "Age Double",
        measure_type="calculated",
        expression='measure("average_student_age") + measure("average_student_age")',
    )
    fixture["measures"] = [fixture["measure"], calc]
    fixture["measure"] = calc

    result = _evaluate(fixture)

    assert fixture["school_dim"].id in result.measures[calc.id].compatible_dimension_ids


def test_unknown_selected_measure_reports_unknown_field_issue():
    fixture = _base_model()
    missing_measure_id = uuid4()

    result = evaluate_field_compatibility(
        model_id=uuid4(),
        measures=fixture["measures"],
        dimensions=fixture["dimensions"],
        tables=fixture["tables"],
        columns=fixture["columns"],
        joins=fixture["joins"],
        selected_measure_ids=[missing_measure_id],
        selected_dimension_ids=[fixture["school_dim"].id],
    )

    issue = result.measures[missing_measure_id].incompatible_dimensions[
        fixture["school_dim"].id
    ]
    assert issue.code == UNKNOWN_FIELD


def test_persona_filtered_dimension_is_not_named_in_message_or_suggestions():
    fixture = _base_model()
    result = _evaluate(
        fixture,
        policy=FieldAccessPolicy(
            allowed_dimension_ids={fixture["school_dim"].id},
            persona_scoped=True,
        ),
    )

    issue = result.measures[fixture["measure"].id].incompatible_dimensions[
        fixture["teacher_dim"].id
    ]

    assert issue.code == PERSONA_FIELD_UNAVAILABLE
    assert "Teacher Name" not in issue.message
    assert issue.compatible_dimension_names == ["School"]


def test_hidden_dimension_is_removed_unless_hidden_fields_are_included():
    fixture = _base_model()
    hidden_column = next(
        c for c in fixture["columns"] if c.id == fixture["teacher_dim"].source_column_id
    )
    hidden_column.is_hidden = True
    fixture["joins"].append(_Join(uuid4(), fixture["fact"].id, fixture["teacher"].id))

    hidden_result = _evaluate(fixture)
    hidden_issue = hidden_result.measures[fixture["measure"].id].incompatible_dimensions[
        fixture["teacher_dim"].id
    ]
    assert hidden_issue.code == HIDDEN_FIELD_UNAVAILABLE
    assert "Teacher Name" not in hidden_issue.message
    assert hidden_issue.compatible_dimension_names == ["School"]

    included_result = _evaluate(
        fixture,
        policy=FieldAccessPolicy(include_hidden=True),
    )
    assert (
        fixture["teacher_dim"].id
        in included_result.measures[fixture["measure"].id].compatible_dimension_ids
    )


def test_multi_measure_summary_does_not_name_hidden_dimensions():
    fixture = _base_model()
    hidden_column = next(
        c for c in fixture["columns"] if c.id == fixture["teacher_dim"].source_column_id
    )
    hidden_column.is_hidden = True
    fixture["joins"].append(_Join(uuid4(), fixture["fact"].id, fixture["teacher"].id))
    second_measure = _Field(
        uuid4(),
        "student_count",
        "Student Count",
        fixture["measure"].source_column_id,
    )
    fixture["measures"].append(second_measure)

    result = evaluate_field_compatibility(
        model_id=uuid4(),
        measures=fixture["measures"],
        dimensions=fixture["dimensions"],
        tables=fixture["tables"],
        columns=fixture["columns"],
        joins=fixture["joins"],
        selected_measure_ids=[fixture["measure"].id, second_measure.id],
        selected_dimension_ids=[fixture["teacher_dim"].id],
    )

    assert result.multi_measure is not None
    assert result.multi_measure.conflicts_by_measure == []


def test_exact_grain_measure_rejects_dimensions_outside_active_aggregate_grain():
    fixture = _base_model()
    fixture["measure"].is_additive = False
    aggregate = _Aggregate(uuid4(), grain=["school"])
    result = _evaluate(
        fixture,
        aggregate_definitions=[aggregate],
        aggregate_columns=[
            _AggregateColumn(
                aggregate_definition_id=aggregate.id,
                measure_id=fixture["measure"].id,
            )
        ],
    )

    issue = result.measures[fixture["measure"].id].incompatible_dimensions[
        fixture["teacher_dim"].id
    ]
    assert issue.code == AGGREGATE_GRAIN_MISMATCH
    assert "aggregation path" in issue.message
    assert "aggregate grain" not in issue.message.lower()


def test_additive_measure_with_aggregate_does_not_emit_grain_mismatch():
    fixture = _base_model()
    aggregate = _Aggregate(uuid4(), grain=["school"])
    result = _evaluate(
        fixture,
        aggregate_definitions=[aggregate],
        aggregate_columns=[
            _AggregateColumn(
                aggregate_definition_id=aggregate.id,
                measure_id=fixture["measure"].id,
            )
        ],
    )

    issue = result.measures[fixture["measure"].id].incompatible_dimensions[
        fixture["teacher_dim"].id
    ]
    assert issue.code == NO_JOIN_PATH
