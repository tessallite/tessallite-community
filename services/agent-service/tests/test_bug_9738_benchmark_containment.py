"""Bug-9738 — benchmark containment (stage 1).

A "how does X compare with the average" plan is wrong whenever the peer figure
it divides by was never computed. Two shapes reached the query router and both
published a plausible wrong ratio:

  (a) a category TOTAL over an UNGROUPED per-row MEAN — the reported
      "20,019x the average" (a category total over a per-transaction mean);
  (b) a single-slice SCALAR over a step GROUPED BY the dimension the slice
      pins — the reported tautology ``X / (X / 5) = 5``, true for any data.

Containment either rewrites the plan to the grouped breakdown the question
asks for, or refuses it, before any SQL is issued. Like-for-like comparisons
(total against total, mean against mean, share of total, row-aligned grouped
ratios) must keep working untouched.
"""
from __future__ import annotations

import json
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.pipeline import run_turn
from src.planning.measure_metadata import MeasureRoleMetadata
from src.planning.validation import (
    BENCHMARK_SHARE_REPLACES_REQUESTED_BREAKDOWN,
    BENCHMARK_PEER_GRAIN_NEVER_REDUCED,
    BENCHMARK_TOTAL_OVER_UNGROUPED_MEAN,
    detect_benchmark_containment,
    invalid_plan_message,
    repair_benchmark_to_grouped_breakdown,
    validate_tool_call_against_bundle,
)
from src.tools.spec import (
    CompoundQueryToolCall,
    CompoundStep,
    QueryToolCall,
)

MODEL_ID = uuid.uuid4()

MEASURES = ["base_amount", "avg_base_amount", "transaction_count"]
DIMENSIONS = [
    "account_type",
    "country_code",
    "business_date",
    "business_date_month",
]


def _profile(model_id: uuid.UUID = MODEL_ID):
    return types.SimpleNamespace(
        id=model_id,
        measure_names=list(MEASURES),
        dimension_names=list(DIMENSIONS),
        filterable_where_names=list(DIMENSIONS),
        sortable_names=[*DIMENSIONS, *MEASURES],
        measure_metadata={
            "base_amount": MeasureRoleMetadata(
                name="base_amount", default_agg="sum", is_additive=True,
            ),
            "avg_base_amount": MeasureRoleMetadata(
                name="avg_base_amount", default_agg="avg", is_additive=False,
            ),
            "transaction_count": MeasureRoleMetadata(
                name="transaction_count", default_agg="count", is_additive=True,
            ),
        },
        kpis=[],
        dimensions={},
    )


def _bundle(profile=None):
    profile = profile or _profile()
    return types.SimpleNamespace(
        system="system",
        user="user",
        narration_system="narration",
        allow_list_model_ids=[profile.id],
        model_profiles=[profile],
        persona_scopes=None,
        prior_questions=[],
        previous_plan=None,
    )


def _ref(step: str, measure: str) -> dict:
    return {"ref": {"step": step, "measure": measure}}


def _div(numerator: dict, denominator: dict) -> dict:
    return {"op": "div", "args": [numerator, denominator]}


def _step(
    name: str,
    measures: list[str],
    dimensions: list[str] | None = None,
    where: list[dict] | None = None,
) -> CompoundStep:
    return CompoundStep(
        name=name,
        model_id=str(MODEL_ID),
        measures=measures,
        dimensions=dimensions or [],
        where=where or [],
        having=[],
        sort=[],
        limit=100,
    )


_YEAR_WINDOW = {
    "name": "business_date",
    "op": "between",
    "value": ["2026-01-01", "2026-12-31"],
}
_CREDIT = {"name": "account_type", "op": "eq", "value": "CREDIT"}


def _total_over_ungrouped_mean_plan() -> CompoundQueryToolCall:
    """Failure (a), as reported live on modely."""
    return CompoundQueryToolCall(
        steps=[
            _step("credit", ["base_amount"], where=[_CREDIT, dict(_YEAR_WINDOW)]),
            _step("all_types", ["avg_base_amount"], where=[dict(_YEAR_WINDOW)]),
        ],
        expression=_div(
            _ref("credit", "base_amount"),
            _ref("all_types", "avg_base_amount"),
        ),
        result_label="CREDIT vs average",
    )


def _scalar_over_grouped_step_plan() -> CompoundQueryToolCall:
    """Failure (b), as reported live on modely: X / (X / 5) = 5."""
    return CompoundQueryToolCall(
        steps=[
            _step("credit", ["base_amount"], where=[_CREDIT]),
            _step("avg_per_type", ["base_amount"], dimensions=["account_type"]),
        ],
        expression=_div(
            _ref("credit", "base_amount"),
            {"op": "div", "args": [
                _ref("avg_per_type", "base_amount"),
                {"const": 5},
            ]},
        ),
        result_label="CREDIT vs average per account type",
    )


_TOTALS_PLUS_BENCHMARK_QUESTION = (
    "What is the total base amount for each account type, and is CREDIT "
    "above or below the average?"
)
_GROUP_AVERAGE_QUESTION = (
    "How does CREDIT's total compare with the average across all account types?"
)


def _grouped_share_substitution_plan() -> CompoundQueryToolCall:
    """The Q7 live failure: grouped totals divided by an overall total."""
    return CompoundQueryToolCall(
        steps=[
            _step("by_account_type", ["base_amount"], dimensions=["account_type"]),
            _step("overall", ["base_amount"]),
        ],
        expression={
            "op": "round",
            "args": [
                _div(
                    _ref("by_account_type", "base_amount"),
                    _ref("overall", "base_amount"),
                ),
                {"const": 4},
            ],
        },
        result_label="Share of total base amount",
    )


def _grouped_difference_substitution_plan() -> CompoundQueryToolCall:
    """The captured Q7 Stage-1 failure: totals minus overall average."""
    return CompoundQueryToolCall(
        steps=[
            _step("by_account_type", ["base_amount"], dimensions=["account_type"]),
            _step("overall", ["base_amount"]),
        ],
        expression={
            "op": "sub",
            "args": [
                _ref("by_account_type", "base_amount"),
                {
                    "op": "div",
                    "args": [_ref("overall", "base_amount"), {"const": 5}],
                },
            ],
        },
        result_label="Base amount vs average account-type value",
    )


# --- detection --------------------------------------------------------------


def test_category_total_over_ungrouped_mean_never_executes():
    call = _total_over_ungrouped_mean_plan()

    issues = validate_tool_call_against_bundle(
        call, _bundle(), _GROUP_AVERAGE_QUESTION,
    )

    assert [issue.reason for issue in issues] == [
        BENCHMARK_TOTAL_OVER_UNGROUPED_MEAN
    ]
    assert issues[0].field_name == "account_type"
    assert issues[0].repairable is False
    assert issues[0].path == "compound_query.expression"


def test_scalar_broadcast_over_grouped_step_never_executes():
    call = _scalar_over_grouped_step_plan()

    issues = validate_tool_call_against_bundle(
        call, _bundle(), _GROUP_AVERAGE_QUESTION,
    )

    assert [issue.reason for issue in issues] == [
        BENCHMARK_PEER_GRAIN_NEVER_REDUCED
    ]
    assert issues[0].field_name == "account_type"
    assert issues[0].repairable is False


def test_refusal_names_the_mismatch_and_the_comparable_form():
    for plan in (_total_over_ungrouped_mean_plan(), _scalar_over_grouped_step_plan()):
        issues = validate_tool_call_against_bundle(
            plan, _bundle(), _GROUP_AVERAGE_QUESTION,
        )

        message = invalid_plan_message(issues)

        assert "account_type" in message
        assert "ratio" in message
        assert "total by account_type" in message


# --- shapes that must keep working -----------------------------------------


def test_share_of_total_is_untouched():
    """One slice over the same measure for the whole population: legitimate."""
    call = CompoundQueryToolCall(
        steps=[
            _step("credit", ["base_amount"], where=[_CREDIT, dict(_YEAR_WINDOW)]),
            _step("all_types", ["base_amount"], where=[dict(_YEAR_WINDOW)]),
        ],
        expression=_div(
            _ref("credit", "base_amount"),
            _ref("all_types", "base_amount"),
        ),
        result_label="CREDIT share",
    )

    assert detect_benchmark_containment(call, _bundle()) is None
    assert validate_tool_call_against_bundle(call, _bundle()) == []


def test_Bug_9973_totals_question_detects_share_substitution():
    call = _grouped_share_substitution_plan()
    bundle = _bundle()

    containment = detect_benchmark_containment(
        call, bundle, _TOTALS_PLUS_BENCHMARK_QUESTION,
    )

    assert containment is not None
    assert containment.reason == BENCHMARK_SHARE_REPLACES_REQUESTED_BREAKDOWN
    assert containment.slice_step == "by_account_type"
    assert containment.breakdown_dimension == "account_type"
    issues = validate_tool_call_against_bundle(
        call, bundle, _TOTALS_PLUS_BENCHMARK_QUESTION,
    )
    assert [issue.reason for issue in issues] == [
        BENCHMARK_SHARE_REPLACES_REQUESTED_BREAKDOWN,
    ]


def test_Bug_9973_repair_returns_absolute_breakdown_and_share_control_survives():
    call = _grouped_share_substitution_plan()
    bundle = _bundle()
    containment = detect_benchmark_containment(
        call, bundle, _TOTALS_PLUS_BENCHMARK_QUESTION,
    )

    repaired = repair_benchmark_to_grouped_breakdown(call, containment, bundle)

    assert isinstance(repaired, QueryToolCall)
    assert repaired.measures == ["base_amount"]
    assert repaired.dimensions == ["account_type"]
    assert repaired.sort == [{"name": "base_amount", "direction": "desc"}]
    assert repaired.where == []
    assert validate_tool_call_against_bundle(repaired, bundle) == []
    assert detect_benchmark_containment(
        call, bundle, "What share of total base amount does each account type represent?",
    ) is None
    assert validate_tool_call_against_bundle(
        call, bundle, "What share of total base amount does each account type represent?",
    ) == []


def test_Bug_9973_captured_difference_substitution_is_contained_with_control():
    call = _grouped_difference_substitution_plan()
    bundle = _bundle()

    containment = detect_benchmark_containment(
        call, bundle, _TOTALS_PLUS_BENCHMARK_QUESTION,
    )

    assert containment is not None
    assert containment.reason == BENCHMARK_SHARE_REPLACES_REQUESTED_BREAKDOWN
    assert containment.breakdown_dimension == "account_type"
    repaired = repair_benchmark_to_grouped_breakdown(call, containment, bundle)
    assert isinstance(repaired, QueryToolCall)
    assert repaired.measures == ["base_amount"]
    assert repaired.dimensions == ["account_type"]
    assert repaired.sort == [{"name": "base_amount", "direction": "desc"}]
    assert [issue.reason for issue in validate_tool_call_against_bundle(
        call, bundle, _TOTALS_PLUS_BENCHMARK_QUESTION,
    )] == [BENCHMARK_SHARE_REPLACES_REQUESTED_BREAKDOWN]

    # A genuine difference question must retain the existing compound path.
    assert detect_benchmark_containment(
        call,
        bundle,
        "What is the difference between each account type and the average base amount?",
    ) is None


@pytest.mark.parametrize("divisor", [0, -1, float("inf")])
def test_Bug_9973_invalid_difference_divisor_is_not_repaired(divisor):
    call = _grouped_difference_substitution_plan()
    call.expression["args"][1]["args"][1]["const"] = divisor

    assert detect_benchmark_containment(
        call, _bundle(), _TOTALS_PLUS_BENCHMARK_QUESTION,
    ) is None


def test_row_aligned_grouped_breakdown_ratio_is_untouched():
    """Both steps grouped on the same axis: a row-aligned ratio, legitimate."""
    call = CompoundQueryToolCall(
        steps=[
            _step(
                "credit",
                ["base_amount"],
                dimensions=["business_date_month"],
                where=[_CREDIT],
            ),
            _step("all_types", ["base_amount"], dimensions=["business_date_month"]),
        ],
        expression=_div(
            _ref("credit", "base_amount"),
            _ref("all_types", "base_amount"),
        ),
        result_label="CREDIT share by month",
    )

    assert detect_benchmark_containment(call, _bundle()) is None
    assert validate_tool_call_against_bundle(call, _bundle()) == []


def test_total_over_the_mean_of_the_same_population_is_untouched():
    """"How many average transactions is this total?" — a real quantity."""
    call = CompoundQueryToolCall(
        steps=[
            _step("credit_total", ["base_amount"], where=[_CREDIT]),
            _step("credit_mean", ["avg_base_amount"], where=[dict(_CREDIT)]),
        ],
        expression=_div(
            _ref("credit_total", "base_amount"),
            _ref("credit_mean", "avg_base_amount"),
        ),
        result_label="Effective transaction count",
    )

    assert detect_benchmark_containment(call, _bundle()) is None
    assert validate_tool_call_against_bundle(call, _bundle()) == []


def test_explicit_fixed_baseline_over_grouped_totals_is_untouched():
    call = _scalar_over_grouped_step_plan()
    message = (
        "Use CREDIT as a fixed baseline. Show CREDIT's total divided by each "
        "account type's total."
    )

    assert detect_benchmark_containment(call, _bundle(), message) is None
    assert validate_tool_call_against_bundle(call, _bundle(), message) == []


def test_explicit_total_over_average_sized_transaction_is_untouched():
    call = _total_over_ungrouped_mean_plan()
    message = "How many average-sized transactions does CREDIT's total represent?"

    assert detect_benchmark_containment(call, _bundle(), message) is None
    assert validate_tool_call_against_bundle(call, _bundle(), message) == []


def test_explicit_total_compared_with_average_transaction_is_untouched():
    call = _total_over_ungrouped_mean_plan()
    message = "Is CREDIT's total above the average transaction amount?"

    assert detect_benchmark_containment(call, _bundle(), message) is None
    assert validate_tool_call_against_bundle(call, _bundle(), message) == []


def test_mean_against_mean_is_untouched():
    call = CompoundQueryToolCall(
        steps=[
            _step("credit", ["avg_base_amount"], where=[_CREDIT]),
            _step("all_types", ["avg_base_amount"]),
        ],
        expression=_div(
            _ref("credit", "avg_base_amount"),
            _ref("all_types", "avg_base_amount"),
        ),
        result_label="CREDIT mean vs overall mean",
    )

    assert detect_benchmark_containment(call, _bundle()) is None
    assert validate_tool_call_against_bundle(call, _bundle()) == []


# --- repair -----------------------------------------------------------------


def test_repair_returns_the_grouped_breakdown_and_keeps_other_filters():
    call = _total_over_ungrouped_mean_plan()
    bundle = _bundle()
    containment = detect_benchmark_containment(
        call, bundle, _GROUP_AVERAGE_QUESTION,
    )

    repaired = repair_benchmark_to_grouped_breakdown(call, containment, bundle)

    assert isinstance(repaired, QueryToolCall)
    assert repaired.measures == ["base_amount"]
    assert repaired.dimensions == ["account_type"]
    assert repaired.where == [dict(_YEAR_WINDOW)]
    assert repaired.sort == [{"name": "base_amount", "direction": "desc"}]
    assert validate_tool_call_against_bundle(repaired, bundle) == []


def test_repair_of_the_grouped_peer_shape_returns_the_same_breakdown():
    call = _scalar_over_grouped_step_plan()
    bundle = _bundle()
    containment = detect_benchmark_containment(
        call, bundle, _GROUP_AVERAGE_QUESTION,
    )

    repaired = repair_benchmark_to_grouped_breakdown(call, containment, bundle)

    assert isinstance(repaired, QueryToolCall)
    assert repaired.measures == ["base_amount"]
    assert repaired.dimensions == ["account_type"]
    assert repaired.where == []


def test_repair_refuses_when_the_breakdown_dimension_is_ambiguous():
    call = CompoundQueryToolCall(
        steps=[
            _step(
                "credit_de",
                ["base_amount"],
                where=[_CREDIT, {"name": "country_code", "op": "eq", "value": "DE"}],
            ),
            _step("all_types", ["avg_base_amount"]),
        ],
        expression=_div(
            _ref("credit_de", "base_amount"),
            _ref("all_types", "avg_base_amount"),
        ),
        result_label="CREDIT in DE vs average",
    )
    bundle = _bundle()
    containment = detect_benchmark_containment(
        call, bundle, _GROUP_AVERAGE_QUESTION,
    )

    assert containment is not None
    assert containment.breakdown_dimension is None
    assert repair_benchmark_to_grouped_breakdown(call, containment, bundle) is None


def test_repair_refuses_when_peer_population_uses_a_different_period():
    call = _total_over_ungrouped_mean_plan()
    call.steps[1].where = [{
        "name": "business_date",
        "op": "between",
        "value": ["2025-01-01", "2025-12-31"],
    }]
    bundle = _bundle()
    containment = detect_benchmark_containment(
        call, bundle, _GROUP_AVERAGE_QUESTION,
    )

    assert containment is not None
    assert repair_benchmark_to_grouped_breakdown(call, containment, bundle) is None


# --- pipeline ---------------------------------------------------------------


def _pipeline_cfg():
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        judge_mode="async",
        safety_policy="",
        max_query_complexity=0,
        max_compound_steps=5,
        chart_type_selector="none",
        agent_output_format="plain",
        include_data_table=False,
    )


def _execution_stub():
    return types.SimpleNamespace(
        sql="SELECT ...",
        columns=["account_type", "base_amount"],
        rows=[
            {"account_type": "WALLET", "base_amount": 36_487_318.78},
            {"account_type": "LOAN", "base_amount": 36_245_683.67},
            {"account_type": "CREDIT", "base_amount": 36_179_774.10},
            {"account_type": "SAVINGS", "base_amount": 35_965_373.66},
            {"account_type": "CURRENT", "base_amount": 35_842_415.96},
        ],
        rows_returned=5,
        route_type="source",
        routed_sql="SELECT ...",
        aggregate_id=None,
        pocket_id=None,
        execution_ms=5,
        truncated=False,
        row_security_denied=False,
    )


def _compound_plan_json(steps: list[dict], expression: dict) -> str:
    return json.dumps({
        "compound_query": {
            "steps": steps,
            "expression": expression,
            "result_label": "CREDIT vs average",
        }
    })


def _step_json(
    name: str,
    measures: list[str],
    dimensions: list[str] | None = None,
    where: list[dict] | None = None,
) -> dict:
    return {
        "name": name,
        "model_id": str(MODEL_ID),
        "measures": measures,
        "dimensions": dimensions or [],
        "where": where or [],
        "having": [],
        "sort": [],
    }


@pytest.mark.asyncio
async def test_run_turn_executes_the_breakdown_not_the_bad_benchmark():
    raw_plan = _compound_plan_json(
        [
            _step_json("credit", ["base_amount"], where=[_CREDIT, dict(_YEAR_WINDOW)]),
            _step_json("all_types", ["avg_base_amount"], where=[dict(_YEAR_WINDOW)]),
        ],
        _div(
            _ref("credit", "base_amount"),
            _ref("all_types", "avg_base_amount"),
        ),
    )
    adapter = MagicMock()
    adapter.complete = AsyncMock(return_value=raw_plan)
    adapter.last_usage = {"input_tokens": 10, "output_tokens": 5}
    db = AsyncMock()
    db.get = AsyncMock(return_value=types.SimpleNamespace(display_name="Model Y"))
    execute_query = AsyncMock(return_value=_execution_stub())

    with patch("src.pipeline.assemble_prompt", AsyncMock(return_value=_bundle())), \
         patch("src.pipeline.resolve_agent_llm_failover_configs", AsyncMock(return_value=[
             types.SimpleNamespace(provider="test", model_name="planner", timeout_seconds=30)
         ])), \
         patch("src.pipeline.build_adapter", return_value=adapter), \
         patch("src.pipeline.RetryingAdapter", side_effect=lambda a: a), \
         patch("src.pipeline.check_budget", AsyncMock(return_value=None)), \
         patch("src.pipeline._attempt_tool_call_correction", AsyncMock(return_value=None)), \
         patch("src.pipeline.execute_query", execute_query), \
         patch("src.pipeline._load_measure_formats", AsyncMock(return_value={})), \
         patch("src.pipeline.narrate_answer", AsyncMock(return_value="CREDIT sits mid-pack.")), \
         patch("src.pipeline.apply_output_guardrails",
               side_effect=lambda c, text: types.SimpleNamespace(text=text, actions=[])), \
         patch("src.pipeline.build_citations", AsyncMock(return_value=[])):
        outcome = await run_turn(
            db=db,
            cfg=_pipeline_cfg(),
            conversation=types.SimpleNamespace(
                id=uuid.uuid4(), persona_id=None, pinned_model_id=None,
            ),
            user_message=(
                "How does the CREDIT account type's base amount compare to the "
                "average base amount across all account types?"
            ),
            jwt_token="token",
        )

    assert outcome.status == "ok"
    assert execute_query.await_count == 1
    executed = execute_query.await_args.args[1]
    assert isinstance(executed, QueryToolCall)
    assert executed.measures == ["base_amount"]
    assert executed.dimensions == ["account_type"]
    assert all(item.get("name") != "account_type" for item in executed.where)
    # The turn is served by the breakdown plan, not the compound benchmark.
    assert "compound_query" not in outcome.plan
    assert outcome.plan["query"]["dimensions"] == ["account_type"]


@pytest.mark.asyncio
async def test_run_turn_preserves_explicit_fixed_baseline_ratio():
    raw_plan = _compound_plan_json(
        [
            _step_json("credit", ["base_amount"], where=[_CREDIT]),
            _step_json(
                "by_type", ["base_amount"], dimensions=["account_type"],
            ),
        ],
        _div(
            _ref("credit", "base_amount"),
            _ref("by_type", "base_amount"),
        ),
    )
    adapter = MagicMock()
    adapter.complete = AsyncMock(return_value=raw_plan)
    adapter.last_usage = {"input_tokens": 10, "output_tokens": 5}
    scalar = types.SimpleNamespace(
        **{
            **_execution_stub().__dict__,
            "columns": ["base_amount"],
            "rows": [{"base_amount": 100}],
            "rows_returned": 1,
        }
    )
    grouped = types.SimpleNamespace(
        **{
            **_execution_stub().__dict__,
            "rows": [
                {"account_type": "CREDIT", "base_amount": 100},
                {"account_type": "SAVINGS", "base_amount": 50},
            ],
            "rows_returned": 2,
        }
    )
    execute_query = AsyncMock(side_effect=[scalar, grouped])

    with patch("src.pipeline.assemble_prompt", AsyncMock(return_value=_bundle())), \
         patch("src.pipeline.resolve_agent_llm_failover_configs", AsyncMock(return_value=[
             types.SimpleNamespace(provider="test", model_name="planner", timeout_seconds=30)
         ])), \
         patch("src.pipeline.build_adapter", return_value=adapter), \
         patch("src.pipeline.RetryingAdapter", side_effect=lambda a: a), \
         patch("src.pipeline.check_budget", AsyncMock(return_value=None)), \
         patch("src.pipeline._attempt_tool_call_correction", AsyncMock(return_value=None)), \
         patch("src.pipeline.execute_query", execute_query), \
         patch("src.pipeline._load_measure_formats", AsyncMock(return_value={})), \
         patch("src.pipeline.narrate_answer", AsyncMock(return_value="Ratios returned.")), \
         patch("src.pipeline.apply_output_guardrails",
               side_effect=lambda c, text: types.SimpleNamespace(text=text, actions=[])), \
         patch("src.pipeline.build_citations", AsyncMock(return_value=[])):
        outcome = await run_turn(
            db=AsyncMock(),
            cfg=_pipeline_cfg(),
            conversation=types.SimpleNamespace(
                id=uuid.uuid4(), persona_id=None, pinned_model_id=None,
            ),
            user_message=(
                "Use CREDIT as a fixed baseline. Show CREDIT's total divided "
                "by each account type's total."
            ),
            jwt_token="token",
        )

    assert outcome.status == "ok"
    assert execute_query.await_count == 2
    assert "compound_query" in outcome.plan
    assert [step["name"] for step in outcome.plan["compound_query"]["steps"]] == [
        "credit",
        "by_type",
    ]


@pytest.mark.asyncio
async def test_Bug_9973_run_turn_keeps_absolute_totals_as_primary_output():
    raw_plan = _compound_plan_json(
        [
            _step_json("by_account_type", ["base_amount"], dimensions=["account_type"]),
            _step_json("overall", ["base_amount"]),
        ],
        {
            "op": "sub",
            "args": [
                _ref("by_account_type", "base_amount"),
                {
                    "op": "div",
                    "args": [_ref("overall", "base_amount"), {"const": 5}],
                },
            ],
        },
    )
    adapter = MagicMock()
    adapter.complete = AsyncMock(return_value=raw_plan)
    adapter.last_usage = {"input_tokens": 10, "output_tokens": 5}
    db = AsyncMock()
    db.get = AsyncMock(return_value=types.SimpleNamespace(display_name="Model Y"))
    execute_query = AsyncMock(return_value=_execution_stub())

    with patch("src.pipeline.assemble_prompt", AsyncMock(return_value=_bundle())), \
         patch("src.pipeline.resolve_agent_llm_failover_configs", AsyncMock(return_value=[
             types.SimpleNamespace(provider="test", model_name="planner", timeout_seconds=30)
         ])), \
         patch("src.pipeline.build_adapter", return_value=adapter), \
         patch("src.pipeline.RetryingAdapter", side_effect=lambda a: a), \
         patch("src.pipeline.check_budget", AsyncMock(return_value=None)), \
         patch("src.pipeline._attempt_tool_call_correction", AsyncMock(return_value=None)), \
         patch("src.pipeline.execute_query", execute_query), \
         patch("src.pipeline._load_measure_formats", AsyncMock(return_value={})), \
         patch("src.pipeline.narrate_answer", AsyncMock(return_value="Absolute totals returned.")), \
         patch("src.pipeline.apply_output_guardrails",
               side_effect=lambda c, text: types.SimpleNamespace(text=text, actions=[])), \
         patch("src.pipeline.build_citations", AsyncMock(return_value=[])):
        outcome = await run_turn(
            db=db,
            cfg=_pipeline_cfg(),
            conversation=types.SimpleNamespace(
                id=uuid.uuid4(), persona_id=None, pinned_model_id=None,
            ),
            user_message=_TOTALS_PLUS_BENCHMARK_QUESTION,
            jwt_token="token",
        )

    assert outcome.status == "ok"
    assert execute_query.await_count == 1
    executed = execute_query.await_args.args[1]
    assert isinstance(executed, QueryToolCall)
    assert executed.measures == ["base_amount"]
    assert executed.dimensions == ["account_type"]
    assert executed.sort == [{"name": "base_amount", "direction": "desc"}]
    assert "compound_query" not in outcome.plan
    assert outcome.semantic_query["shape"]["narration_facts"]["average_comparison"] == {
        "status": "available",
        "category_field": "account_type",
        "value_field": "base_amount",
        "category_count": 5,
        "average_value": pytest.approx(36_144_113.234),
        "target_category": "CREDIT",
        "target_value": 36_179_774.10,
        "target_relation": "above",
        "comparisons": {
            "WALLET": {"value": 36_487_318.78, "relation": "above"},
            "LOAN": {"value": 36_245_683.67, "relation": "above"},
            "CREDIT": {"value": 36_179_774.10, "relation": "above"},
            "SAVINGS": {"value": 35_965_373.66, "relation": "below"},
            "CURRENT": {"value": 35_842_415.96, "relation": "below"},
        },
    }


@pytest.mark.asyncio
async def test_Bug_9974_run_turn_passes_temporal_filter_to_shape_facts():
    profile = _profile()
    profile.dimension_names = [*profile.dimension_names, "business_date_year"]
    profile.filterable_where_names = [*profile.filterable_where_names, "business_date_year"]
    profile.sortable_names = [*profile.sortable_names, "business_date_year"]
    bundle = _bundle(profile)
    raw_plan = json.dumps({
        "query": {
            "model_id": str(MODEL_ID),
            "measures": ["base_amount"],
            "dimensions": ["business_date_year"],
            "where": [{
                "name": "business_date",
                "op": "between",
                "value": ["2026-01-01", "2026-09-10"],
            }],
            "having": [],
            "sort": [],
        },
    })
    adapter = MagicMock()
    adapter.complete = AsyncMock(return_value=raw_plan)
    adapter.last_usage = {"input_tokens": 10, "output_tokens": 5}
    db = AsyncMock()
    db.get = AsyncMock(return_value=types.SimpleNamespace(display_name="Model Y"))
    execute_query = AsyncMock(return_value=types.SimpleNamespace(
        sql="SELECT ...",
        columns=["business_date_year", "base_amount"],
        rows=[
            {"business_date_year": "2026-01-01T00:00:00Z", "base_amount": 10},
            {"business_date_year": "2026-01-01T00:00:00Z", "base_amount": 20},
        ],
        rows_returned=2,
        route_type="source",
        routed_sql="SELECT ...",
        aggregate_id=None,
        pocket_id=None,
        execution_ms=5,
        truncated=False,
        row_security_denied=False,
    ))

    with patch("src.pipeline.assemble_prompt", AsyncMock(return_value=bundle)), \
         patch("src.pipeline.resolve_agent_llm_failover_configs", AsyncMock(return_value=[
             types.SimpleNamespace(provider="test", model_name="planner", timeout_seconds=30)
         ])), \
         patch("src.pipeline.build_adapter", return_value=adapter), \
         patch("src.pipeline.RetryingAdapter", side_effect=lambda a: a), \
         patch("src.pipeline.check_budget", AsyncMock(return_value=None)), \
         patch("src.pipeline._attempt_tool_call_correction", AsyncMock(return_value=None)), \
         patch("src.pipeline.execute_query", execute_query), \
         patch("src.pipeline._load_measure_formats", AsyncMock(return_value={})), \
         patch("src.pipeline.narrate_answer", AsyncMock(return_value="Year coverage returned.")), \
         patch("src.pipeline.apply_output_guardrails",
               side_effect=lambda c, text: types.SimpleNamespace(text=text, actions=[])), \
         patch("src.pipeline.build_citations", AsyncMock(return_value=[])):
        outcome = await run_turn(
            db=db,
            cfg=_pipeline_cfg(),
            conversation=types.SimpleNamespace(
                id=uuid.uuid4(), persona_id=None, pinned_model_id=None,
            ),
            user_message="Show the total base amount by year from January through September 2026.",
            jwt_token="token",
        )

    assert outcome.status == "ok"
    assert execute_query.await_count == 1
    shape = outcome.semantic_query["shape"]["narration_facts"]
    assert shape["date_range"]["business_date_year"] == (
        "2026-01-01",
        "2026-09-10",
    )


@pytest.mark.asyncio
async def test_run_turn_refuses_the_unrepairable_benchmark_before_any_sql():
    raw_plan = _compound_plan_json(
        [
            _step_json(
                "credit_de",
                ["base_amount"],
                where=[_CREDIT, {"name": "country_code", "op": "eq", "value": "DE"}],
            ),
            _step_json("all_types", ["avg_base_amount"]),
        ],
        _div(
            _ref("credit_de", "base_amount"),
            _ref("all_types", "avg_base_amount"),
        ),
    )
    adapter = MagicMock()
    adapter.complete = AsyncMock(return_value=raw_plan)
    adapter.last_usage = {"input_tokens": 10, "output_tokens": 5}
    execute_query = AsyncMock()
    correction = AsyncMock(return_value=None)

    with patch("src.pipeline.assemble_prompt", AsyncMock(return_value=_bundle())), \
         patch("src.pipeline.resolve_agent_llm_failover_configs", AsyncMock(return_value=[
             types.SimpleNamespace(provider="test", model_name="planner", timeout_seconds=30)
         ])), \
         patch("src.pipeline.build_adapter", return_value=adapter), \
         patch("src.pipeline.RetryingAdapter", side_effect=lambda a: a), \
         patch("src.pipeline.check_budget", AsyncMock(return_value=None)), \
         patch("src.pipeline._attempt_tool_call_correction", correction), \
         patch("src.pipeline.execute_query", execute_query):
        outcome = await run_turn(
            db=AsyncMock(),
            cfg=_pipeline_cfg(),
            conversation=types.SimpleNamespace(
                id=uuid.uuid4(), persona_id=None, pinned_model_id=None,
                ),
                user_message=(
                    "How does CREDIT in Germany compare to the average across "
                    "all account types?"
                ),
            jwt_token="token",
        )

    assert outcome.status == "refused"
    issues = outcome.plan["validation"]["issues"]
    assert issues[0]["reason"] == BENCHMARK_TOTAL_OVER_UNGROUPED_MEAN
    assert "different kinds of figure" in outcome.answer_text
    execute_query.assert_not_awaited()
    # A wrong benchmark is not a planner typo: no correction round-trip.
    correction.assert_not_awaited()
