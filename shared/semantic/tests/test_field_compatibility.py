from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from shared.semantic.field_compatibility import (
    AGGREGATE_GRAIN_MISMATCH,
    AMBIGUOUS_JOIN_PATH,
    HIDDEN_FIELD_UNAVAILABLE,
    MANY_TO_MANY_UNSUPPORTED,
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
    # Join orientation and cardinality are SEPARATE fields (join-orientation
    # contract, invariant 3). Without this the fixture could not express the
    # shape the Joins panel, ``JoinCreate`` and the YAML importer now write,
    # which is exactly why the many-to-many guard's regression went unseen.
    cardinality: str | None = None


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


def test_bug_9458_same_table_avg_pair_is_source_compatible_without_grain():
    """Q13.10: source semantics stay valid when optional aggregates omit the dim."""
    fixture = _base_model()
    risk_col = _Column(uuid4(), fixture["fact"].id, "risk_score")
    decision_col = _Column(uuid4(), fixture["fact"].id, "risk_decision")
    risk = _Field(
        uuid4(), "risk_score", "Risk Score", risk_col.id,
        is_additive=False, default_agg="avg",
    )
    decision = _Field(uuid4(), "risk_decision", "Risk Decision", decision_col.id)
    fixture["columns"].extend([risk_col, decision_col])
    fixture["measures"] = [risk]
    fixture["dimensions"] = [decision]
    unrelated = _Aggregate(uuid4(), grain=["school"])
    result = evaluate_field_compatibility(
        model_id=uuid4(), measures=[risk], dimensions=[decision],
        tables=fixture["tables"], columns=fixture["columns"], joins=[],
        selected_measure_ids=[risk.id], selected_dimension_ids=[decision.id],
        aggregate_definitions=[unrelated],
        aggregate_columns=[_AggregateColumn(unrelated.id, risk.id)],
    )
    entry = result.measures[risk.id]
    assert decision.id in entry.compatible_dimension_ids
    assert decision.id not in entry.incompatible_dimensions


def test_bug_9458_count_distinct_same_table_still_requires_exact_grain():
    fixture = _base_model()
    value_col = _Column(uuid4(), fixture["fact"].id, "customer_id")
    group_col = _Column(uuid4(), fixture["fact"].id, "risk_decision")
    measure = _Field(
        uuid4(), "customers", "Customers", value_col.id,
        is_additive=False, default_agg="count_distinct",
    )
    dimension = _Field(uuid4(), "risk_decision", "Risk Decision", group_col.id)
    fixture["columns"].extend([value_col, group_col])
    aggregate = _Aggregate(uuid4(), grain=["school"])
    result = evaluate_field_compatibility(
        model_id=uuid4(), measures=[measure], dimensions=[dimension],
        tables=fixture["tables"], columns=fixture["columns"], joins=[],
        selected_measure_ids=[measure.id], selected_dimension_ids=[dimension.id],
        aggregate_definitions=[aggregate],
        aggregate_columns=[_AggregateColumn(aggregate.id, measure.id)],
    )
    assert result.measures[measure.id].incompatible_dimensions[dimension.id].code == AGGREGATE_GRAIN_MISMATCH


@pytest.mark.parametrize(
    ("default_agg", "semi_additive_behavior", "variant_kind"),
    [
        ("sum", "last_non_empty", None),
        ("sum", None, "trailing_n"),
        ("p90", None, None),
    ],
)
def test_bug_l3_r2_b01_nonordinary_same_table_pair_still_requires_exact_grain(
    default_agg, semi_additive_behavior, variant_kind,
):
    """L3-R2-B01: Q13.10 must not widen governed measure families."""
    table = _Table(uuid4(), "payment_transaction", "fact")
    score_column = _Column(uuid4(), table.id, "risk_score")
    decision_column = _Column(uuid4(), table.id, "risk_decision")
    measure = _Field(
        uuid4(), "risk_score", "Risk Score", score_column.id,
        default_agg=default_agg, is_additive=False,
        semi_additive_behavior=semi_additive_behavior,
        variant_kind=variant_kind,
    )
    dimension = _Field(
        uuid4(), "risk_decision", "Risk Decision", decision_column.id,
    )
    aggregate = _Aggregate(uuid4(), grain=["unrelated_dimension"])
    result = evaluate_field_compatibility(
        model_id=uuid4(), measures=[measure], dimensions=[dimension],
        tables=[table], columns=[score_column, decision_column], joins=[],
        selected_measure_ids=[measure.id], selected_dimension_ids=[dimension.id],
        aggregate_definitions=[aggregate],
        aggregate_columns=[_AggregateColumn(aggregate.id, measure.id)],
    )
    assert result.measures[measure.id].incompatible_dimensions[dimension.id].code == AGGREGATE_GRAIN_MISMATCH


def _bug_9458_risk_pair():
    table = _Table(uuid4(), "payment_transaction", "fact")
    score_column = _Column(uuid4(), table.id, "risk_score")
    decision_column = _Column(uuid4(), table.id, "risk_decision")
    score = _Field(
        uuid4(), "risk_score", "Risk Score", score_column.id,
        is_additive=False, default_agg="avg",
    )
    decision = _Field(
        uuid4(), "risk_decision", "Risk Decision", decision_column.id,
    )
    return table, score_column, decision_column, score, decision


def test_bug_9458_same_table_avg_pair_without_any_aggregate_uses_source():
    """No acceleration artifact is required for valid source semantics."""
    table, score_column, decision_column, score, decision = _bug_9458_risk_pair()
    result = evaluate_field_compatibility(
        model_id=uuid4(), measures=[score], dimensions=[decision],
        tables=[table], columns=[score_column, decision_column], joins=[],
        selected_measure_ids=[score.id], selected_dimension_ids=[decision.id],
    )
    assert decision.id in result.measures[score.id].compatible_dimension_ids


@pytest.mark.parametrize("status", ["retired", "stale"])
def test_bug_9458_retired_or_stale_optional_aggregate_cannot_block_source(status):
    """Lifecycle state of optional acceleration never changes legality."""
    table, score_column, decision_column, score, decision = _bug_9458_risk_pair()
    aggregate = _Aggregate(uuid4(), grain=["unrelated_dimension"], status=status)
    result = evaluate_field_compatibility(
        model_id=uuid4(), measures=[score], dimensions=[decision],
        tables=[table], columns=[score_column, decision_column], joins=[],
        selected_measure_ids=[score.id], selected_dimension_ids=[decision.id],
        aggregate_definitions=[aggregate],
        aggregate_columns=[_AggregateColumn(aggregate.id, score.id)],
    )
    assert decision.id in result.measures[score.id].compatible_dimension_ids


# ---------------------------------------------------------------------------
# Bug-8653 — the many-to-many guard must read the CARDINALITY field
# ---------------------------------------------------------------------------


def test_many_to_many_declared_in_cardinality_is_still_unsupported():
    """The orientation/cardinality split moved the many-to-many declaration
    out of ``join_type``.

    The Joins panel selector, ``JoinCreate`` and the YAML importer (via
    ``split_join_token``) all now write ``join_type="left"/"inner"`` plus
    ``cardinality="many_to_many"``. A guard still reading the raw
    ``join_type`` classifies that path as safe and offers the pair as
    compatible; querying it fans out and double-counts with no warning.
    """
    for join_type in ("left", "inner"):
        fixture = _base_model()
        join = fixture["joins"][0]
        join.join_type = join_type
        join.cardinality = "many_to_many"
        result = _evaluate(
            fixture, selected_dimension_ids=[fixture["school_dim"].id]
        )
        entry = result.measures[fixture["measure"].id]
        assert fixture["school_dim"].id not in entry.compatible_dimension_ids, (
            f"join_type={join_type!r} + cardinality='many_to_many' was offered "
            "as compatible; the fan-out guard is reading the wrong field"
        )
        assert entry.incompatible_dimensions[fixture["school_dim"].id].code == (
            MANY_TO_MANY_UNSUPPORTED
        )


def test_many_to_many_in_a_legacy_join_type_token_is_still_caught():
    """Invariant 4: a row written before the split keeps being caught."""
    fixture = _base_model()
    fixture["joins"][0].join_type = "many_to_many"
    result = _evaluate(fixture, selected_dimension_ids=[fixture["school_dim"].id])
    entry = result.measures[fixture["measure"].id]
    assert entry.incompatible_dimensions[fixture["school_dim"].id].code == (
        MANY_TO_MANY_UNSUPPORTED
    )


def test_a_non_fanning_declared_cardinality_stays_compatible():
    """Guard the guard: the check must not reject every declared cardinality,
    only many-to-many. Without this the two tests above would still pass if
    the guard were changed to refuse any declared fan-out at all."""
    fixture = _base_model()
    fixture["joins"][0].join_type = "left"
    fixture["joins"][0].cardinality = "many_to_one"
    result = _evaluate(fixture, selected_dimension_ids=[fixture["school_dim"].id])
    entry = result.measures[fixture["measure"].id]
    assert fixture["school_dim"].id in entry.compatible_dimension_ids
