"""Tests for compound query feature.

Bug-5346 — the combine expression is a semantic tree of data (ExprNode), never
a formula string. Helpers below build trees compactly.
"""
from __future__ import annotations

import json
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.recipes.eval import (
    CombineEvalError,
    evaluate_combine,
    evaluate_combine_aligned,
    validate_expression,
)
from src.tools.spec import (
    CompoundStep,
    CompoundQueryToolCall,
    ToolCallParseError,
    parse_tool_call,
    make_tool_spec,
)
from src.narrate.narrate import _build_compound_narrate_prompt
from src.charts.renderer import render_compound_result


# --- ExprNode tree helpers --------------------------------------------------

def _ref(step: str, measure: str) -> dict:
    return {"ref": {"step": step, "measure": measure}}


def _c(value) -> dict:
    return {"const": value}


def _op(name: str, *args) -> dict:
    return {"op": name, "args": list(args)}


def _pct(num: dict, den: dict) -> dict:
    """round(num / den * 100, 2)."""
    return _op("round", _op("mul", _op("div", num, den), _c(100)), _c(2))


# ---------------------------------------------------------------------------
# validate_expression
# ---------------------------------------------------------------------------

def _step(name: str, measures: list[str]) -> types.SimpleNamespace:
    return types.SimpleNamespace(name=name, measures=measures)


def test_validate_expression_valid():
    steps = [_step("germany", ["transaction_amount"]), _step("worldwide", ["transaction_amount"])]
    errors = validate_expression(
        _pct(_ref("germany", "transaction_amount"), _ref("worldwide", "transaction_amount")),
        steps,
    )
    assert errors == []


def test_validate_expression_unknown_step():
    steps = [_step("germany", ["transaction_amount"])]
    errors = validate_expression(_ref("france", "transaction_amount"), steps)
    assert len(errors) == 1
    assert "france" in errors[0]


def test_validate_expression_unknown_measure():
    steps = [_step("germany", ["transaction_amount"])]
    errors = validate_expression(_ref("germany", "refund_amount"), steps)
    assert len(errors) == 1
    assert "refund_amount" in errors[0]


def test_validate_expression_malformed_tree():
    # A binary op with one argument is structurally invalid.
    steps = [_step("germany", ["transaction_amount"])]
    errors = validate_expression(_op("add", _ref("germany", "transaction_amount")), steps)
    assert len(errors) == 1


def test_validate_expression_allowed_functions():
    steps = [_step("a", ["v"]), _step("b", ["v"])]
    errors = validate_expression(_pct(_ref("a", "v"), _ref("b", "v")), steps)
    assert errors == []


def test_validate_expression_disallowed_function():
    steps = [_step("a", ["v"])]
    errors = validate_expression(_op("eval", _ref("a", "v")), steps)
    assert len(errors) == 1
    assert "eval" in errors[0]


def test_validate_expression_none_is_valid():
    # No combine step = no expression = valid (no errors).
    assert validate_expression(None, []) == []


def test_validate_expression_non_object_rejected():
    errors = validate_expression("germany.tx", [_step("germany", ["tx"])])
    assert len(errors) == 1


def test_validate_expression_constants_only():
    steps = [_step("a", ["v"])]
    errors = validate_expression(_op("add", _c(1), _c(2)), steps)
    assert errors == []


# ---------------------------------------------------------------------------
# CompoundStep / CompoundQueryToolCall dataclasses
# ---------------------------------------------------------------------------

def test_compound_step_dataclass():
    step = CompoundStep(
        name="germany",
        model_id="abc-123",
        measures=["transaction_amount"],
        dimensions=[],
        where=[{"name": "country_code", "op": "eq", "value": "DE"}],
        having=[],
        sort=[],
        limit=100,
    )
    assert step.name == "germany"
    assert step.model_id == "abc-123"
    assert step.measures == ["transaction_amount"]
    assert step.limit == 100


# ---------------------------------------------------------------------------
# parse_tool_call — compound_query
# ---------------------------------------------------------------------------

_DEFAULT_EXPR = _op("mul", _op("div", _ref("a", "v"), _ref("b", "v")), _c(100))


def _make_compound_json(
    steps=None,
    expression=None,
    result_label="ratio",
) -> str:
    if expression is None:
        expression = _DEFAULT_EXPR
    if steps is None:
        steps = [
            {
                "name": "a",
                "model_id": "model-1",
                "measures": ["v"],
                "dimensions": [],
                "where": [],
                "having": [],
                "sort": [],
                "limit": 100,
            },
            {
                "name": "b",
                "model_id": "model-1",
                "measures": ["v"],
                "dimensions": [],
                "where": [],
                "having": [],
                "sort": [],
                "limit": 100,
            },
        ]
    return json.dumps({
        "compound_query": {
            "steps": steps,
            "expression": expression,
            "result_label": result_label,
        }
    })


def test_parse_compound_query_valid():
    call = parse_tool_call(_make_compound_json())
    assert isinstance(call, CompoundQueryToolCall)
    assert len(call.steps) == 2
    assert call.steps[0].name == "a"
    assert call.steps[1].name == "b"
    assert call.expression == _DEFAULT_EXPR
    assert call.result_label == "ratio"


def test_parse_compound_query_accepts_structured_dimensions():
    raw = _make_compound_json(steps=[
        {
            "name": "a",
            "model_id": "m1",
            "measures": ["v"],
            "dimensions": [{"name": "business_date", "grain": "month"}],
            "where": [],
            "having": [],
            "sort": [],
            "limit": 100,
        },
        {
            "name": "b",
            "model_id": "m1",
            "measures": ["v"],
            "dimensions": [{"name": "business_date", "grain": "month"}],
            "where": [],
            "having": [],
            "sort": [],
            "limit": 100,
        },
    ])
    call = parse_tool_call(raw)
    assert isinstance(call, CompoundQueryToolCall)
    assert call.steps[0].dimensions == ["business_date_month"]
    assert call.steps[0].dimension_refs[0].is_bare is False
    assert call.steps[0].dimension_refs[0].base_fields == ("business_date",)


def test_parse_compound_query_dimension_exprs_wins_on_replay():
    plan = {
        "compound_query": {
            "steps": [
                {
                    "name": "a",
                    "model_id": "m1",
                    "measures": ["v"],
                    "dimensions": ["business_date_month"],
                    "dimension_exprs": [{"name": "business_date", "grain": "month"}],
                    "where": [],
                    "having": [],
                    "sort": [],
                    "limit": 100,
                },
                {
                    "name": "b",
                    "model_id": "m1",
                    "measures": ["v"],
                    "dimensions": ["business_date_month"],
                    "dimension_exprs": [{"name": "business_date", "grain": "month"}],
                    "where": [],
                    "having": [],
                    "sort": [],
                    "limit": 100,
                },
            ],
            "expression": _DEFAULT_EXPR,
            "result_label": "ratio",
        }
    }
    call = parse_tool_call(json.dumps(plan))
    assert isinstance(call, CompoundQueryToolCall)
    assert call.steps[0].dimension_refs[0].is_bare is False
    assert call.steps[0].dimension_refs[0].raw == {"name": "business_date", "grain": "month"}


def test_parse_compound_query_missing_steps():
    raw = json.dumps({"compound_query": {"expression": _ref("a", "v"), "result_label": "r"}})
    with pytest.raises(ToolCallParseError, match="(?i)steps"):
        parse_tool_call(raw)


def test_parse_compound_query_single_step():
    raw = _make_compound_json(steps=[{
        "name": "only",
        "model_id": "m1",
        "measures": ["v"],
        "dimensions": [],
        "where": [],
        "having": [],
        "sort": [],
        "limit": 100,
    }])
    with pytest.raises(ToolCallParseError, match="at least 2"):
        parse_tool_call(raw)


def test_parse_compound_query_duplicate_step_names():
    raw = _make_compound_json(steps=[
        {"name": "dup", "model_id": "m1", "measures": ["v"], "dimensions": [], "where": [], "having": [], "sort": [], "limit": 100},
        {"name": "dup", "model_id": "m1", "measures": ["v"], "dimensions": [], "where": [], "having": [], "sort": [], "limit": 100},
    ])
    with pytest.raises(ToolCallParseError, match="(?i)duplicate"):
        parse_tool_call(raw)


def test_parse_compound_query_missing_expression():
    raw = json.dumps({"compound_query": {
        "steps": [
            {"name": "a", "model_id": "m1", "measures": ["v"], "dimensions": [], "where": [], "having": [], "sort": [], "limit": 100},
            {"name": "b", "model_id": "m1", "measures": ["v"], "dimensions": [], "where": [], "having": [], "sort": [], "limit": 100},
        ],
        "result_label": "r",
    }})
    with pytest.raises(ToolCallParseError, match="(?i)expression"):
        parse_tool_call(raw)


def test_parse_compound_query_string_expression_rejected():
    raw = _make_compound_json(expression="a.v / b.v")
    with pytest.raises(ToolCallParseError, match="(?i)expression"):
        parse_tool_call(raw)


def test_parse_compound_query_missing_result_label():
    raw = json.dumps({"compound_query": {
        "steps": [
            {"name": "a", "model_id": "m1", "measures": ["v"], "dimensions": [], "where": [], "having": [], "sort": [], "limit": 100},
            {"name": "b", "model_id": "m1", "measures": ["v"], "dimensions": [], "where": [], "having": [], "sort": [], "limit": 100},
        ],
        "expression": _op("add", _ref("a", "v"), _ref("b", "v")),
    }})
    with pytest.raises(ToolCallParseError, match="(?i)result_label"):
        parse_tool_call(raw)


def test_parse_compound_query_step_reuses_query_validation():
    raw = _make_compound_json(steps=[
        {
            "name": "a",
            "model_id": "m1",
            "measures": ["v"],
            "dimensions": ["region"],
            "where": [{"name": "country", "op": "eq", "value": "DE"}],
            "having": [{"name": "v", "op": "gt", "value": 100}],
            "sort": [{"name": "v", "direction": "desc"}],
            "limit": 10,
        },
        {
            "name": "b",
            "model_id": "m1",
            "measures": ["v"],
            "dimensions": [],
            "where": [],
            "having": [],
            "sort": [],
            "limit": 100,
        },
    ])
    call = parse_tool_call(raw)
    assert isinstance(call, CompoundQueryToolCall)
    assert call.steps[0].where == [{"name": "country", "op": "eq", "value": "DE"}]
    assert call.steps[0].having == [{"name": "v", "op": "gt", "value": 100}]
    assert call.steps[0].sort == [{"name": "v", "direction": "desc"}]
    assert call.steps[0].limit == 10


def test_parse_compound_query_step_missing_model_id():
    raw = _make_compound_json(steps=[
        {"name": "a", "measures": ["v"], "dimensions": [], "where": [], "having": [], "sort": [], "limit": 100},
        {"name": "b", "model_id": "m1", "measures": ["v"], "dimensions": [], "where": [], "having": [], "sort": [], "limit": 100},
    ])
    with pytest.raises(ToolCallParseError, match="(?i)model_id"):
        parse_tool_call(raw)


def test_parse_compound_query_chart_type_optional():
    raw = json.dumps({"compound_query": {
        "steps": [
            {"name": "a", "model_id": "m1", "measures": ["v"], "dimensions": [], "where": [], "having": [], "sort": [], "limit": 100},
            {"name": "b", "model_id": "m1", "measures": ["v"], "dimensions": [], "where": [], "having": [], "sort": [], "limit": 100},
        ],
        "expression": _op("add", _ref("a", "v"), _ref("b", "v")),
        "result_label": "total",
        "chart_type": "kpi",
    }})
    call = parse_tool_call(raw)
    assert isinstance(call, CompoundQueryToolCall)
    assert call.chart_type == "kpi"


def test_parse_compound_query_no_chart_type_defaults_none():
    call = parse_tool_call(_make_compound_json())
    assert isinstance(call, CompoundQueryToolCall)
    assert call.chart_type is None


# ---------------------------------------------------------------------------
# Compound narration prompt building
# ---------------------------------------------------------------------------

def test_build_compound_narrate_prompt_structure():
    step_summaries = [
        {
            "name": "germany",
            "columns": ["transaction_amount"],
            "rows_returned": 1,
            "sample_rows": [{"transaction_amount": 2_334_940.63}],
        },
        {
            "name": "worldwide",
            "columns": ["transaction_amount"],
            "rows_returned": 1,
            "sample_rows": [{"transaction_amount": 21_037_698.04}],
        },
    ]
    computed = {
        "label": "Germany share (%)",
        "value": 11.1,
    }
    system, user_prompt = _build_compound_narrate_prompt(
        "You are an analyst.",
        "What percentage of transactions are from Germany?",
        step_summaries,
        computed,
    )
    assert system == "You are an analyst."
    assert "multiple" in user_prompt.lower()
    assert "11.1" in user_prompt
    assert "Germany share" in user_prompt
    assert "germany" in user_prompt
    assert "worldwide" in user_prompt
    assert "server-calculated" in user_prompt.lower()


def test_build_compound_narrate_prompt_contains_all_steps():
    step_summaries = [
        {"name": "jan", "columns": ["revenue"], "rows_returned": 1, "sample_rows": [{"revenue": 100}]},
        {"name": "feb", "columns": ["revenue"], "rows_returned": 1, "sample_rows": [{"revenue": 120}]},
    ]
    computed = {"label": "Growth (%)", "value": 20.0}
    _, user_prompt = _build_compound_narrate_prompt("sys", "Growth?", step_summaries, computed)
    assert "jan" in user_prompt
    assert "feb" in user_prompt
    assert "20.0" in user_prompt


def test_build_compound_narrate_prompt_includes_shape_facts():
    computed = {
        "expression": {"op": "div", "args": []},
        "label": "share",
        "value": None,
        "is_multi_row": True,
        "result_columns": ["period", "country", "share"],
        "result_rows": [{"period": "2026-01", "country": "GB", "share": 10}],
        "shape": {
            "shape": "multi_series_time",
            "output_mode": "chart_table",
            "narration_facts": {
                "series_coverage": {
                    "GB": {"first_period": "2026-01", "last_period": "2026-01"}
                }
            },
        },
    }

    _, user_prompt = _build_compound_narrate_prompt("sys", "share trend", [], computed)

    assert "Deterministic shape facts for narration" in user_prompt
    assert "multi_series_time" in user_prompt
    assert "series_coverage" in user_prompt


# ---------------------------------------------------------------------------
# Compound result rendering
# ---------------------------------------------------------------------------

def test_render_compound_combined_table():
    step_results = [
        {"name": "germany", "columns": ["transaction_amount"], "rows": [{"transaction_amount": 2_334_940.63}]},
        {"name": "worldwide", "columns": ["transaction_amount"], "rows": [{"transaction_amount": 21_037_698.04}]},
    ]
    computed = {"label": "Germany share (%)", "value": 11.1}
    html = render_compound_result(step_results, computed)
    assert "compound-summary" in html
    assert "germany" in html
    assert "worldwide" in html
    assert "11.1" in html
    assert "Germany share" in html


def test_render_compound_stacked_sections():
    step_results = [
        {"name": "revenue", "columns": ["transaction_amount"], "rows": [{"transaction_amount": 100}]},
        {"name": "detail", "columns": ["merchant_name", "transaction_count"], "rows": [{"merchant_name": "Acme", "transaction_count": 50}]},
    ]
    computed = {"label": "Avg per merchant", "value": 2.0}
    html = render_compound_result(step_results, computed)
    assert "compound-step" in html
    assert "compound-kpi" in html
    assert "2" in html


def test_render_compound_kpi_always_present():
    step_results = [
        {"name": "a", "columns": ["v"], "rows": [{"v": 10}]},
        {"name": "b", "columns": ["v"], "rows": [{"v": 20}]},
    ]
    computed = {"label": "Sum", "value": 30}
    html = render_compound_result(step_results, computed)
    assert "30" in html


def test_render_compound_empty_steps():
    html = render_compound_result([], {"label": "x", "value": 0})
    assert "0" in html


# ---------------------------------------------------------------------------
# _plan_dict for compound_query
# ---------------------------------------------------------------------------

from src.pipeline import (
    _plan_dict,
    _prepare_compound_alignment,
    _run_compound_query_branch,
    TurnOutcome,
)


def test_plan_dict_compound_query():
    expr = _op("add", _ref("a", "v"), _ref("b", "v"))
    call = CompoundQueryToolCall(
        steps=[
            CompoundStep("a", "m1", ["v"], [], [], [], [], 100),
            CompoundStep("b", "m1", ["v"], [], [], [], [], 100),
        ],
        expression=expr,
        result_label="total",
    )
    plan = _plan_dict(call)
    assert "compound_query" in plan
    inner = plan["compound_query"]
    assert len(inner["steps"]) == 2
    assert inner["expression"] == expr
    assert inner["result_label"] == "total"
    assert "chart_type" not in inner


def test_plan_dict_compound_query_with_chart_type():
    call = CompoundQueryToolCall(
        steps=[
            CompoundStep("a", "m1", ["v"], [], [], [], [], 100),
            CompoundStep("b", "m1", ["v"], [], [], [], [], 100),
        ],
        expression=_op("add", _ref("a", "v"), _ref("b", "v")),
        result_label="total",
        chart_type="kpi",
    )
    plan = _plan_dict(call)
    assert plan["compound_query"]["chart_type"] == "kpi"


def test_plan_dict_compound_query_carries_dimension_exprs_when_grained():
    call = parse_tool_call(_make_compound_json(steps=[
        {
            "name": "a",
            "model_id": "m1",
            "measures": ["v"],
            "dimensions": [{"name": "business_date", "grain": "month"}],
            "where": [],
            "having": [],
            "sort": [],
            "limit": 100,
        },
        {
            "name": "b",
            "model_id": "m1",
            "measures": ["v"],
            "dimensions": [{"name": "business_date", "grain": "month"}],
            "where": [],
            "having": [],
            "sort": [],
            "limit": 100,
        },
    ]))
    plan = _plan_dict(call)["compound_query"]
    assert plan["steps"][0]["dimensions"] == ["business_date_month"]
    assert plan["steps"][0]["dimension_exprs"] == [
        {"name": "business_date", "grain": "month"}
    ]


# ---------------------------------------------------------------------------
# _run_compound_query_branch
# ---------------------------------------------------------------------------

def _make_query_execution(rows, columns):
    return MagicMock(
        rows=rows,
        columns=columns,
        rows_returned=len(rows),
        sql="SELECT ...",
        routed_sql="SELECT ...",
        route_type="source",
    )


_ALLOW_LISTED_MODEL = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def compound_call():
    return CompoundQueryToolCall(
        steps=[
            CompoundStep("germany", _ALLOW_LISTED_MODEL, ["transaction_amount"], [],
                         [{"name": "country", "op": "eq", "value": "DE"}], [], [], 100),
            CompoundStep("worldwide", _ALLOW_LISTED_MODEL, ["transaction_amount"], [],
                         [], [], [], 100),
        ],
        expression=_pct(_ref("germany", "transaction_amount"),
                        _ref("worldwide", "transaction_amount")),
        result_label="Germany share (%)",
    )


@pytest.mark.asyncio
async def test_compound_branch_happy_path(compound_call):
    db = AsyncMock()
    cfg = MagicMock()
    cfg.project_id = uuid.uuid4()
    cfg.max_compound_steps = 5
    cfg.max_query_complexity = 0
    cfg.chart_type_selector = "none"
    cfg.chart_color_palette = "default"
    cfg.chart_size = "md"
    cfg.chart_max_rows = 500
    cfg.include_data_table = True

    adapter = AsyncMock()
    adapter.complete = AsyncMock(return_value="Germany accounts for 11.1% of worldwide transactions.")
    adapter.last_usage = {"input_tokens": 100, "output_tokens": 50}

    bundle = MagicMock()
    bundle.narration_system = "You are an analyst."
    bundle.allow_list_model_ids = [uuid.UUID(_ALLOW_LISTED_MODEL)]

    exec_germany = _make_query_execution(
        [{"transaction_amount": 2_334_940.63}], ["transaction_amount"]
    )
    exec_worldwide = _make_query_execution(
        [{"transaction_amount": 21_037_698.04}], ["transaction_amount"]
    )

    with patch("src.pipeline.execute_query", new_callable=AsyncMock) as mock_exec, \
         patch("src.pipeline.apply_output_guardrails") as mock_guard:
        mock_exec.side_effect = [exec_germany, exec_worldwide]
        mock_guard.return_value = MagicMock(text="Germany accounts for 11.1%.", actions=[])

        outcome = await _run_compound_query_branch(
            db=db,
            cfg=cfg,
            adapter=adapter,
            bundle=bundle,
            user_message="What percentage of transactions are from Germany?",
            call=compound_call,
            jwt_token="tok",
            publisher=None,
            usage_totals={"input": 0, "output": 0},
            prompt_messages={"system": "...", "user": "..."},
            llm_raw_response='{"compound_query": {...}}',
        )

    assert isinstance(outcome, TurnOutcome)
    assert outcome.status == "ok"
    assert outcome.semantic_query["tool"] == "compound_query"
    assert len(outcome.semantic_query["steps"]) == 2
    assert outcome.semantic_query["combine_value"] == 11.1
    assert outcome.rows_returned == 2
    assert mock_exec.call_count == 2


@pytest.mark.asyncio
async def test_compound_branch_passes_dimension_refs_into_step_execution():
    call = parse_tool_call(_make_compound_json(steps=[
        {
            "name": "a",
            "model_id": _ALLOW_LISTED_MODEL,
            "measures": ["v"],
            "dimensions": [{"name": "business_date", "grain": "month"}],
            "where": [],
            "having": [],
            "sort": [],
            "limit": 100,
        },
        {
            "name": "b",
            "model_id": _ALLOW_LISTED_MODEL,
            "measures": ["v"],
            "dimensions": [{"name": "business_date", "grain": "month"}],
            "where": [],
            "having": [],
            "sort": [],
            "limit": 100,
        },
    ]))
    cfg = MagicMock()
    cfg.project_id = uuid.uuid4()
    cfg.max_compound_steps = 5
    cfg.max_query_complexity = 0
    cfg.chart_type_selector = "none"
    cfg.include_data_table = True
    adapter = AsyncMock()
    adapter.complete = AsyncMock(return_value="ratio")
    adapter.last_usage = {"input_tokens": 1, "output_tokens": 1}
    bundle = MagicMock()
    bundle.narration_system = "You are an analyst."
    bundle.allow_list_model_ids = [uuid.UUID(_ALLOW_LISTED_MODEL)]
    exec_a = _make_query_execution([{"business_date_month": "2026-01-01", "v": 10}], ["business_date_month", "v"])
    exec_b = _make_query_execution([{"business_date_month": "2026-01-01", "v": 5}], ["business_date_month", "v"])

    with patch("src.pipeline.execute_query", new_callable=AsyncMock) as mock_exec, \
         patch("src.pipeline.apply_output_guardrails") as mock_guard:
        mock_exec.side_effect = [exec_a, exec_b]
        mock_guard.return_value = MagicMock(text="ratio", actions=[])
        outcome = await _run_compound_query_branch(
            db=AsyncMock(),
            cfg=cfg,
            adapter=adapter,
            bundle=bundle,
            user_message="ratio by month",
            call=call,
            jwt_token="tok",
            publisher=None,
            usage_totals={"input": 0, "output": 0},
            prompt_messages=None,
            llm_raw_response=None,
        )

    first_step_call = mock_exec.await_args_list[0].args[1]
    assert first_step_call.dimension_refs[0].is_bare is False
    assert first_step_call.dimension_refs[0].base_fields == ("business_date",)
    assert outcome.status == "ok"
    assert mock_exec.call_count == 2


@pytest.mark.asyncio
async def test_compound_branch_step_count_exceeded(compound_call):
    cfg = MagicMock()
    cfg.max_compound_steps = 1

    outcome = await _run_compound_query_branch(
        db=AsyncMock(),
        cfg=cfg,
        adapter=AsyncMock(),
        bundle=MagicMock(),
        user_message="test",
        call=compound_call,
        jwt_token="tok",
        publisher=None,
        usage_totals={"input": 0, "output": 0},
        prompt_messages=None,
        llm_raw_response=None,
    )
    assert outcome.status == "refused"
    assert "exceeds" in outcome.answer_text.lower() or "limit" in outcome.answer_text.lower()


@pytest.mark.asyncio
async def test_compound_branch_expression_validation_failure():
    call = CompoundQueryToolCall(
        steps=[
            CompoundStep("a", _ALLOW_LISTED_MODEL, ["v"], [], [], [], [], 100),
            CompoundStep("b", _ALLOW_LISTED_MODEL, ["v"], [], [], [], [], 100),
        ],
        expression=_op("add", _ref("nonexistent", "v"), _ref("b", "v")),
        result_label="bad",
    )
    cfg = MagicMock()
    cfg.max_compound_steps = 5
    cfg.max_query_complexity = 0
    bundle = MagicMock()
    bundle.allow_list_model_ids = [uuid.UUID(_ALLOW_LISTED_MODEL)]

    outcome = await _run_compound_query_branch(
        db=AsyncMock(),
        cfg=cfg,
        adapter=AsyncMock(),
        bundle=bundle,
        user_message="test",
        call=call,
        jwt_token="tok",
        publisher=None,
        usage_totals={"input": 0, "output": 0},
        prompt_messages=None,
        llm_raw_response=None,
    )
    assert outcome.status == "refused"
    assert "nonexistent" in outcome.answer_text.lower()


@pytest.mark.asyncio
async def test_compound_branch_step_execution_failure(compound_call):
    from src.exec.query import QueryExecutionError

    cfg = MagicMock()
    cfg.max_compound_steps = 5
    cfg.max_query_complexity = 0
    bundle = MagicMock()
    bundle.allow_list_model_ids = [uuid.UUID(_ALLOW_LISTED_MODEL)]

    with patch("src.pipeline.execute_query", new_callable=AsyncMock) as mock_exec:
        mock_exec.side_effect = QueryExecutionError("column not found")

        outcome = await _run_compound_query_branch(
            db=AsyncMock(),
            cfg=cfg,
            adapter=AsyncMock(),
            bundle=bundle,
            user_message="test",
            call=compound_call,
            jwt_token="tok",
            publisher=None,
            usage_totals={"input": 0, "output": 0},
            prompt_messages=None,
            llm_raw_response=None,
        )
    assert outcome.status == "refused"
    assert "germany" in outcome.answer_text.lower() or "could not" in outcome.answer_text.lower()


# ---------------------------------------------------------------------------
# Prompt / tool spec includes compound_query
# ---------------------------------------------------------------------------

def test_tool_spec_includes_compound_query():
    spec = make_tool_spec("none")
    assert "compound_query" in spec


def test_tool_spec_llm_mode_includes_compound_query():
    spec = make_tool_spec("llm")
    assert "compound_query" in spec


def test_tool_spec_compound_query_has_steps():
    spec = make_tool_spec("none")
    assert "steps" in spec
    assert "expression" in spec
    assert "result_label" in spec


def test_tool_spec_compound_query_rules():
    spec = make_tool_spec("none")
    assert "COMPOUND QUERY RULES" in spec


def test_tool_spec_compound_query_describes_expression_tree():
    spec = make_tool_spec("none")
    assert "EXPRESSION TREE" in spec
    assert '"ref"' in spec


# ---------------------------------------------------------------------------
# Bug-233: per-step complexity check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compound_branch_step_too_complex():
    call = CompoundQueryToolCall(
        steps=[
            CompoundStep("a", _ALLOW_LISTED_MODEL, ["m1", "m2", "m3"], ["d1", "d2", "d3"], [], [], [], 100),
            CompoundStep("b", _ALLOW_LISTED_MODEL, ["m1"], [], [], [], [], 100),
        ],
        expression=_op("add", _ref("a", "m1"), _ref("b", "m1")),
        result_label="total",
    )
    cfg = MagicMock()
    cfg.max_compound_steps = 5
    cfg.max_query_complexity = 3

    bundle = MagicMock()
    bundle.allow_list_model_ids = [uuid.UUID(_ALLOW_LISTED_MODEL)]
    outcome = await _run_compound_query_branch(
        db=AsyncMock(),
        cfg=cfg,
        adapter=AsyncMock(),
        bundle=bundle,
        user_message="test",
        call=call,
        jwt_token="tok",
        publisher=None,
        usage_totals={"input": 0, "output": 0},
        prompt_messages=None,
        llm_raw_response=None,
    )
    assert outcome.status == "refused"
    assert "too complex" in outcome.answer_text.lower()


# ---------------------------------------------------------------------------
# Bug-234: thought summary wired into TurnOutcome
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compound_branch_thought_summary_populated(compound_call):
    cfg = MagicMock()
    cfg.project_id = uuid.uuid4()
    cfg.max_compound_steps = 5
    cfg.max_query_complexity = 0
    cfg.chart_type_selector = "none"
    cfg.chart_color_palette = "default"
    cfg.chart_size = "md"
    cfg.chart_max_rows = 500
    cfg.include_data_table = True

    thought_text = "Queried Germany and worldwide separately then compared."
    narration_text = "Germany accounts for 11.1%."

    thinking_parts: list[str] = []

    async def _on_thinking(token: str) -> None:
        thinking_parts.append(token)

    async def _thinking_complete(system, user, on_thinking=None):
        if on_thinking:
            await on_thinking(thought_text)
        return narration_text

    adapter = AsyncMock()
    adapter.complete = _thinking_complete
    adapter.last_usage = {"input_tokens": 50, "output_tokens": 25}

    bundle = MagicMock()
    bundle.narration_system = "You are an analyst."
    bundle.allow_list_model_ids = [uuid.UUID(_ALLOW_LISTED_MODEL)]

    exec_germany = _make_query_execution(
        [{"transaction_amount": 2_334_940.63}], ["transaction_amount"]
    )
    exec_worldwide = _make_query_execution(
        [{"transaction_amount": 21_037_698.04}], ["transaction_amount"]
    )

    with patch("src.pipeline.execute_query", new_callable=AsyncMock) as mock_exec, \
         patch("src.pipeline.apply_output_guardrails") as mock_guard:
        mock_exec.side_effect = [exec_germany, exec_worldwide]
        mock_guard.return_value = MagicMock(text=narration_text, actions=[])

        outcome = await _run_compound_query_branch(
            db=AsyncMock(),
            cfg=cfg,
            adapter=adapter,
            bundle=bundle,
            user_message="What percentage of transactions are from Germany?",
            call=compound_call,
            jwt_token="tok",
            publisher=None,
            usage_totals={"input": 0, "output": 0},
            prompt_messages={"system": "...", "user": "..."},
            llm_raw_response='{"compound_query": {...}}',
            on_thinking=_on_thinking,
        )
    outcome.thought_summary = "".join(thinking_parts) or None

    assert outcome.status == "ok"
    assert outcome.thought_summary == thought_text


# ---------------------------------------------------------------------------
# Row-aligned expression evaluation
# ---------------------------------------------------------------------------

def test_prepare_compound_alignment_reuses_alias_for_same_semantic_dimension():
    call = parse_tool_call(_make_compound_json(steps=[
        {
            "name": "a",
            "model_id": "m1",
            "measures": ["v"],
            "dimensions": [{"name": "business_date", "grain": "month"}],
            "where": [],
            "having": [],
            "sort": [],
            "limit": 100,
        },
        {
            "name": "b",
            "model_id": "m1",
            "measures": ["v"],
            "dimensions": [
                {
                    "expr": {
                        "fn": "date_trunc",
                        "args": [{"literal": "month"}, {"field": "business_date"}],
                    },
                    "alias": "period",
                }
            ],
            "where": [],
            "having": [],
            "sort": [],
            "limit": 100,
        },
    ]))
    exec_a = _make_query_execution(
        [{"business_date_month": "2026-01-01", "v": 10}],
        ["business_date_month", "v"],
    )
    exec_b = _make_query_execution(
        [{"period": "2026-01-01", "v": 5}],
        ["period", "v"],
    )

    rows, dims, trace = _prepare_compound_alignment([
        (call.steps[0], exec_a),
        (call.steps[1], exec_b),
    ])

    assert dims == {"a": ["business_date_month"], "b": ["business_date_month"]}
    assert rows["b"][0]["business_date_month"] == "2026-01-01"
    assert trace[1]["dimension"] == "period"
    assert trace[1]["alignment_alias"] == "business_date_month"


def test_prepare_compound_alignment_does_not_join_same_alias_different_expression():
    call = parse_tool_call(_make_compound_json(steps=[
        {
            "name": "a",
            "model_id": "m1",
            "measures": ["v"],
            "dimensions": [{"name": "business_date", "grain": "month"}],
            "where": [],
            "having": [],
            "sort": [],
            "limit": 100,
        },
        {
            "name": "b",
            "model_id": "m1",
            "measures": ["v"],
            "dimensions": [
                {
                    "expr": {
                        "fn": "date_trunc",
                        "args": [{"literal": "month"}, {"field": "posting_date"}],
                    },
                    "alias": "business_date_month",
                }
            ],
            "where": [],
            "having": [],
            "sort": [],
            "limit": 100,
        },
    ]))
    exec_a = _make_query_execution(
        [{"business_date_month": "2026-01-01", "v": 10}],
        ["business_date_month", "v"],
    )
    exec_b = _make_query_execution(
        [{"business_date_month": "2026-01-01", "v": 5}],
        ["business_date_month", "v"],
    )

    rows, dims, trace = _prepare_compound_alignment([
        (call.steps[0], exec_a),
        (call.steps[1], exec_b),
    ])

    assert dims == {"a": ["business_date_month"], "b": ["business_date_month_2"]}
    assert rows["b"][0]["business_date_month_2"] == "2026-01-01"
    assert trace[0]["alignment_key"] != trace[1]["alignment_key"]


def test_aligned_eval_scalar_when_no_dimensions():
    step_rows = {
        "germany": [{"tx": 100}],
        "world": [{"tx": 1000}],
    }
    step_dims = {"germany": [], "world": []}
    rows, cols, multi, _mode = evaluate_combine_aligned(
        _pct(_ref("germany", "tx"), _ref("world", "tx")), step_rows, step_dims, "share"
    )
    assert not multi
    assert len(rows) == 1
    assert rows[0]["share"] == 10.0


def test_aligned_eval_multi_row_with_shared_dimensions():
    step_rows = {
        "germany": [
            {"month": 1, "tx": 50},
            {"month": 2, "tx": 80},
            {"month": 3, "tx": 120},
        ],
        "world": [
            {"month": 1, "tx": 500},
            {"month": 2, "tx": 400},
            {"month": 3, "tx": 600},
        ],
    }
    step_dims = {"germany": ["month"], "world": ["month"]}
    rows, cols, multi, _mode = evaluate_combine_aligned(
        _pct(_ref("germany", "tx"), _ref("world", "tx")), step_rows, step_dims, "share"
    )
    assert multi
    assert cols == ["month", "share"]
    assert len(rows) == 3
    assert rows[0] == {"month": 1, "share": 10.0}
    assert rows[1] == {"month": 2, "share": 20.0}
    assert rows[2] == {"month": 3, "share": 20.0}


def test_aligned_eval_inner_join_drops_unmatched():
    step_rows = {
        "a": [{"m": 1, "v": 10}, {"m": 2, "v": 20}],
        "b": [{"m": 2, "v": 5}, {"m": 3, "v": 15}],
    }
    step_dims = {"a": ["m"], "b": ["m"]}
    rows, cols, multi, _mode = evaluate_combine_aligned(
        _op("add", _ref("a", "v"), _ref("b", "v")), step_rows, step_dims, "total"
    )
    assert multi
    assert len(rows) == 1
    assert rows[0] == {"m": 2, "total": 25}


def test_aligned_eval_no_shared_dims_stacks():
    """When steps have dimensions but no shared dim columns, stack all rows."""
    step_rows = {
        "a": [{"x": 1, "v": 10}],
        "b": [{"y": 2, "v": 5}],
    }
    step_dims = {"a": ["x"], "b": ["y"]}
    rows, cols, multi, _mode = evaluate_combine_aligned(
        _op("add", _ref("a", "v"), _ref("b", "v")), step_rows, step_dims, "total",
    )
    assert multi
    assert len(rows) == 2
    assert rows[0]["Step"] == "a"
    assert rows[1]["Step"] == "b"
    assert "v" in cols


def test_aligned_eval_stacks_on_non_overlapping_values():
    """When dimension column names match but values don't overlap, stack."""
    step_rows = {
        "uk": [
            {"city_name": "Birmingham", "amount": 31},
            {"city_name": "London", "amount": 31},
            {"city_name": "Manchester", "amount": 31},
        ],
        "germany": [
            {"city_name": "Berlin", "amount": 30},
        ],
    }
    step_dims = {"uk": ["city_name"], "germany": ["city_name"]}
    rows, cols, multi, _mode = evaluate_combine_aligned(
        _op("div", _ref("uk", "amount"), _ref("germany", "amount")),
        step_rows, step_dims, "Amount",
    )
    assert multi
    assert len(rows) == 4
    assert cols == ["Step", "city_name", "amount"]
    assert rows[0]["Step"] == "uk"
    assert rows[0]["city_name"] == "Birmingham"
    assert rows[3]["Step"] == "germany"
    assert rows[3]["city_name"] == "Berlin"


def test_aligned_eval_stacks_multi_dim_non_overlapping():
    """Stacking works with multiple dimensions (city + month)."""
    step_rows = {
        "uk": [
            {"city": "London", "month": 1, "tx": 100},
            {"city": "London", "month": 2, "tx": 110},
        ],
        "de": [
            {"city": "Berlin", "month": 1, "tx": 50},
            {"city": "Berlin", "month": 2, "tx": 60},
        ],
    }
    step_dims = {"uk": ["city", "month"], "de": ["city", "month"]}
    rows, cols, multi, _mode = evaluate_combine_aligned(
        _op("div", _ref("uk", "tx"), _ref("de", "tx")),
        step_rows, step_dims, "Amount",
    )
    assert multi
    assert len(rows) == 4
    assert "Step" in cols
    assert "city" in cols
    assert "month" in cols
    assert rows[0]["Step"] == "uk"
    assert rows[2]["Step"] == "de"


def test_division_by_zero_returns_none():
    """Division by zero should return None, not crash."""
    result = evaluate_combine(_op("div", _c(10), _c(0)), {})
    assert result is None


def test_none_propagates_through_outer_ops():
    """When a sub-expression returns None (e.g. div-by-zero), outer ops
    should propagate None rather than raising TypeError (Bug-238)."""
    ctx = {"a": {"x": 10}, "b": {"y": 0}}
    result = evaluate_combine(
        _op("add", _op("div", _ref("a", "x"), _ref("b", "y")), _c(1)), ctx)
    assert result is None


def test_nested_none_propagation_in_multiply():
    ctx = {"a": {"x": 5}, "b": {"y": 0}}
    result = evaluate_combine(
        _op("mul", _op("div", _ref("a", "x"), _ref("b", "y")), _c(100)), ctx)
    assert result is None


def test_string_values_coerced_to_numbers():
    ctx = {"a": {"revenue": "1234.56"}, "b": {"qty": "10"}}
    result = evaluate_combine(_op("div", _ref("a", "revenue"), _ref("b", "qty")), ctx)
    assert abs(result - 123.456) < 0.001


def test_string_int_coerced_to_int():
    ctx = {"a": {"count": "42"}, "b": {"factor": "2"}}
    result = evaluate_combine(_op("mul", _ref("a", "count"), _ref("b", "factor")), ctx)
    assert result == 84
    assert isinstance(result, int)


def test_single_expression_no_expansion():
    """A single expression produces a single result column."""
    step_rows = {
        "a": [{"month": 1, "x": 10}],
        "b": [{"month": 1, "x": 5}],
    }
    step_dims = {"a": ["month"], "b": ["month"]}
    rows, cols, is_multi, mode = evaluate_combine_aligned(
        _op("div", _ref("a", "x"), _ref("b", "x")), step_rows, step_dims, "ratio",
    )
    assert is_multi
    assert cols == ["month", "ratio"]
    assert rows[0]["ratio"] == 2.0
    assert mode == "evaluated"


# ---------------------------------------------------------------------------
# alignment_mode metadata (Bug-245)
# ---------------------------------------------------------------------------


def test_alignment_mode_evaluated_for_scalar():
    step_rows = {"a": [{"v": 10}], "b": [{"v": 5}]}
    step_dims = {"a": [], "b": []}
    _, _, _, mode = evaluate_combine_aligned(
        _op("add", _ref("a", "v"), _ref("b", "v")), step_rows, step_dims, "total")
    assert mode == "evaluated"


def test_alignment_mode_evaluated_for_aligned_rows():
    step_rows = {
        "a": [{"m": 1, "v": 10}],
        "b": [{"m": 1, "v": 5}],
    }
    step_dims = {"a": ["m"], "b": ["m"]}
    _, _, _, mode = evaluate_combine_aligned(
        _op("add", _ref("a", "v"), _ref("b", "v")), step_rows, step_dims, "total")
    assert mode == "evaluated"


def test_alignment_mode_stacked_no_shared_dims():
    step_rows = {
        "a": [{"x": 1, "v": 10}],
        "b": [{"y": 2, "v": 5}],
    }
    step_dims = {"a": ["x"], "b": ["y"]}
    _, _, _, mode = evaluate_combine_aligned(
        _op("add", _ref("a", "v"), _ref("b", "v")), step_rows, step_dims, "total")
    assert mode == "stacked_no_shared_dims"


def test_alignment_mode_stacked_no_overlap():
    step_rows = {
        "a": [{"m": 1, "v": 10}],
        "b": [{"m": 99, "v": 5}],
    }
    step_dims = {"a": ["m"], "b": ["m"]}
    _, _, _, mode = evaluate_combine_aligned(
        _op("add", _ref("a", "v"), _ref("b", "v")), step_rows, step_dims, "total")
    assert mode == "stacked_no_overlap"


# ---------------------------------------------------------------------------
# F-023-05 — scalar broadcast across a dimensioned step
# ---------------------------------------------------------------------------


def test_scalar_broadcast_share_by_month():
    step_rows = {
        "germany": [
            {"month_no": 1, "amt": 10},
            {"month_no": 2, "amt": 20},
        ],
        "total": [{"amt": 100}],
    }
    step_dims = {"germany": ["month_no"], "total": []}
    rows, cols, is_multi, mode = evaluate_combine_aligned(
        _pct(_ref("germany", "amt"), _ref("total", "amt")),
        step_rows, step_dims, "Share (%)",
    )
    assert mode == "evaluated"
    assert is_multi
    assert cols == ["month_no", "Share (%)"]
    by_month = {r["month_no"]: r["Share (%)"] for r in rows}
    assert by_month == {1: 10.0, 2: 20.0}


def test_scalar_broadcast_multi_row_scalar_falls_back_to_stack():
    step_rows = {
        "germany": [{"month_no": 1, "amt": 10}],
        "total": [{"amt": 100}, {"amt": 200}],
    }
    step_dims = {"germany": ["month_no"], "total": []}
    _, _, _, mode = evaluate_combine_aligned(
        _pct(_ref("germany", "amt"), _ref("total", "amt")), step_rows, step_dims, "Share (%)",
    )
    assert mode == "stacked_no_shared_dims"


# ---------------------------------------------------------------------------
# Bug-1104 — alignment of dimensioned steps with UNEQUAL dimension sets
# ---------------------------------------------------------------------------


def test_unequal_dim_sets_preserve_fine_grain_rows():
    step_rows = {
        "a": [
            {"country": "DE", "month": "Jan", "x": 5},
            {"country": "DE", "month": "Feb", "x": 7},
            {"country": "FR", "month": "Jan", "x": 3},
        ],
        "b": [
            {"country": "DE", "y": 2},
            {"country": "FR", "y": 1},
        ],
    }
    step_dims = {"a": ["country", "month"], "b": ["country"]}
    rows, cols, multi, mode = evaluate_combine_aligned(
        _op("div", _ref("a", "x"), _ref("b", "y")), step_rows, step_dims, "ratio",
    )
    assert mode == "evaluated"
    assert multi
    assert cols == ["country", "month", "ratio"]
    assert len(rows) == 3
    by_key = {(r["country"], r["month"]): r["ratio"] for r in rows}
    assert by_key == {
        ("DE", "Jan"): 2.5,
        ("DE", "Feb"): 3.5,
        ("FR", "Jan"): 3.0,
    }


def test_unequal_dim_sets_coarse_step_first_in_dict():
    step_rows = {
        "total": [
            {"country": "DE", "y": 2},
            {"country": "FR", "y": 1},
        ],
        "detail": [
            {"country": "DE", "month": "Jan", "x": 5},
            {"country": "DE", "month": "Feb", "x": 7},
            {"country": "FR", "month": "Jan", "x": 3},
        ],
    }
    step_dims = {"total": ["country"], "detail": ["country", "month"]}
    rows, cols, multi, mode = evaluate_combine_aligned(
        _op("div", _ref("detail", "x"), _ref("total", "y")), step_rows, step_dims, "ratio",
    )
    assert mode == "evaluated"
    assert len(rows) == 3
    by_key = {(r["country"], r["month"]): r["ratio"] for r in rows}
    assert by_key == {
        ("DE", "Jan"): 2.5,
        ("DE", "Feb"): 3.5,
        ("FR", "Jan"): 3.0,
    }


def test_unequal_dim_sets_inner_join_drops_unmatched_shared_key():
    step_rows = {
        "a": [
            {"country": "DE", "month": "Jan", "x": 5},
            {"country": "DE", "month": "Feb", "x": 7},
            {"country": "ES", "month": "Jan", "x": 9},
        ],
        "b": [
            {"country": "DE", "y": 2},
        ],
    }
    step_dims = {"a": ["country", "month"], "b": ["country"]}
    rows, _cols, _multi, mode = evaluate_combine_aligned(
        _op("div", _ref("a", "x"), _ref("b", "y")), step_rows, step_dims, "ratio",
    )
    assert mode == "evaluated"
    by_key = {(r["country"], r["month"]): r["ratio"] for r in rows}
    assert by_key == {("DE", "Jan"): 2.5, ("DE", "Feb"): 3.5}


def test_two_fine_steps_ambiguous_grain_stacks_visibly():
    step_rows = {
        "a": [
            {"country": "DE", "month": "Jan", "x": 5},
            {"country": "DE", "month": "Feb", "x": 7},
        ],
        "b": [
            {"country": "DE", "week": "W1", "y": 2},
            {"country": "DE", "week": "W2", "y": 3},
        ],
    }
    step_dims = {"a": ["country", "month"], "b": ["country", "week"]}
    rows, _cols, multi, mode = evaluate_combine_aligned(
        _op("div", _ref("a", "x"), _ref("b", "y")), step_rows, step_dims, "ratio",
    )
    assert mode == "stacked_ambiguous_grain"
    assert multi
    assert len(rows) == 4


# ---------------------------------------------------------------------------
# F-023-06 — round(null)/abs(null) return null, not a crashed turn
# ---------------------------------------------------------------------------


def test_round_of_null_returns_none_scalar():
    value = evaluate_combine(
        _pct(_ref("a", "x"), _ref("b", "y")), {"a": {"x": 5}, "b": {"y": 0}},
    )
    assert value is None


def test_function_null_propagation():
    cases = [
        _op("round", _ref("a", "x"), _c(2)),
        _op("abs", _ref("a", "x")),
        _op("min", _ref("a", "x"), _c(1)),
        _op("max", _ref("a", "x"), _c(1)),
    ]
    for expr in cases:
        assert evaluate_combine(expr, {"a": {"x": None}}) is None, expr


def test_round_of_null_in_broadcast_does_not_crash():
    step_rows = {
        "germany": [
            {"month_no": 1, "amt": 10},
            {"month_no": 2, "amt": 5},
        ],
        "total": [{"amt": 0}],
    }
    step_dims = {"germany": ["month_no"], "total": []}
    rows, _cols, _multi, mode = evaluate_combine_aligned(
        _pct(_ref("germany", "amt"), _ref("total", "amt")),
        step_rows, step_dims, "Share (%)",
    )
    assert mode == "evaluated"
    by_month = {r["month_no"]: r["Share (%)"] for r in rows}
    assert by_month == {1: None, 2: None}
