from __future__ import annotations

import json
import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from src.pipeline import (
    run_turn,
    _raw_record_refusal_outcome,
    _repair_evaluate_kpi_presentation_call,
    _repair_preview_named_set_ranking_call,
)
from src.planning.enums import AnalyticalShape
from src.planning.intent import AnalyticalIntent
from src.planning.measure_metadata import MeasureRoleMetadata
from src.planning.validation import (
    apply_pre_validation_repairs,
    build_model_field_indexes,
    validate_tool_call_against_bundle,
    validation_feedback_for_correction,
)
from src.tools.spec import (
    ClarifyToolCall,
    CompoundQueryToolCall,
    CompoundStep,
    EvaluateKpiToolCall,
    PreviewNamedSetToolCall,
    QueryToolCall,
)
from src.tools.expressions import normalize_dimensions


MODEL_ID = uuid.uuid4()


def _profile(
    *,
    model_id: uuid.UUID = MODEL_ID,
    measures: list[str] | None = None,
    dimensions: list[str] | None = None,
    filterable_where: list[str] | None = None,
    sortable: list[str] | None = None,
    measure_metadata: dict[str, MeasureRoleMetadata] | None = None,
    kpis: list[types.SimpleNamespace] | None = None,
):
    return types.SimpleNamespace(
        id=model_id,
        measure_names=measures or ["revenue", "revenue_ytd"],
        dimension_names=dimensions or ["country", "business_date"],
        filterable_where_names=filterable_where or ["country", "business_date"],
        sortable_names=sortable or ["country", "business_date", "revenue", "revenue_ytd"],
        measure_metadata=measure_metadata or {
            "revenue": MeasureRoleMetadata(name="revenue"),
            "revenue_ytd": MeasureRoleMetadata(
                name="revenue_ytd",
                variant_kind="ytd",
                variant_of_measure="revenue",
                resolved_calendar_id="cal-1",
                resolved_date_col_id="date-col-1",
            ),
        },
        kpis=kpis or [],
    )


def _bundle(profile=None, allow_ids=None):
    profile = profile or _profile()
    return types.SimpleNamespace(
        system="system",
        user="user",
        narration_system="narration",
        allow_list_model_ids=allow_ids or [profile.id],
        model_profiles=[profile],
        persona_scopes=None,
        prior_questions=[],
    )


def _query(**overrides):
    data = {
        "model_id": str(MODEL_ID),
        "measures": ["revenue"],
        "dimensions": ["country"],
        "where": [],
        "having": [],
        "sort": [],
        "limit": 100,
    }
    data.update(overrides)
    return QueryToolCall(**data)


def test_evaluate_kpi_presentation_prompt_repairs_to_shaped_query():
    kpi_id = uuid.uuid4()
    profile = _profile(
        measures=["Revenue"],
        filterable_where=[],
        sortable=["Revenue"],
        measure_metadata={"Revenue": MeasureRoleMetadata(name="Revenue")},
        kpis=[
            types.SimpleNamespace(
                id=kpi_id,
                name="Revenue",
                display_name=None,
            )
        ],
    )
    call = EvaluateKpiToolCall(model_id=str(profile.id), kpi_id=str(kpi_id))

    repaired = _repair_evaluate_kpi_presentation_call(
        call,
        _bundle(profile),
        "Show overall total revenue as a KPI",
    )

    assert isinstance(repaired, QueryToolCall)
    assert repaired.measures == ["Revenue"]
    assert repaired.dimensions == []
    assert repaired.chart_type == "kpi"


def test_explicit_evaluate_kpi_intent_is_not_repaired():
    kpi_id = uuid.uuid4()
    profile = _profile(
        measures=["Revenue"],
        kpis=[
            types.SimpleNamespace(
                id=kpi_id,
                name="Revenue",
                display_name=None,
            )
        ],
    )
    call = EvaluateKpiToolCall(model_id=str(profile.id), kpi_id=str(kpi_id))

    repaired = _repair_evaluate_kpi_presentation_call(
        call,
        _bundle(profile),
        "Evaluate the Revenue KPI status against its target",
    )

    assert repaired is None


def test_preview_named_set_ranking_prompt_repairs_to_ranked_query():
    named_set_id = uuid.uuid4()
    profile = _profile(
        measures=["Revenue"],
        dimensions=["country_code"],
        sortable=["Revenue", "country_code"],
        measure_metadata={"Revenue": MeasureRoleMetadata(name="Revenue")},
    )
    call = PreviewNamedSetToolCall(
        model_id=str(profile.id),
        named_set_id=str(named_set_id),
    )

    repaired = _repair_preview_named_set_ranking_call(
        call,
        _bundle(profile),
        "Which five countries have the highest revenue?",
    )

    assert isinstance(repaired, QueryToolCall)
    assert repaired.measures == ["Revenue"]
    assert repaired.dimensions == ["country_code"]
    assert repaired.sort == [{"name": "Revenue", "direction": "desc"}]
    assert repaired.limit == 5
    assert repaired.limit_explicit is True
    assert repaired.chart_type == "h_bar"


def test_genuine_preview_named_set_intent_is_not_repaired():
    profile = _profile(measures=["Revenue"], dimensions=["country_code"])
    call = PreviewNamedSetToolCall(
        model_id=str(profile.id),
        named_set_id=str(uuid.uuid4()),
    )

    repaired = _repair_preview_named_set_ranking_call(
        call,
        _bundle(profile),
        "Preview the EMEA country named set",
    )

    assert repaired is None


def test_raw_record_clarify_plan_is_refused_before_tool_branch():
    intent = AnalyticalIntent(
        shape_hint=AnalyticalShape.DETAIL_TABLE,
        wants_detail_rows=True,
        notes=["raw_records_requested"],
    )

    outcome = _raw_record_refusal_outcome(
        ClarifyToolCall("Which transactions do you mean?"),
        intent,
    )

    assert outcome is not None
    assert outcome.status == "refused"
    assert outcome.answer_text == "raw_records_not_supported_by_aggregate_tools"
    assert outcome.guardrail_actions[0]["reason"] == "raw_records_not_supported_by_aggregate_tools"
    assert outcome.guardrail_actions[0]["message_i18n_key"] == "turn.rawRecordsNotSupported"


def test_valid_grained_dimension_survives_validation():
    call = QueryToolCall(
        model_id=str(MODEL_ID),
        measures=["revenue"],
        dimensions=["business_date_month"],
        dimension_refs=None,
        where=[],
        having=[],
        sort=[{"name": "business_date_month", "direction": "asc"}],
        limit=100,
    )
    call.dimension_refs = normalize_dimensions([{"name": "business_date", "grain": "month"}])

    assert validate_tool_call_against_bundle(call, _bundle()) == []


def test_structured_where_expression_with_invented_field_is_caught():
    # Bug-5349 Phase 2/3 (Codex-R2) — an invented field hidden inside a
    # structured WHERE predicate must be flagged by the pre-execution validator,
    # not slip through to the binder. (revenue/country exist; "ghost" does not.)
    from src.tools.spec import parse_tool_call
    import json as _json
    call = parse_tool_call(_json.dumps({"query": {
        "model_id": str(MODEL_ID), "measures": ["revenue"], "dimensions": ["country"],
        "where": [{"left": {"fn": "lower", "args": [{"field": "ghost"}]},
                   "op": "eq", "right": {"literal": "x"}}],
        "having": [], "sort": [],
    }}))
    issues = validate_tool_call_against_bundle(call, _bundle())
    assert any(i.field_name == "ghost" for i in issues)


def test_structured_projection_with_valid_fields_passes_validation():
    from src.tools.spec import parse_tool_call
    import json as _json
    call = parse_tool_call(_json.dumps({"query": {
        "model_id": str(MODEL_ID), "measures": [], "dimensions": ["country"],
        "projections": [{"expr": {"fn": "round",
                         "args": [{"field": "revenue"}, {"literal": 2}]}, "alias": "r"}],
        "where": [], "having": [], "sort": [],
    }}))
    assert validate_tool_call_against_bundle(call, _bundle()) == []


def test_structured_having_with_invented_measure_is_caught():
    from src.tools.spec import parse_tool_call
    import json as _json
    call = parse_tool_call(_json.dumps({"query": {
        "model_id": str(MODEL_ID), "measures": ["revenue"], "dimensions": ["country"],
        "having": [{"left": {"fn": "sum", "args": [{"field": "phantom"}]},
                    "op": "gt", "right": {"literal": 1}}],
        "where": [], "sort": [],
    }}))
    issues = validate_tool_call_against_bundle(call, _bundle())
    assert any(i.field_name == "phantom" for i in issues)


def test_compound_step_structured_expression_invented_field_is_caught():
    # Bug-5349 Phase 2/3 (Codex-R2 / Round-3) — an invented field inside a
    # COMPOUND-step structured predicate must be flagged by the pre-execution
    # bundle validator (the compound reconstruction threads the structured refs
    # so _validate_query_like walks their base fields).
    from src.tools.spec import parse_tool_call
    import json as _json
    call = parse_tool_call(_json.dumps({"compound_query": {
        "steps": [
            {"name": "a", "model_id": str(MODEL_ID), "measures": ["revenue"],
             "dimensions": ["country"],
             "where": [{"left": {"fn": "lower", "args": [{"field": "ghost_dim"}]},
                        "op": "eq", "right": {"literal": "x"}}],
             "having": [], "sort": []},
            {"name": "b", "model_id": str(MODEL_ID), "measures": ["revenue"],
             "dimensions": ["country"], "where": [], "having": [], "sort": []},
        ],
        "expression": {"ref": {"step": "a", "measure": "revenue"}},
        "result_label": "x",
    }}))
    issues = validate_tool_call_against_bundle(call, _bundle())
    assert any(i.field_name == "ghost_dim" for i in issues)


def test_generated_month_alias_repairs_before_field_validation():
    call = QueryToolCall(
        model_id=str(MODEL_ID),
        measures=["revenue"],
        dimensions=["business_date_month"],
        dimension_refs=None,
        where=[],
        having=[],
        sort=[{"name": "business_date_month", "direction": "asc"}],
        limit=100,
    )

    repaired = apply_pre_validation_repairs(
        call,
        AnalyticalIntent(
            shape_hint=AnalyticalShape.TIME_SERIES,
            wants_trend=True,
            requested_grain="month",
        ),
        _bundle(),
    )

    assert repaired is True
    assert call.dimensions == ["business_date_month"]
    assert call.dimension_refs
    assert call.dimension_refs[0].is_bare is False
    assert call.dimension_refs[0].base_fields == ("business_date",)
    assert call.sort == [{"name": "business_date_month", "direction": "asc"}]
    assert validate_tool_call_against_bundle(call, _bundle()) == []


def test_grained_month_ref_prefers_available_semantic_year_month_parts():
    profile = _profile(
        dimensions=["business_date", "business_date_year", "business_date_month"],
        filterable_where=["business_date"],
        sortable=["business_date", "business_date_year", "business_date_month", "revenue"],
    )
    call = QueryToolCall(
        model_id=str(MODEL_ID),
        measures=["revenue"],
        dimensions=["business_date_month"],
        dimension_refs=normalize_dimensions([{"name": "business_date", "grain": "month"}]),
        where=[],
        having=[],
        sort=[{"name": "business_date_month", "direction": "asc"}],
        limit=100,
    )

    repaired = apply_pre_validation_repairs(
        call,
        AnalyticalIntent(
            shape_hint=AnalyticalShape.TIME_SERIES,
            wants_trend=True,
            requested_grain="month",
        ),
        _bundle(profile),
    )

    assert repaired is True
    assert call.dimensions == ["business_date_year", "business_date_month"]
    assert [ref.is_bare for ref in call.dimension_refs or []] == [True, True]
    assert call.sort == [
        {"name": "business_date_year", "direction": "asc"},
        {"name": "business_date_month", "direction": "asc"},
    ]
    assert validate_tool_call_against_bundle(call, _bundle(profile)) == []


def test_temporal_sort_on_base_date_repairs_to_selected_year_month_parts():
    profile = _profile(
        dimensions=["business_date", "business_date_year", "business_date_month"],
        filterable_where=["business_date"],
        sortable=["business_date", "business_date_year", "business_date_month", "revenue"],
    )
    call = QueryToolCall(
        model_id=str(MODEL_ID),
        measures=["revenue"],
        dimensions=["business_date_year", "business_date_month"],
        where=[],
        having=[],
        sort=[{"name": "business_date", "direction": "asc"}],
        limit=100,
    )

    repaired = apply_pre_validation_repairs(
        call,
        AnalyticalIntent(
            shape_hint=AnalyticalShape.TIME_SERIES,
            wants_trend=True,
            requested_grain="month",
        ),
        _bundle(profile),
    )

    assert repaired is True
    assert call.sort == [
        {"name": "business_date_year", "direction": "asc"},
        {"name": "business_date_month", "direction": "asc"},
    ]
    assert validate_tool_call_against_bundle(call, _bundle(profile)) == []


def test_unknown_dimension_is_rejected_before_execution():
    call = _query(dimensions=["business_month"])

    issues = validate_tool_call_against_bundle(call, _bundle())

    assert len(issues) == 1
    assert issues[0].reason == "unknown_dimension"
    assert issues[0].field_name == "business_month"
    assert issues[0].repairable is True


def test_persona_filtered_profile_rejects_unavailable_field():
    profile = _profile(dimensions=["country"], filterable_where=["country"], sortable=["country", "revenue"])
    call = _query(dimensions=["employee_name"])

    issues = validate_tool_call_against_bundle(call, _bundle(profile))

    assert issues[0].reason == "unknown_dimension"
    assert issues[0].field_name == "employee_name"


def test_having_requires_measure_and_where_requires_filterable_field():
    call = _query(
        where=[{"name": "revenue", "op": "gt", "value": 100}],
        having=[{"name": "country", "op": "eq", "value": "DE"}],
    )

    issues = validate_tool_call_against_bundle(call, _bundle())
    reasons = {issue.reason for issue in issues}

    assert "field_not_filterable_where" in reasons
    assert "having_requires_measure" in reasons


def test_compound_steps_use_same_runtime_field_validation():
    call = CompoundQueryToolCall(
        steps=[
            CompoundStep("a", str(MODEL_ID), ["revenue"], ["country"], [], [], [], 100),
            CompoundStep("b", str(MODEL_ID), ["missing"], ["business_month"], [], [], [], 100),
        ],
        expression={"op": "div", "args": [{"ref": {"step": "a", "measure": "revenue"}}, {"ref": {"step": "b", "measure": "missing"}}]},
        result_label="ratio",
    )

    issues = validate_tool_call_against_bundle(call, _bundle())
    fields = {issue.field_name for issue in issues}

    assert {"missing", "business_month"}.issubset(fields)


def test_time_variant_metadata_is_carried_in_model_index():
    indexes = build_model_field_indexes(_bundle())

    metadata = indexes[str(MODEL_ID)].measure_metadata["revenue_ytd"]

    assert metadata.variant_kind == "ytd"
    assert metadata.needs_calendar is True
    assert metadata.resolved_calendar_id == "cal-1"


def test_correction_feedback_lists_available_runtime_fields():
    call = _query(dimensions=["business_month"])
    bundle = _bundle()
    issues = validate_tool_call_against_bundle(call, bundle)

    feedback = validation_feedback_for_correction(issues, bundle)

    assert "business_month" in feedback
    assert "measures=revenue, revenue_ytd" in feedback
    assert "dimensions=business_date, country" in feedback


@pytest.mark.asyncio
async def test_run_turn_refuses_invalid_plan_before_execute_query():
    raw_plan = json.dumps({
        "query": {
            "model_id": str(MODEL_ID),
            "measures": ["revenue"],
            "dimensions": ["business_month"],
            "where": [],
            "having": [],
            "sort": [],
            "limit": 100,
        }
    })
    adapter = AsyncMock()
    adapter.complete = AsyncMock(return_value=raw_plan)
    adapter.last_usage = {"input_tokens": 10, "output_tokens": 5}
    cfg = types.SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        judge_mode="async",
        safety_policy="",
    )
    conversation = types.SimpleNamespace(
        id=uuid.uuid4(),
        persona_id=None,
        pinned_model_id=None,
    )

    with patch("src.pipeline.assemble_prompt", AsyncMock(return_value=_bundle())), \
         patch("src.pipeline.resolve_agent_llm_failover_configs", AsyncMock(return_value=[
             types.SimpleNamespace(provider="test", model_name="planner", timeout_seconds=30)
         ])), \
         patch("src.pipeline.build_adapter", return_value=adapter), \
         patch("src.pipeline.RetryingAdapter", side_effect=lambda a: a), \
         patch("src.pipeline.check_budget", AsyncMock(return_value=None)), \
         patch("src.pipeline._attempt_tool_call_correction", AsyncMock(return_value=None)), \
         patch("src.pipeline.execute_query", AsyncMock()) as execute_query:
        outcome = await run_turn(
            db=AsyncMock(),
            cfg=cfg,
            conversation=conversation,
            user_message="show trend by business month",
            jwt_token="token",
        )

    assert outcome.status == "refused"
    assert outcome.plan["validation"]["issues"][0]["field_name"] == "business_month"
    execute_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_turn_refuses_shape_invalid_trend_before_execute_query():
    raw_plan = json.dumps({
        "query": {
            "model_id": str(MODEL_ID),
            "measures": ["revenue"],
            "dimensions": ["country"],
            "where": [],
            "having": [],
            "sort": [],
            "limit": 100,
        }
    })
    adapter = AsyncMock()
    adapter.complete = AsyncMock(return_value=raw_plan)
    adapter.last_usage = {"input_tokens": 10, "output_tokens": 5}
    cfg = types.SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        judge_mode="async",
        safety_policy="",
    )
    conversation = types.SimpleNamespace(
        id=uuid.uuid4(),
        persona_id=None,
        pinned_model_id=None,
    )

    with patch("src.pipeline.assemble_prompt", AsyncMock(return_value=_bundle())), \
         patch("src.pipeline.resolve_agent_llm_failover_configs", AsyncMock(return_value=[
             types.SimpleNamespace(provider="test", model_name="planner", timeout_seconds=30)
         ])), \
         patch("src.pipeline.build_adapter", return_value=adapter), \
         patch("src.pipeline.RetryingAdapter", side_effect=lambda a: a), \
         patch("src.pipeline.check_budget", AsyncMock(return_value=None)), \
         patch("src.pipeline.execute_query", AsyncMock()) as execute_query:
        outcome = await run_turn(
            db=AsyncMock(),
            cfg=cfg,
            conversation=conversation,
            user_message="show the revenue trend",
            jwt_token="token",
        )

    assert outcome.status == "refused"
    issues = outcome.plan["shape_validation"]["validation"]["issues"]
    assert issues[0]["reason"] == "trend_requires_temporal_axis"
    execute_query.assert_not_awaited()


# ── Bug-5373: create_aggregate validation against metadata ────────────

class TestCreateAggregateValidation:
    """Bug-5373 — create_aggregate measures/dimensions must be validated
    against the model metadata before execution."""

    def test_invalid_measure_detected(self):
        from src.tools.spec import CreateAggregateToolCall
        profile = _profile(
            measures=["revenue", "costs"],
            dimensions=["country", "date"],
        )
        bundle = _bundle(profile=profile)
        call = CreateAggregateToolCall(
            model_id=str(MODEL_ID),
            measures=["invented_measure"],
            dimensions=["country"],
            description="test",
        )
        issues = validate_tool_call_against_bundle(call, bundle)
        assert len(issues) == 1
        assert issues[0].reason == "unknown_measure"
        assert issues[0].field_name == "invented_measure"

    def test_invalid_dimension_detected(self):
        from src.tools.spec import CreateAggregateToolCall
        profile = _profile(
            measures=["revenue"],
            dimensions=["country"],
        )
        bundle = _bundle(profile=profile)
        call = CreateAggregateToolCall(
            model_id=str(MODEL_ID),
            measures=["revenue"],
            dimensions=["invented_dim"],
            description="test",
        )
        issues = validate_tool_call_against_bundle(call, bundle)
        assert len(issues) == 1
        assert issues[0].reason == "unknown_dimension"
        assert issues[0].field_name == "invented_dim"

    def test_valid_aggregate_passes(self):
        from src.tools.spec import CreateAggregateToolCall
        profile = _profile(
            measures=["revenue"],
            dimensions=["country"],
        )
        bundle = _bundle(profile=profile)
        call = CreateAggregateToolCall(
            model_id=str(MODEL_ID),
            measures=["revenue"],
            dimensions=["country"],
            description="test",
        )
        issues = validate_tool_call_against_bundle(call, bundle)
        assert len(issues) == 0


# ── Bug-5284: compound turn-level complexity ─────────────────────────

class TestCompoundTurnComplexity:
    """Bug-5284 — compound query complexity checked per turn, not per step."""

    @pytest.mark.asyncio
    async def test_turn_complexity_blocks_compound(self):
        from src.pipeline import _run_compound_query_branch
        from src.tools.spec import CompoundStep, CompoundQueryToolCall
        model_id = str(MODEL_ID)
        call = CompoundQueryToolCall(
            steps=[
                CompoundStep("a", model_id, ["m1", "m2"], ["d1"], [], [], [], 100),
                CompoundStep("b", model_id, ["m1", "m2"], ["d1"], [], [], [], 100),
            ],
            expression={"op": "add", "args": [
                {"ref": {"step": "a", "measure": "m1"}},
                {"ref": {"step": "b", "measure": "m1"}},
            ]},
            result_label="total",
        )
        cfg = types.SimpleNamespace(
            max_compound_steps=5,
            max_query_complexity=4,  # Each step has 3, total is 6
        )
        from unittest.mock import AsyncMock
        bundle = types.SimpleNamespace(
            allow_list_model_ids=[MODEL_ID],
        )
        outcome = await _run_compound_query_branch(
            db=AsyncMock(),
            cfg=cfg,
            adapter=AsyncMock(),
            bundle=bundle,
            user_message="test",
            call=call,
            jwt_token="token",
        )
        assert outcome.status == "refused"
        assert "too complex" in outcome.answer_text.lower()
