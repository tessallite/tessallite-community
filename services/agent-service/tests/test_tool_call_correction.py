"""Tests for the LLM tool-call correction mechanism."""
from __future__ import annotations

import json
import types
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.pipeline import _attempt_tool_call_correction
from src.tools.spec import (
    CompoundQueryToolCall,
    ToolCallParseError,
    parse_tool_call,
)
from src.recipes.eval import validate_expression


CONV_ID = uuid.uuid4()


def _mock_adapter(corrected_response: str | Exception) -> MagicMock:
    adapter = MagicMock()
    if isinstance(corrected_response, Exception):
        adapter.complete = AsyncMock(side_effect=corrected_response)
    else:
        adapter.complete = AsyncMock(return_value=corrected_response)
    adapter.last_usage = {"input_tokens": 50, "output_tokens": 30}
    return adapter


def _mock_db(turns: list[tuple[str, str | None]] | None = None) -> AsyncMock:
    """Mock db that returns fake conversation turns."""
    db = AsyncMock()
    rows = []
    if turns:
        for user_msg, answer in turns:
            rows.append(types.SimpleNamespace(
                user_message=user_msg,
                answer_text=answer,
            ))
    result_mock = MagicMock()
    result_mock.all.return_value = rows
    db.execute = AsyncMock(return_value=result_mock)
    return db


# ---------------------------------------------------------------------------
# _attempt_tool_call_correction
# ---------------------------------------------------------------------------


def test_parse_tool_call_wraps_malformed_embedded_json_as_parse_error():
    raw = (
        'The plan is {"query":{"model_id":"abc","measures":["revenue"],'
        '"dimensions":[],"where":[],"having":[],"sort":[],}}'
    )

    with pytest.raises(ToolCallParseError, match="malformed JSON object"):
        parse_tool_call(raw)


@pytest.mark.asyncio
async def test_correction_succeeds_with_valid_json():
    good_json = json.dumps({"query": {
        "model_id": "abc", "measures": ["revenue"],
        "dimensions": [], "where": [], "having": [], "sort": [],
    }})
    adapter = _mock_adapter(good_json)
    db = _mock_db([("What is total revenue?", "Revenue is $100")])
    usage = {"input": 0, "output": 0}

    result = await _attempt_tool_call_correction(
        adapter, "broken {json", "LLM did not return JSON",
        db, CONV_ID, usage,
    )
    assert result == good_json
    adapter.complete.assert_awaited_once()
    system, user = adapter.complete.call_args.args
    assert "REQUIRED SCHEMA" in system
    assert "broken {json" in user
    assert "What is total revenue?" in user


@pytest.mark.asyncio
async def test_correction_returns_none_on_llm_failure():
    adapter = _mock_adapter(RuntimeError("LLM down"))
    db = _mock_db()
    usage = {"input": 0, "output": 0}

    result = await _attempt_tool_call_correction(
        adapter, "bad", "parse error", db, CONV_ID, usage,
    )
    assert result is None


@pytest.mark.asyncio
async def test_correction_accumulates_usage():
    adapter = _mock_adapter('{"query": {"model_id":"x","measures":["m"]}}')
    db = _mock_db()
    usage = {"input": 100, "output": 50}

    await _attempt_tool_call_correction(
        adapter, "bad", "error", db, CONV_ID, usage,
    )
    assert usage["input"] == 150
    assert usage["output"] == 80


@pytest.mark.asyncio
async def test_correction_includes_recent_turns():
    turns = [
        ("first question", "first answer"),
        ("second question", "second answer"),
    ]
    adapter = _mock_adapter('{"query": {"model_id":"x","measures":["m"]}}')
    db = _mock_db(turns)
    usage = {"input": 0, "output": 0}

    await _attempt_tool_call_correction(
        adapter, "bad", "error", db, CONV_ID, usage,
    )
    _, user_prompt = adapter.complete.call_args.args
    assert "first question" in user_prompt
    assert "second question" in user_prompt


@pytest.mark.asyncio
async def test_correction_context_window_is_configurable(monkeypatch):
    """Bug-7353 — the correction context window must be env-tunable (default 4,
    up from the previous hard-coded 2) so a follow-up correction can carry more
    than two recent turns. The SQL LIMIT applied to the history fetch must equal
    the configured window."""
    import importlib
    import src.pipeline as pipeline

    monkeypatch.setenv("AGENT_TOOL_CORRECTION_CONTEXT_TURNS", "6")
    importlib.reload(pipeline)
    try:
        assert pipeline._TOOL_CORRECTION_CONTEXT_TURNS == 6

        captured: dict[str, int] = {}

        class _CapDB:
            async def execute(self, stmt):
                # Pull the LIMIT off the compiled statement so we assert the
                # window is actually threaded into the query, not just stored.
                captured["limit"] = stmt._limit
                result_mock = MagicMock()
                result_mock.all.return_value = []
                return result_mock

        adapter = _mock_adapter('{"query": {"model_id":"x","measures":["m"]}}')
        usage = {"input": 0, "output": 0}
        await pipeline._attempt_tool_call_correction(
            adapter, "bad", "error", _CapDB(), CONV_ID, usage,
        )
        assert captured["limit"] == 6
    finally:
        monkeypatch.delenv("AGENT_TOOL_CORRECTION_CONTEXT_TURNS", raising=False)
        importlib.reload(pipeline)


def test_tuning_knob_defaults_and_invalid_env(monkeypatch):
    """Bug-7353 / Bug-7354 — the narration + correction tuning knobs default to
    their historical values and reject invalid/zero env overrides rather than
    silently zeroing a limit."""
    import importlib
    import src.pipeline as pipeline

    # Defaults with no env set.
    for var in (
        "AGENT_MAX_NARRATE_ROWS",
        "AGENT_RESULT_SAMPLE_CAP",
        "AGENT_TOOL_CORRECTION_CONTEXT_TURNS",
    ):
        monkeypatch.delenv(var, raising=False)
    importlib.reload(pipeline)
    assert pipeline._MAX_NARRATE_ROWS == 25
    assert pipeline._RESULT_SAMPLE_CAP == 50
    assert pipeline._TOOL_CORRECTION_CONTEXT_TURNS == 4

    # Valid override raises the narration sample size.
    monkeypatch.setenv("AGENT_MAX_NARRATE_ROWS", "100")
    importlib.reload(pipeline)
    assert pipeline._MAX_NARRATE_ROWS == 100

    # Invalid / below-minimum overrides fall back to the default.
    for bad in ("0", "-3", "notanint", "", "  "):
        monkeypatch.setenv("AGENT_MAX_NARRATE_ROWS", bad)
        importlib.reload(pipeline)
        assert pipeline._MAX_NARRATE_ROWS == 25, f"bad={bad!r} should fall back"

    monkeypatch.delenv("AGENT_MAX_NARRATE_ROWS", raising=False)
    importlib.reload(pipeline)


@pytest.mark.asyncio
async def test_correction_no_history_shows_first_turn():
    adapter = _mock_adapter('{"query": {"model_id":"x","measures":["m"]}}')
    db = _mock_db([])
    usage = {"input": 0, "output": 0}

    await _attempt_tool_call_correction(
        adapter, "bad", "error", db, CONV_ID, usage,
    )
    _, user_prompt = adapter.complete.call_args.args
    assert "(first turn)" in user_prompt


@pytest.mark.asyncio
async def test_correction_truncates_long_failing_output():
    long_output = "x" * 5000
    adapter = _mock_adapter('{"query": {"model_id":"x","measures":["m"]}}')
    db = _mock_db()
    usage = {"input": 0, "output": 0}

    await _attempt_tool_call_correction(
        adapter, long_output, "error", db, CONV_ID, usage,
    )
    _, user_prompt = adapter.complete.call_args.args
    assert len(user_prompt) < 5000


# ---------------------------------------------------------------------------
# End-to-end: correction fixes parse error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_correction_fixes_bad_json_to_valid_tool_call():
    corrected = json.dumps({"query": {
        "model_id": "m1", "measures": ["revenue"],
        "dimensions": ["country"], "where": [], "having": [], "sort": [],
    }})
    adapter = _mock_adapter(corrected)
    db = _mock_db()
    usage = {"input": 0, "output": 0}

    raw = await _attempt_tool_call_correction(
        adapter, "not json at all!", "LLM did not return JSON",
        db, CONV_ID, usage,
    )
    assert raw is not None
    call = parse_tool_call(raw)
    assert call.model_id == "m1"
    assert call.measures == ["revenue"]


# ---------------------------------------------------------------------------
# Compound expression correction scenario
# ---------------------------------------------------------------------------

def _step(name, measures):
    return types.SimpleNamespace(name=name, measures=measures)


def _ref(s, m):
    return {"ref": {"step": s, "measure": m}}


def _op(name, *args):
    return {"op": name, "args": list(args)}


def test_unknown_step_in_tree_flagged():
    """validate_expression catches bad step refs inside the expression tree."""
    steps = [_step("a", ["v"])]
    errors = validate_expression(_op("div", _ref("a", "v"), _ref("z", "v")), steps)
    assert len(errors) == 1
    assert "z" in errors[0]


@pytest.mark.asyncio
async def test_compound_expression_correction_flow():
    """Simulates the compound expression correction: bad expression tree in
    (references an unknown step), corrected tool call out."""
    good_expr = _op("mul", _op("div", _ref("a", "v"), _ref("b", "v")),
                    {"const": 100})
    corrected_tool_call = json.dumps({"compound_query": {
        "steps": [
            {"name": "a", "model_id": "m1", "measures": ["v"],
             "dimensions": [], "where": [], "having": [], "sort": []},
            {"name": "b", "model_id": "m1", "measures": ["v"],
             "dimensions": [], "where": [], "having": [], "sort": []},
        ],
        "expression": good_expr,
        "result_label": "Ratio",
    }})

    adapter = _mock_adapter(corrected_tool_call)
    db = _mock_db([("compare a vs b", None)])
    usage = {"input": 0, "output": 0}

    failing_body = {
        "steps": [
            {"name": "a", "model_id": "m1", "measures": ["v"],
             "dimensions": [], "where": [], "having": [], "sort": [], "limit": 100},
            {"name": "b", "model_id": "m1", "measures": ["v"],
             "dimensions": [], "where": [], "having": [], "sort": [], "limit": 100},
        ],
        "expression": _op("div", _ref("ghost", "v"), _ref("b", "v")),
        "result_label": "Ratio",
    }
    failing_json = json.dumps({"compound_query": failing_body})

    raw = await _attempt_tool_call_correction(
        adapter, failing_json,
        "Step 'ghost' not found.",
        db, CONV_ID, usage,
    )
    assert raw is not None
    new_call = parse_tool_call(raw)
    assert isinstance(new_call, CompoundQueryToolCall)
    assert new_call.expression == good_expr
    new_errors = validate_expression(new_call.expression, new_call.steps)
    assert new_errors == []


def test_compound_correction_andgate_rejects_invented_structured_field():
    # Bug-5349 Phase 2/3 (Codex-R3 / Round-4) — the compound expression-correction
    # retry accepts a corrected call ONLY when BOTH validate_expression AND
    # validate_tool_call_against_bundle are clean. This pins the AND-gate: a
    # correction that fixes the combine tree but smuggles an INVENTED field into
    # a structured WHERE must still be rejected by the bundle validator.
    from src.planning.validation import validate_tool_call_against_bundle
    model_id = uuid.uuid4()
    bundle = types.SimpleNamespace(
        allow_list_model_ids=[model_id],
        model_profiles=[types.SimpleNamespace(
            id=model_id,
            measure_names=["v"], dimension_names=["country"],
            filterable_where_names=["country"],
            sortable_names=["country", "v"],
        )],
        persona_scopes=None,
    )
    good_expr = _op("div", _ref("a", "v"), _ref("b", "v"))
    corrected = json.dumps({"compound_query": {
        "steps": [
            {"name": "a", "model_id": str(model_id), "measures": ["v"],
             "dimensions": ["country"],
             # combine tree is fine, but this base field is invented:
             "where": [{"left": {"fn": "lower", "args": [{"field": "ghost"}]},
                        "op": "eq", "right": {"literal": "x"}}],
             "having": [], "sort": []},
            {"name": "b", "model_id": str(model_id), "measures": ["v"],
             "dimensions": ["country"], "where": [], "having": [], "sort": []},
        ],
        "expression": good_expr,
        "result_label": "Ratio",
    }})
    new_call = parse_tool_call(corrected)
    # First gate (combine-tree) is clean...
    assert validate_expression(new_call.expression, new_call.steps) == []
    # ...but the second gate (bundle validator) catches the invented field, so
    # the AND-condition rejects the correction.
    bundle_issues = validate_tool_call_against_bundle(new_call, bundle)
    assert any(i.field_name == "ghost" for i in bundle_issues)
