"""F-023-01 — judge blocking must be enforced end-to-end.

Business outcome under test: an answer the rubric judge blocked must
never leave the agent-service unredacted — not in the sync REST
response, not over SSE, and not via the trace payload the SPA reads.
Blocked turns must also withhold judge reasoning.
"""
from __future__ import annotations

import json
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.guardrails.block import apply_judge_block, _OPAQUE_TEXT
from src.judge.judge import JudgeOutcome
from src.sse.events import EventPublisher, _END_SENTINEL


SECRET_ANSWER = "Secret revenue for Acme is 4.2M and the CFO is resigning."
JUDGE_REASON = "Answer discloses confidential personnel information."


def _cfg(visibility: str = "transparent", judge_mode: str = "sync"):
    return types.SimpleNamespace(
        project_id=uuid.uuid4(),
        enabled=True,
        show_thought_process=True,
        show_semantic_query=True,
        show_physical_query=True,
        judge_block_visibility=visibility,
        judge_mode=judge_mode,
        judge_llm_config_id=None,
        answer_llm_config_id=None,
    )


def _ok_turn():
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        turn_index=0,
        user_message="What is revenue?",
        answer_text=SECRET_ANSWER,
        status="ok",
        latency_ms=100,
        llm_plan={"tool": "query"},
        thought_summary="thinking",
        semantic_query={"model_id": "m", "shape": {"shape": "kpi", "chart_type": "kpi"}},
        routed_sql="SELECT 1",
        route="source",
        citations=[{"id": "c1"}],
        user_feedback=None,
        judge_verdict=None,
        judge_reasoning=None,
        judge_metrics=None,
        guardrail_actions=[],
        usage_input_tokens=10,
        usage_output_tokens=10,
        rendered_output="<table><tr><td>4.2M</td></tr></table>",
        chart_type="kpi",
        calculation_steps=[{"step": 1, "description": "sum"}],
        query_result_sample=[{"revenue": 4200000}],
    )


def _blocked_turn(cfg) -> types.SimpleNamespace:
    """Produce a blocked turn through the real apply_judge_block path."""
    turn = _ok_turn()
    judge = JudgeOutcome(verdict="fail", reasoning=JUDGE_REASON, metrics={"accuracy": 0.1})
    turn.judge_verdict = judge.verdict
    turn.judge_reasoning = judge.reasoning
    turn.judge_metrics = judge.metrics
    apply_judge_block(cfg, turn, judge)
    return turn


# ---------------------------------------------------------------------------
# _redact_trace — the single serialisation gate for REST + turn list
# ---------------------------------------------------------------------------


class TestRedactTrace:
    def test_blocked_turn_payload_contains_no_original_answer(self):
        from src.api.conversations import _redact_trace

        cfg = _cfg("transparent")
        turn = _blocked_turn(cfg)
        # storage keeps the original for the role-gated admin surfaces
        assert turn.llm_plan["original_answer_blocked"] == SECRET_ANSWER

        payload = _redact_trace(turn, cfg).model_dump_json()
        assert SECRET_ANSWER not in payload
        assert "original_answer_blocked" not in payload

    def test_blocked_turn_shows_block_message_not_answer(self):
        from src.api.conversations import _redact_trace

        cfg = _cfg("transparent")
        turn = _blocked_turn(cfg)
        resp = _redact_trace(turn, cfg)
        assert resp.status == "judge_blocked"
        assert resp.answer_text != SECRET_ANSWER
        assert "withheld" in (resp.answer_text or "").lower()

    def test_blocked_turn_withholds_answer_artifacts(self):
        """Chart HTML, calculation steps, sample rows and citations are
        all derived from the blocked answer and must not be served."""
        from src.api.conversations import _redact_trace

        cfg = _cfg("transparent")
        resp = _redact_trace(_blocked_turn(cfg), cfg)
        assert resp.rendered_output is None
        assert resp.chart_type is None
        assert resp.calculation_steps is None
        assert resp.query_result_sample is None
        assert resp.citations is None

    def test_transparent_mode_strips_judge_reasoning_for_blocked_turns(self):
        from src.api.conversations import _redact_trace

        cfg = _cfg("transparent")
        resp = _redact_trace(_blocked_turn(cfg), cfg)
        assert resp.judge_reasoning is None
        payload = resp.model_dump_json()
        assert JUDGE_REASON not in payload
        assert SECRET_ANSWER not in payload

    def test_opaque_mode_strips_judge_reasoning_and_uses_generic_text(self):
        from src.api.conversations import _redact_trace

        cfg = _cfg("opaque")
        turn = _blocked_turn(cfg)
        resp = _redact_trace(turn, cfg)
        assert resp.judge_reasoning is None
        assert resp.answer_text == _OPAQUE_TEXT
        payload = resp.model_dump_json()
        assert JUDGE_REASON not in payload
        assert SECRET_ANSWER not in payload

    def test_ok_turn_passes_through_unchanged(self):
        from src.api.conversations import _redact_trace

        cfg = _cfg("transparent")
        turn = _ok_turn()
        resp = _redact_trace(turn, cfg)
        assert resp.answer_text == SECRET_ANSWER
        assert resp.rendered_output == turn.rendered_output
        assert resp.chart_type == "kpi"
        assert resp.query_result_sample == turn.query_result_sample
        assert resp.citations == turn.citations
        assert resp.semantic_query["shape"]["shape"] == "kpi"

    def test_blocked_turn_withholds_shape_semantic_trace(self):
        from src.api.conversations import _redact_trace

        cfg = _cfg("transparent")
        resp = _redact_trace(_blocked_turn(cfg), cfg)
        assert resp.semantic_query is None


# ---------------------------------------------------------------------------
# SSE streaming path — sync judge mode
# ---------------------------------------------------------------------------


async def _drain_events(publisher: EventPublisher) -> list[dict]:
    events: list[dict] = []
    while True:
        item = await publisher._queue.get()
        if item is _END_SENTINEL:
            return events
        events.append({"event": item.name, "data": item.data})


@pytest.mark.asyncio
async def test_sse_sync_blocked_answer_never_reaches_the_stream():
    """Business outcome: with judge_mode=sync, a rubric-blocked answer
    reaches the SSE client redacted — no event in the whole stream may
    contain the original answer, and the final answer_text is the block
    message."""
    from src.api.conversations import _run_turn_into_publisher

    cfg = _cfg("transparent", judge_mode="sync")
    project_id = cfg.project_id
    conv = types.SimpleNamespace(
        id=uuid.uuid4(), project_id=project_id, persona_id=None,
        caller_ref="u", title="t", last_active_at=None,
    )
    turn = _ok_turn()
    turn.conversation_id = conv.id

    outcome = types.SimpleNamespace(
        status="ok",
        plan={"tool": "query"},
        answer_text=SECRET_ANSWER,
        result_sample=[{"revenue": 4200000}],
        result_row_count=1,
        provider="anthropic",
        calculation_steps=None,
    )
    judge_outcome = JudgeOutcome(
        verdict="fail", reasoning=JUDGE_REASON, metrics={"accuracy": 0.1},
    )

    db = AsyncMock()
    cfg_result = MagicMock()
    cfg_result.scalar_one_or_none.return_value = cfg
    db.execute = AsyncMock(return_value=cfg_result)
    db.get = AsyncMock(return_value=conv)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    async def _gen(*a, **kw):
        yield db

    publisher = EventPublisher()
    bundle = types.SimpleNamespace(
        system="sys", system_sections=[("## PROJECT CONTEXT", "sys")],
        grounding_matches="",
    )

    with (
        patch("src.api.conversations.get_tenant_db", _gen),
        patch("src.api.conversations.run_turn", AsyncMock(return_value=outcome)),
        patch("src.api.conversations.persist_turn", AsyncMock(return_value=turn)),
        patch("src.prompt.assembler.assemble_prompt", AsyncMock(return_value=bundle)),
        patch("src.api.conversations.run_judge", AsyncMock(return_value=judge_outcome)),
        patch("src.api.conversations._prior_turns_for_judge", AsyncMock(return_value=[])),
        patch("src.api.conversations.record_turn_cost", AsyncMock()),
        patch("src.api.conversations.dispatch_event", AsyncMock()),
    ):
        await _run_turn_into_publisher(
            tenant_id="t",
            project_id=project_id,
            conversation_id=conv.id,
            turn_index=0,
            user_message="What is revenue?",
            jwt_token="jwt",
            started=0.0,
            publisher=publisher,
        )

    events = await _drain_events(publisher)
    stream_text = json.dumps(events, default=str)

    assert SECRET_ANSWER not in stream_text, "blocked answer leaked over SSE"
    assert "original_answer_blocked" not in stream_text

    completed = [e for e in events if e["event"] == "turn.completed"]
    assert len(completed) == 1
    data = completed[0]["data"]
    assert data["status"] == "judge_blocked"
    assert "withheld" in (data["answer_text"] or "").lower()
    assert data["rendered_output"] is None
    assert data["result_sample"] is None
    assert data["citations"] is None

    judged = [e for e in events if e["event"] == "turn.judged"]
    assert len(judged) == 1
    assert judged[0]["data"]["verdict"] == "fail"
    assert judged[0]["data"]["reasoning"] is None
    assert JUDGE_REASON not in stream_text


@pytest.mark.asyncio
async def test_sse_sync_opaque_strips_reasoning_from_judged_event():
    from src.api.conversations import _run_turn_into_publisher

    cfg = _cfg("opaque", judge_mode="sync")
    conv = types.SimpleNamespace(
        id=uuid.uuid4(), project_id=cfg.project_id, persona_id=None,
        caller_ref="u", title="t", last_active_at=None,
    )
    turn = _ok_turn()
    turn.conversation_id = conv.id
    outcome = types.SimpleNamespace(
        status="ok", plan={"tool": "query"}, answer_text=SECRET_ANSWER,
        result_sample=None, result_row_count=1, provider="anthropic",
        calculation_steps=None,
    )
    judge_outcome = JudgeOutcome(verdict="fail", reasoning=JUDGE_REASON, metrics={})

    db = AsyncMock()
    cfg_result = MagicMock()
    cfg_result.scalar_one_or_none.return_value = cfg
    db.execute = AsyncMock(return_value=cfg_result)
    db.get = AsyncMock(return_value=conv)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    async def _gen(*a, **kw):
        yield db

    publisher = EventPublisher()
    with (
        patch("src.api.conversations.get_tenant_db", _gen),
        patch("src.api.conversations.run_turn", AsyncMock(return_value=outcome)),
        patch("src.api.conversations.persist_turn", AsyncMock(return_value=turn)),
        patch("src.prompt.assembler.assemble_prompt",
              AsyncMock(return_value=types.SimpleNamespace(
                  system="sys",
                  system_sections=[("## PROJECT CONTEXT", "sys")],
                  grounding_matches="",
              ))),
        patch("src.api.conversations.run_judge", AsyncMock(return_value=judge_outcome)),
        patch("src.api.conversations._prior_turns_for_judge", AsyncMock(return_value=[])),
        patch("src.api.conversations.record_turn_cost", AsyncMock()),
        patch("src.api.conversations.dispatch_event", AsyncMock()),
    ):
        await _run_turn_into_publisher(
            tenant_id="t",
            project_id=cfg.project_id,
            conversation_id=conv.id,
            turn_index=0,
            user_message="What is revenue?",
            jwt_token="jwt",
            started=0.0,
            publisher=publisher,
        )

    events = await _drain_events(publisher)
    stream_text = json.dumps(events, default=str)
    assert SECRET_ANSWER not in stream_text
    assert JUDGE_REASON not in stream_text, "opaque mode leaked judge reasoning"

    judged = [e for e in events if e["event"] == "turn.judged"]
    assert len(judged) == 1
    assert judged[0]["data"]["reasoning"] is None
    completed = [e for e in events if e["event"] == "turn.completed"]
    assert completed[0]["data"]["answer_text"] == _OPAQUE_TEXT


# ---------------------------------------------------------------------------
# Pipeline narration buffering — sync judge mode (no pre-verdict tokens)
# ---------------------------------------------------------------------------


def _query_tool_json(model_id: str) -> str:
    return json.dumps({"query": {"model_id": model_id, "measures": ["revenue"], "dimensions": []}})


def _execution_stub():
    return types.SimpleNamespace(
        sql="SELECT 1",
        columns=["revenue"],
        rows=[{"revenue": 100}],
        rows_returned=1,
        route_type="source",
        routed_sql="SELECT 1",
        aggregate_id=None,
        pocket_id=None,
        execution_ms=5,
    )


# Sentinel: pass to _run_turn_with_mode to build a cfg with NO judge_mode
# attribute at all, exercising the run_turn missing-attribute fallback path.
_ABSENT = object()


async def _run_turn_with_mode(judge_mode):
    """Drive the real run_turn single-query branch with a live publisher
    and return (emitted_events, narrate_stream_mock, narrate_mock).

    Pass ``_ABSENT`` to omit the ``judge_mode`` attribute entirely (tests the
    run_turn fail-closed fallback); pass a string to set it explicitly."""
    from src.pipeline import run_turn

    model_uuid = uuid.uuid4()
    cfg = _cfg("transparent")
    if judge_mode is _ABSENT:
        del cfg.judge_mode
    else:
        cfg.judge_mode = judge_mode
    cfg.chart_type_selector = "none"
    cfg.agent_output_format = "plain"
    cfg.max_query_complexity = 0
    conv = types.SimpleNamespace(id=uuid.uuid4(), persona_id=None)
    bundle = types.SimpleNamespace(
        system="sys", user="user", narration_system="narr",
        allow_list_model_ids={model_uuid}, prior_questions=[],
        persona_scopes=None,
    )
    llm_cfg = types.SimpleNamespace(
        provider="anthropic", model_name="m", timeout_seconds=30,
        display_name="cfg",
    )
    adapter = MagicMock()
    adapter.complete = AsyncMock(return_value=_query_tool_json(str(model_uuid)))
    adapter.last_usage = {"input_tokens": 1, "output_tokens": 1}

    narrate_stream = AsyncMock(return_value=SECRET_ANSWER)
    narrate = AsyncMock(return_value=SECRET_ANSWER)

    db = AsyncMock()
    db.get = AsyncMock(return_value=types.SimpleNamespace(display_name="Model"))

    publisher = EventPublisher()
    with (
        patch("src.pipeline.scan_input_message",
              return_value=types.SimpleNamespace(ok=True, reason=None, matched_topic=None)),
        patch("src.pipeline.assemble_prompt", AsyncMock(return_value=bundle)),
        patch("src.pipeline.resolve_agent_llm_failover_configs",
              AsyncMock(return_value=[llm_cfg])),
        patch("src.pipeline.check_budget", AsyncMock(return_value=None)),
        patch("src.pipeline.build_adapter", return_value=adapter),
        patch("src.pipeline.RetryingAdapter", side_effect=lambda a: a),
        patch("src.pipeline.execute_query", AsyncMock(return_value=_execution_stub())),
        patch("src.pipeline._load_measure_formats", AsyncMock(return_value={})),
        patch("src.pipeline.narrate_answer_stream", narrate_stream),
        patch("src.pipeline.narrate_answer", narrate),
        patch("src.pipeline.apply_output_guardrails",
              side_effect=lambda c, text: types.SimpleNamespace(text=text, actions=[])),
        patch("src.pipeline.build_citations", AsyncMock(return_value=[])),
    ):
        outcome = await run_turn(
            db=db,
            cfg=cfg,
            conversation=conv,
            user_message="What is revenue?",
            jwt_token="jwt",
            publisher=publisher,
        )
    await publisher.close()
    events = await _drain_events(publisher)
    return events, narrate_stream, narrate, outcome


@pytest.mark.asyncio
async def test_sync_mode_buffers_narration_no_tokens_before_verdict():
    events, narrate_stream, narrate, outcome = await _run_turn_with_mode("sync")
    narration_events = [e for e in events if e["event"] == "narration.delta"]
    assert narration_events == [], "sync judge mode streamed answer tokens pre-verdict"
    narrate_stream.assert_not_called()
    narrate.assert_called_once()
    assert outcome.answer_text == SECRET_ANSWER  # buffered, not lost


@pytest.mark.asyncio
async def test_async_mode_still_streams_narration_tokens():
    events, narrate_stream, narrate, outcome = await _run_turn_with_mode("async")
    narrate_stream.assert_called_once()
    narrate.assert_not_called()
    assert outcome.answer_text == SECRET_ANSWER


# ---------------------------------------------------------------------------
# F-023-29 / Bug-8148 — the DEFAULT judge mode is validated-first ("sync"):
# an unvalidated answer must never be shown by default, and an explicit
# "async" override must still stream pre-verdict.
# Test escape: producer defaults + all missing-attribute fallbacks were "async",
# so a new/attribute-less config exposed the answer before validation.
# Guard: exact producer-default contract (ORM default + server_default), and all
# THREE runtime fallbacks (_narration_publisher, run_turn _SyncVerdictGate wrap,
# block._should_block) pinned individually to fall closed to "sync" when the
# attribute is MISSING; each guard is proven to fail on a revert. (A present-but-
# malformed stored value is normalised uniformly at ingress — intake
# 2026-07-22-judge-mode-ingress-validation.md — not at each read gate, so the
# gates key on == "sync" to stay coherent with the conversations.py delivery
# boundary rather than diverging into a half-sync state.)
# Tier: T1 (producer/consumer contract + security fail-closed).
# ---------------------------------------------------------------------------


def test_orm_default_judge_mode_is_validated_first():
    """Producer contract: a ProjectAgentConfig with no explicit judge_mode
    resolves to validated-first ("sync"), so the answer is validated before
    it is shown."""
    from shared.db.models import ProjectAgentConfig

    col = ProjectAgentConfig.__table__.c.judge_mode
    # Exact-match both defaults. NOTE: a substring check ("sync" in ...) is
    # VACUOUS here because "async" contains "sync" — it would pass for a revert
    # to 'async'. The server_default governs every row inserted outside the ORM
    # (raw SQL, migration autogen comparison), so it must be pinned exactly.
    assert col.default.arg == "sync"
    assert str(col.server_default.arg).strip("'\"") == "sync"


def test_pydantic_upsert_default_judge_mode_is_validated_first():
    """Producer contract: creating an agent config without naming a mode
    yields validated-first ("sync")."""
    from src.api.agent_config import AgentConfigUpsert

    assert AgentConfigUpsert().judge_mode == "sync"


def test_narration_publisher_defaults_to_buffered_when_mode_absent():
    """A config object missing judge_mode entirely must fall CLOSED to
    validated-first: narration is buffered (no live publisher passthrough)."""
    from src.pipeline import _narration_publisher

    cfg_no_mode = types.SimpleNamespace()  # no judge_mode attribute at all
    publisher = EventPublisher()
    assert _narration_publisher(cfg_no_mode, publisher) is None


def test_narration_publisher_streams_only_on_explicit_async():
    """The explicit async override still streams pre-verdict."""
    from src.pipeline import _narration_publisher

    publisher = EventPublisher()
    assert _narration_publisher(_cfg(judge_mode="async"), publisher) is publisher
    assert _narration_publisher(_cfg(judge_mode="sync"), publisher) is None


def test_should_block_fails_closed_when_mode_absent():
    """block._should_block must withhold a non-vetted verdict for a config
    missing judge_mode (default validated-first), not release it."""
    from src.guardrails.block import _should_block

    cfg_no_mode = types.SimpleNamespace(judge_block_visibility="transparent")
    unknown = JudgeOutcome(
        verdict="unknown", reasoning="judge could not run", metrics={},
    )
    assert _should_block(cfg_no_mode, unknown) is True


@pytest.mark.asyncio
async def test_default_mode_buffers_narration_no_tokens_before_verdict():
    """End-to-end: a turn driven with NO judge_mode attribute (the run_turn
    fail-closed fallback path) buffers narration exactly as explicit sync does
    — no answer token leaves before the verdict."""
    events, narrate_stream, narrate, outcome = await _run_turn_with_mode(_ABSENT)
    narration_events = [e for e in events if e["event"] == "narration.delta"]
    assert narration_events == []
    narrate_stream.assert_not_called()
    narrate.assert_called_once()


@pytest.mark.asyncio
async def test_run_turn_wraps_publisher_in_sync_gate_when_mode_absent():
    """Pins the run_turn (pipeline.py:~1349) fail-closed fallback SPECIFICALLY:
    a cfg missing judge_mode must still wrap the live publisher in the
    _SyncVerdictGate pre-verdict allowlist. This gate protects the event types
    _narration_publisher does not (thought.delta, compound first_row, combine
    value), so it needs its own guard. Reverting the run_turn fallback to
    "async" (or the gate condition to == "sync" with an "async" getattr
    default) leaves an absent-attribute cfg UNWRAPPED and fails this test."""
    import src.pipeline as pipeline

    wrapped = {"count": 0}
    real_gate = pipeline._SyncVerdictGate

    class _SpyGate(real_gate):  # type: ignore[misc,valid-type]
        def __init__(self, inner):
            wrapped["count"] += 1
            super().__init__(inner)

    with patch.object(pipeline, "_SyncVerdictGate", _SpyGate):
        await _run_turn_with_mode(_ABSENT)
    assert wrapped["count"] == 1, (
        "run_turn did not wrap the publisher in _SyncVerdictGate for a cfg "
        "missing judge_mode — the pre-verdict gate fallback is not fail-closed"
    )


@pytest.mark.asyncio
async def test_run_turn_does_not_wrap_publisher_on_explicit_async():
    """Complements the above: explicit async must NOT wrap the publisher, so
    the spy proves the two branches are genuinely distinguished (not a
    both-always-wrap false pass)."""
    import src.pipeline as pipeline

    wrapped = {"count": 0}
    real_gate = pipeline._SyncVerdictGate

    class _SpyGate(real_gate):  # type: ignore[misc,valid-type]
        def __init__(self, inner):
            wrapped["count"] += 1
            super().__init__(inner)

    with patch.object(pipeline, "_SyncVerdictGate", _SpyGate):
        await _run_turn_with_mode("async")
    assert wrapped["count"] == 0


# ---------------------------------------------------------------------------
# Round 2 — the sync pre-verdict gate is a fail-closed allowlist
# (F-023-03 reopened: compound/recipe structured values + thought.delta)
# ---------------------------------------------------------------------------


class TestSyncVerdictGate:
    @pytest.mark.asyncio
    async def test_unknown_events_are_dropped_fail_closed(self):
        """The round-1 lesson: enumerations miss branches. Any event not
        explicitly allowlisted must be withheld pre-verdict."""
        from src.pipeline import _SyncVerdictGate

        inner = EventPublisher()
        gate = _SyncVerdictGate(inner)
        await gate.emit("narration.delta", text=SECRET_ANSWER)
        await gate.emit("thought.delta", text="thinking about " + SECRET_ANSWER)
        await gate.emit("some.future.event", value=SECRET_ANSWER)
        await gate.close()
        events = await _drain_events(inner)
        assert events == []

    @pytest.mark.asyncio
    async def test_value_bearing_fields_are_stripped(self):
        from src.pipeline import _SyncVerdictGate

        inner = EventPublisher()
        gate = _SyncVerdictGate(inner)
        await gate.emit(
            "compound.step", step_name="a", model_id="m",
            rows_returned=1, first_row={"revenue": 4200000},
        )
        await gate.emit(
            "recipe.step", step_name="b", model_id="m",
            rows_returned=2, first_row={"qty": 7},
        )
        await gate.emit(
            "compound.expression", expression="a.revenue / b.qty",
            label="ratio", value=600000.0,
        )
        await gate.close()
        events = await _drain_events(inner)
        by_name = {e["event"]: e["data"] for e in events}
        assert by_name["compound.step"] == {
            "step_name": "a", "model_id": "m", "rows_returned": 1,
        }
        assert by_name["recipe.step"] == {
            "step_name": "b", "model_id": "m", "rows_returned": 2,
        }
        assert by_name["compound.expression"] == {
            "expression": "a.revenue / b.qty", "label": "ratio",
        }
        assert "4200000" not in json.dumps(events, default=str)
        assert "600000" not in json.dumps(events, default=str)


def _compound_tool_json(model_id: str) -> str:
    return json.dumps({"compound_query": {
        "steps": [
            {"name": "a", "model_id": model_id, "measures": ["revenue"],
             "dimensions": [], "where": [], "having": [], "sort": [], "limit": 100},
            {"name": "b", "model_id": model_id, "measures": ["revenue"],
             "dimensions": [], "where": [], "having": [], "sort": [], "limit": 100},
        ],
        "expression": {"op": "div", "args": [
            {"ref": {"step": "a", "measure": "revenue"}},
            {"ref": {"step": "b", "measure": "revenue"}}]},
        "result_label": "ratio",
    }})


SECRET_ROW_VALUE = 4200000


def _secret_execution():
    return types.SimpleNamespace(
        sql="SELECT 1",
        columns=["revenue"],
        rows=[{"revenue": SECRET_ROW_VALUE}],
        rows_returned=1,
        route_type="source",
        routed_sql="SELECT 1",
        aggregate_id=None,
        pocket_id=None,
        execution_ms=5,
    )


async def _run_compound_turn(judge_mode: str):
    """Drive the REAL run_turn compound branch (real parse, real
    expression validation, real combine evaluation, real event emission)
    and return the full captured event stream."""
    from src.pipeline import run_turn

    model_uuid = uuid.uuid4()
    cfg = _cfg("transparent", judge_mode=judge_mode)
    cfg.chart_type_selector = "none"
    cfg.agent_output_format = "plain"
    cfg.max_query_complexity = 0
    cfg.max_compound_steps = 3
    conv = types.SimpleNamespace(id=uuid.uuid4(), persona_id=None)
    bundle = types.SimpleNamespace(
        system="sys", user="user", narration_system="narr",
        allow_list_model_ids={model_uuid}, prior_questions=[],
        persona_scopes=None,
    )
    llm_cfg = types.SimpleNamespace(
        provider="anthropic", model_name="m", timeout_seconds=30,
        display_name="cfg",
    )
    adapter = MagicMock()
    adapter.complete = AsyncMock(return_value=_compound_tool_json(str(model_uuid)))
    adapter.last_usage = {"input_tokens": 1, "output_tokens": 1}

    narrate_compound = AsyncMock(return_value=SECRET_ANSWER)
    narrate_compound_stream = AsyncMock(return_value=SECRET_ANSWER)

    db = AsyncMock()
    publisher = EventPublisher()
    with (
        patch("src.pipeline.scan_input_message",
              return_value=types.SimpleNamespace(ok=True, reason=None, matched_topic=None)),
        patch("src.pipeline.assemble_prompt", AsyncMock(return_value=bundle)),
        patch("src.pipeline.resolve_agent_llm_failover_configs",
              AsyncMock(return_value=[llm_cfg])),
        patch("src.pipeline.check_budget", AsyncMock(return_value=None)),
        patch("src.pipeline.build_adapter", return_value=adapter),
        patch("src.pipeline.RetryingAdapter", side_effect=lambda a: a),
        patch("src.pipeline.execute_query", AsyncMock(return_value=_secret_execution())),
        patch("src.pipeline.narrate_compound_answer", narrate_compound),
        patch("src.pipeline.narrate_compound_answer_stream", narrate_compound_stream),
        patch("src.pipeline.apply_output_guardrails",
              side_effect=lambda c, text: types.SimpleNamespace(text=text, actions=[])),
    ):
        outcome = await run_turn(
            db=db, cfg=cfg, conversation=conv,
            user_message="ratio of revenue?",
            jwt_token="jwt", publisher=publisher,
        )
    await publisher.close()
    events = await _drain_events(publisher)
    return events, outcome, narrate_compound, narrate_compound_stream


@pytest.mark.asyncio
async def test_sync_compound_stream_carries_no_answer_derived_values():
    """Business outcome: in sync judge mode a compound turn's SSE stream
    contains NO step row values, NO computed combine value, and no
    thought/narration tokens before the verdict — asserted over the full
    captured event stream, not a per-event enumeration."""
    events, outcome, narrate_compound, narrate_compound_stream = (
        await _run_compound_turn("sync")
    )
    stream_text = json.dumps(events, default=str)

    assert str(SECRET_ROW_VALUE) not in stream_text, "step row value leaked"
    assert SECRET_ANSWER not in stream_text
    names = [e["event"] for e in events]
    assert "narration.delta" not in names
    assert "thought.delta" not in names
    for e in events:
        assert "first_row" not in e["data"], f"{e['event']} leaked first_row"
        if e["event"] == "compound.expression":
            assert "value" not in e["data"], "combine value leaked pre-verdict"
    # phase telemetry still flows (counts/labels only)
    assert "compound.step" in names
    assert "compound.expression" in names
    # buffered, not lost: the computed result is still in the outcome
    assert outcome.status == "ok"
    assert outcome.answer_text == SECRET_ANSWER
    narrate_compound.assert_called_once()
    narrate_compound_stream.assert_not_called()


@pytest.mark.asyncio
async def test_async_compound_stream_still_carries_values():
    """Async mode regression: the structured phase payloads (first_row,
    combine value) must keep streaming — async displays pre-verdict by
    design (accepted-risk posture)."""
    events, outcome, narrate_compound, narrate_compound_stream = (
        await _run_compound_turn("async")
    )
    steps = [e for e in events if e["event"] == "compound.step"]
    assert steps and all(
        e["data"]["first_row"] == {"revenue": SECRET_ROW_VALUE} for e in steps
    )
    expr = [e for e in events if e["event"] == "compound.expression"]
    assert len(expr) == 1
    assert expr[0]["data"]["value"] == 1.0  # 4200000 / 4200000
    narrate_compound_stream.assert_called_once()
    narrate_compound.assert_not_called()


@pytest.mark.asyncio
async def test_sync_recipe_stream_carries_no_step_row_values():
    """Drives the REAL execute_recipe emission path through run_turn:
    recipe.step must reach the wire without first_row in sync mode."""
    from src.pipeline import run_turn

    model_uuid = uuid.uuid4()
    recipe_uuid = uuid.uuid4()
    cfg = _cfg("transparent", judge_mode="sync")
    cfg.chart_type_selector = "none"
    cfg.agent_output_format = "plain"
    cfg.max_query_complexity = 0
    conv = types.SimpleNamespace(id=uuid.uuid4(), persona_id=None)
    bundle = types.SimpleNamespace(
        system="sys", user="user", narration_system="narr",
        allow_list_model_ids={model_uuid}, prior_questions=[],
        persona_scopes=None,
    )
    llm_cfg = types.SimpleNamespace(
        provider="anthropic", model_name="m", timeout_seconds=30,
        display_name="cfg",
    )
    adapter = MagicMock()
    adapter.complete = AsyncMock(return_value=json.dumps(
        {"run_recipe": {"recipe_id": str(recipe_uuid), "parameters": {}}}
    ))
    adapter.last_usage = {"input_tokens": 1, "output_tokens": 1}

    recipe = types.SimpleNamespace(
        id=recipe_uuid,
        project_id=cfg.project_id,
        name="secret recipe",
        steps=[{"name": "s1", "model_id": str(model_uuid), "measures": ["revenue"]}],
        combine=None,
        parameters=[],
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=recipe)

    publisher = EventPublisher()
    with (
        patch("src.pipeline.scan_input_message",
              return_value=types.SimpleNamespace(ok=True, reason=None, matched_topic=None)),
        patch("src.pipeline.assemble_prompt", AsyncMock(return_value=bundle)),
        patch("src.pipeline.resolve_agent_llm_failover_configs",
              AsyncMock(return_value=[llm_cfg])),
        patch("src.pipeline.check_budget", AsyncMock(return_value=None)),
        patch("src.pipeline.build_adapter", return_value=adapter),
        patch("src.pipeline.RetryingAdapter", side_effect=lambda a: a),
        patch("src.exec.recipe.execute_query",
              AsyncMock(return_value=_secret_execution())),
        patch("src.pipeline.apply_output_guardrails",
              side_effect=lambda c, text: types.SimpleNamespace(text=text, actions=[])),
    ):
        outcome = await run_turn(
            db=db, cfg=cfg, conversation=conv,
            user_message="run the recipe",
            jwt_token="jwt", publisher=publisher,
        )
    await publisher.close()
    events = await _drain_events(publisher)
    stream_text = json.dumps(events, default=str)

    assert outcome.status == "ok"
    assert str(SECRET_ROW_VALUE) not in stream_text, "recipe step row leaked"
    names = [e["event"] for e in events]
    assert "narration.delta" not in names
    steps = [e for e in events if e["event"] == "recipe.step"]
    assert len(steps) == 1
    assert steps[0]["data"] == {
        "step_name": "s1", "model_id": str(model_uuid), "rows_returned": 1,
    }


# ---------------------------------------------------------------------------
# Round 2 — webhook redaction (F-023-01 reopened: pre-verdict webhook)
# ---------------------------------------------------------------------------


async def _run_stream_with_webhooks(visibility: str, verdict: str):
    """Drive _run_turn_into_publisher with a sync judge and capture every
    dispatch_event call (the outbound webhook boundary)."""
    from src.api.conversations import _run_turn_into_publisher

    cfg = _cfg(visibility, judge_mode="sync")
    conv = types.SimpleNamespace(
        id=uuid.uuid4(), project_id=cfg.project_id, persona_id=None,
        caller_ref="u", title="t", last_active_at=None,
    )
    turn = _ok_turn()
    turn.conversation_id = conv.id
    outcome = types.SimpleNamespace(
        status="ok", plan={"tool": "query"}, answer_text=SECRET_ANSWER,
        result_sample=[{"revenue": 4200000}], result_row_count=1,
        provider="anthropic", calculation_steps=None,
    )
    judge_outcome = JudgeOutcome(
        verdict=verdict, reasoning=JUDGE_REASON, metrics={"accuracy": 0.1},
    )

    db = AsyncMock()
    cfg_result = MagicMock()
    cfg_result.scalar_one_or_none.return_value = cfg
    db.execute = AsyncMock(return_value=cfg_result)
    db.get = AsyncMock(return_value=conv)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    async def _gen(*a, **kw):
        yield db

    dispatch = AsyncMock()
    publisher = EventPublisher()
    with (
        patch("src.api.conversations.get_tenant_db", _gen),
        patch("src.api.conversations.run_turn", AsyncMock(return_value=outcome)),
        patch("src.api.conversations.persist_turn", AsyncMock(return_value=turn)),
        patch("src.prompt.assembler.assemble_prompt",
              AsyncMock(return_value=types.SimpleNamespace(
                  system="sys",
                  system_sections=[("## PROJECT CONTEXT", "sys")],
                  grounding_matches="",
              ))),
        patch("src.api.conversations.run_judge", AsyncMock(return_value=judge_outcome)),
        patch("src.api.conversations._prior_turns_for_judge", AsyncMock(return_value=[])),
        patch("src.api.conversations.record_turn_cost", AsyncMock()),
        patch("src.api.conversations.dispatch_event", dispatch),
    ):
        await _run_turn_into_publisher(
            tenant_id="t",
            project_id=cfg.project_id,
            conversation_id=conv.id,
            turn_index=0,
            user_message="What is revenue?",
            jwt_token="jwt",
            started=0.0,
            publisher=publisher,
        )
    await _drain_events(publisher)
    return dispatch


def _webhook_payloads(dispatch) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for c in dispatch.call_args_list:
        out[c.kwargs["event_type"]] = c.kwargs["payload"]
    return out


@pytest.mark.asyncio
async def test_sync_blocked_turn_webhook_is_redacted_and_post_verdict():
    """Business outcome: the turn.completed webhook for a sync-blocked
    turn must match what a user surface would see — block message,
    judge_blocked status, no original answer, no citations."""
    dispatch = await _run_stream_with_webhooks("transparent", verdict="fail")
    payloads = _webhook_payloads(dispatch)

    assert "turn.completed" in payloads
    completed = payloads["turn.completed"]
    assert completed["status"] == "judge_blocked", (
        "webhook fired before apply_judge_block"
    )
    assert "withheld" in (completed["answer_text"] or "").lower()
    assert completed["citations"] is None
    assert SECRET_ANSWER not in json.dumps(
        {k: v for k, v in payloads.items()}, default=str
    )
    assert "turn.judge_blocked" in payloads
    assert payloads["turn.judge_blocked"]["reasoning"] is None
    assert JUDGE_REASON not in json.dumps(payloads, default=str)


@pytest.mark.asyncio
async def test_sync_pass_turn_webhook_keeps_the_answer():
    """No over-redaction: a pass verdict still ships the real answer."""
    dispatch = await _run_stream_with_webhooks("transparent", verdict="pass")
    payloads = _webhook_payloads(dispatch)
    assert payloads["turn.completed"]["answer_text"] == SECRET_ANSWER
    assert payloads["turn.completed"]["status"] == "ok"
    assert "turn.judge_blocked" not in payloads


@pytest.mark.asyncio
async def test_opaque_mode_strips_reasoning_from_judge_webhook():
    dispatch = await _run_stream_with_webhooks("opaque", verdict="fail")
    payloads = _webhook_payloads(dispatch)
    assert payloads["turn.judge_blocked"]["reasoning"] is None
    assert JUDGE_REASON not in json.dumps(payloads, default=str)
    assert payloads["turn.completed"]["answer_text"] == _OPAQUE_TEXT
