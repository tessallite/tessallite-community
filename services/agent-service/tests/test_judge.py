"""Tests for LLM-as-judge fixes.

Covers:
  - Judge LLM fallback to answer LLM when judge config is None.
  - _parse_judge_json handles clean, fenced, and malformed JSON.
  - run_judge() end-to-end: prompt context, row counts, provider propagation.
  - TurnOutcome.result_sample and result_row_count fields.
"""
from __future__ import annotations

import json
import types
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.judge.judge import _parse_judge_json, JudgeOutcome


# ---------------------------------------------------------------------------
# _parse_judge_json
# ---------------------------------------------------------------------------

class TestParseJudgeJson:

    def test_clean_json(self):
        raw = '{"verdict": "pass", "reasoning": "good", "metrics": {"accuracy": 1.0}}'
        outcome = _parse_judge_json(raw)
        assert outcome.verdict == "pass"
        assert outcome.reasoning == "good"
        assert outcome.metrics == {"accuracy": 1.0}

    def test_strips_markdown_fences(self):
        raw = '```json\n{"verdict": "warn", "reasoning": "ok", "metrics": {}}\n```'
        outcome = _parse_judge_json(raw)
        assert outcome.verdict == "warn"

    def test_extracts_json_from_prose(self):
        raw = 'Here is my verdict:\n{"verdict": "fail", "reasoning": "bad", "metrics": {"q": 0.2}}\nDone.'
        outcome = _parse_judge_json(raw)
        assert outcome.verdict == "fail"
        assert outcome.metrics == {"q": 0.2}

    def test_raises_on_non_json(self):
        with pytest.raises((json.JSONDecodeError, ValueError)):
            _parse_judge_json("I refuse to return JSON")

    def test_unknown_verdict_normalised(self):
        raw = '{"verdict": "maybe", "reasoning": "dunno", "metrics": {}}'
        outcome = _parse_judge_json(raw)
        assert outcome.verdict == "unknown"

    def test_missing_metrics_defaults_to_empty_dict(self):
        raw = '{"verdict": "pass", "reasoning": "fine"}'
        outcome = _parse_judge_json(raw)
        assert outcome.metrics == {}

    def test_non_dict_metrics_defaults_to_empty_dict(self):
        raw = '{"verdict": "pass", "reasoning": "fine", "metrics": [1,2,3]}'
        outcome = _parse_judge_json(raw)
        assert outcome.metrics == {}


# ---------------------------------------------------------------------------
# run_judge() end-to-end with mocked adapter
# ---------------------------------------------------------------------------

class TestRunJudge:

    @pytest.mark.asyncio
    async def test_system_prompt_is_judge_instructions_only(self):
        from unittest.mock import patch as _patch
        from src.judge.judge import run_judge, JUDGE_INSTRUCTIONS

        system_prompt = (
            "## TASK\nYou are an analyst.\n\n"
            "## AVAILABLE MODELS\nModel: modely\n"
            "Measures: base_amount\n"
            "Dimensions: country\n\n"
            "## GROUNDING\nGlossary here\n\n"
            "## CONVERSATION HISTORY\nUser: hello\n"
        )

        captured = {}

        async def _fake_complete(system, user, on_thinking=None):
            captured["system"] = system
            captured["user"] = user
            return '{"verdict": "pass", "reasoning": "ok", "metrics": {"accuracy": 1.0}}'

        mock_adapter = MagicMock()
        mock_adapter.complete = AsyncMock(side_effect=_fake_complete)
        mock_adapter.last_usage = {"input_tokens": 100, "output_tokens": 50}

        llm_config = types.SimpleNamespace(provider="openai", model_name="gpt-4o")
        cfg = types.SimpleNamespace(
            project_id=uuid.uuid4(),
            judge_rubric_id=None,
        )
        db = AsyncMock()

        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]

        with (
            _patch("src.judge.judge.resolve_agent_llm_config",
                   AsyncMock(return_value=llm_config)),
            _patch("src.judge.judge.build_adapter", return_value=mock_adapter),
        ):
            outcome = await run_judge(
                db=db, cfg=cfg,
                system_prompt=system_prompt,
                user_message="Show me revenue",
                plan={"query": {"model_id": "x"}},
                answer_text="Revenue is 100",
                sample_rows=[{"revenue": 100}],
                result_row_count=500,
                conversation_history=history,
            )

        assert outcome.verdict == "pass"
        assert outcome.provider == "openai"
        assert captured["system"] == JUDGE_INSTRUCTIONS
        user_text = captured["user"]
        assert "## 1. RUBRIC" in user_text
        assert "## 2. AGENT INPUT" in user_text
        assert "### A) Current question" in user_text
        assert "Show me revenue" in user_text
        assert "### B) Conversation history" in user_text
        assert "User: hello" in user_text
        assert "Assistant: hi there" in user_text
        assert "### C) Agent system prompt" in user_text
        assert "## AVAILABLE MODELS" in user_text
        assert "## 3. AGENT OUTPUT" in user_text
        assert "Revenue is 100" in user_text

    @pytest.mark.asyncio
    async def test_prompt_displays_true_row_count(self):
        from unittest.mock import patch as _patch
        from src.judge.judge import run_judge

        captured_user = {}

        async def _fake_complete(system, user, on_thinking=None):
            captured_user["value"] = user
            return '{"verdict": "pass", "reasoning": "ok", "metrics": {}}'

        mock_adapter = MagicMock()
        mock_adapter.complete = AsyncMock(side_effect=_fake_complete)
        mock_adapter.last_usage = {}

        llm_config = types.SimpleNamespace(provider="anthropic", model_name="claude")
        cfg = types.SimpleNamespace(
            project_id=uuid.uuid4(),
            judge_rubric_id=None,
        )
        db = AsyncMock()

        with (
            _patch("src.judge.judge.resolve_agent_llm_config",
                   AsyncMock(return_value=llm_config)),
            _patch("src.judge.judge.build_adapter", return_value=mock_adapter),
        ):
            outcome = await run_judge(
                db=db, cfg=cfg,
                system_prompt="## TASK\nTest",
                user_message="question",
                plan=None,
                answer_text="answer",
                sample_rows=[{"a": 1}, {"a": 2}],
                result_row_count=250,
            )

        user_text = captured_user["value"]
        assert "(250 total, showing 2)" in user_text

    @pytest.mark.asyncio
    async def test_compound_scalar_evidence_includes_label_and_expression(self):
        from unittest.mock import patch as _patch
        from src.judge.judge import run_judge

        captured_user = {}

        async def _fake_complete(system, user, on_thinking=None):
            captured_user["value"] = user
            return '{"verdict": "pass", "reasoning": "ok", "metrics": {}}'

        mock_adapter = MagicMock()
        mock_adapter.complete = AsyncMock(side_effect=_fake_complete)
        mock_adapter.last_usage = {}

        llm_config = types.SimpleNamespace(provider="openai", model_name="gpt-4o")
        cfg = types.SimpleNamespace(
            project_id=uuid.uuid4(),
            judge_rubric_id=None,
        )
        db = AsyncMock()

        scalar_evidence = [{
            "label": "Revenue Growth",
            "expression": "B.revenue - A.revenue",
            "value": 12345.67,
        }]

        with (
            _patch("src.judge.judge.resolve_agent_llm_config",
                   AsyncMock(return_value=llm_config)),
            _patch("src.judge.judge.build_adapter", return_value=mock_adapter),
        ):
            await run_judge(
                db=db, cfg=cfg,
                system_prompt="## TASK\nTest",
                user_message="What is revenue growth?",
                plan={"compound_query": {"expression": "B.revenue - A.revenue"}},
                answer_text="Revenue grew by 12345.67",
                sample_rows=scalar_evidence,
                result_row_count=1,
            )

        user_text = captured_user["value"]
        assert "Revenue Growth" in user_text
        assert "B.revenue - A.revenue" in user_text
        assert "12345.67" in user_text
        assert "(1 total, showing 1)" in user_text

    @pytest.mark.asyncio
    async def test_row_count_falls_back_to_sample_length(self):
        from unittest.mock import patch as _patch
        from src.judge.judge import run_judge

        captured_user = {}

        async def _fake_complete(system, user, on_thinking=None):
            captured_user["value"] = user
            return '{"verdict": "pass", "reasoning": "ok", "metrics": {}}'

        mock_adapter = MagicMock()
        mock_adapter.complete = AsyncMock(side_effect=_fake_complete)
        mock_adapter.last_usage = {}

        llm_config = types.SimpleNamespace(provider="openai", model_name="gpt-4o")
        cfg = types.SimpleNamespace(
            project_id=uuid.uuid4(),
            judge_rubric_id=None,
        )
        db = AsyncMock()

        with (
            _patch("src.judge.judge.resolve_agent_llm_config",
                   AsyncMock(return_value=llm_config)),
            _patch("src.judge.judge.build_adapter", return_value=mock_adapter),
        ):
            await run_judge(
                db=db, cfg=cfg,
                system_prompt="## TASK\nTest",
                user_message="q",
                plan=None,
                answer_text="a",
                sample_rows=[{"a": 1}, {"a": 2}, {"a": 3}],
            )

        user_text = captured_user["value"]
        assert "(3 total, showing 3)" in user_text

    @pytest.mark.asyncio
    async def test_provider_set_on_outcome(self):
        from unittest.mock import patch as _patch
        from src.judge.judge import run_judge

        async def _fake_complete(system, user, on_thinking=None):
            return '{"verdict": "warn", "reasoning": "ok", "metrics": {}}'

        mock_adapter = MagicMock()
        mock_adapter.complete = AsyncMock(side_effect=_fake_complete)
        mock_adapter.last_usage = {"input_tokens": 10, "output_tokens": 5}

        llm_config = types.SimpleNamespace(provider="gemini", model_name="gemini-pro")
        cfg = types.SimpleNamespace(
            project_id=uuid.uuid4(),
            judge_rubric_id=None,
        )
        db = AsyncMock()

        with (
            _patch("src.judge.judge.resolve_agent_llm_config",
                   AsyncMock(return_value=llm_config)),
            _patch("src.judge.judge.build_adapter", return_value=mock_adapter),
        ):
            outcome = await run_judge(
                db=db, cfg=cfg,
                system_prompt="## TASK\nTest",
                user_message="q",
                plan=None,
                answer_text="a",
                sample_rows=[],
            )

        assert outcome.provider == "gemini"
        assert outcome.usage_input_tokens == 10
        assert outcome.usage_output_tokens == 5


# ---------------------------------------------------------------------------
# TurnOutcome.result_sample field
# ---------------------------------------------------------------------------

class TestTurnOutcomeResultSample:

    def test_result_sample_field_exists(self):
        from src.pipeline import TurnOutcome
        outcome = TurnOutcome(
            answer_text="test",
            status="ok",
            plan=None,
            semantic_query=None,
            routed_sql=None,
            route=None,
            rows_returned=0,
            guardrail_actions=[],
            result_sample=[{"col": "val"}],
        )
        assert outcome.result_sample == [{"col": "val"}]

    def test_result_sample_defaults_to_none(self):
        from src.pipeline import TurnOutcome
        outcome = TurnOutcome(
            answer_text="test",
            status="ok",
            plan=None,
            semantic_query=None,
            routed_sql=None,
            route=None,
            rows_returned=0,
            guardrail_actions=[],
        )
        assert outcome.result_sample is None


# ---------------------------------------------------------------------------
# Judge LLM config fallback
# ---------------------------------------------------------------------------

class TestJudgeLlmFallback:

    @pytest.mark.asyncio
    async def test_judge_falls_back_to_answer_config(self):
        from unittest.mock import patch as _patch
        from shared.llm.config_resolution import resolve_agent_llm_config

        answer_config_id = uuid.uuid4()
        llm_record = types.SimpleNamespace(
            id=answer_config_id,
            provider="openai",
            model_name="gpt-4o",
            display_name="GPT-4o",
            encrypted_api_key=None,
            base_url=None,
            temperature=None,
            max_tokens=None,
            timeout_seconds=None,
        )

        cfg = types.SimpleNamespace(
            answer_llm_config_id=answer_config_id,
            judge_llm_config_id=None,
        )

        db = AsyncMock()
        cfg_result = MagicMock()
        cfg_result.scalar_one_or_none.return_value = cfg
        db.execute = AsyncMock(return_value=cfg_result)
        db.get = AsyncMock(return_value=llm_record)

        with _patch("shared.llm.adapter.decrypt_api_key", return_value="sk-test"):
            result = await resolve_agent_llm_config(uuid.uuid4(), "judge", db)

        assert result.provider == "openai"
        assert result.model_name == "gpt-4o"
        db.get.assert_called_once()
        call_args = db.get.call_args
        assert call_args[0][1] == answer_config_id


# ---------------------------------------------------------------------------
# Cost provider integration — _judge_turn_background passes real provider
# ---------------------------------------------------------------------------

class TestJudgeCostProviderIntegration:

    @pytest.mark.asyncio
    async def test_sync_judge_passes_provider_to_record_turn_cost(self):
        from unittest.mock import patch as _patch
        from src.judge.judge import JudgeOutcome
        from src.api.conversations import send_message, MessageSend

        project_id = uuid.uuid4()
        conversation_id = uuid.uuid4()
        turn_id = uuid.uuid4()
        judge_llm_config_id = uuid.uuid4()
        answer_llm_config_id = uuid.uuid4()

        judge_outcome = JudgeOutcome(
            verdict="warn",
            reasoning="check values",
            metrics={"accuracy": 0.8},
            usage_input_tokens=200,
            usage_output_tokens=75,
            provider="openai",
        )

        cfg_row = types.SimpleNamespace(
            project_id=project_id,
            judge_llm_config_id=judge_llm_config_id,
            answer_llm_config_id=answer_llm_config_id,
            judge_mode="sync",
            judge_rubric_id=None,
            show_thought_process=False,
            show_physical_query=False,
        )

        turn_obj = types.SimpleNamespace(
            id=turn_id,
            conversation_id=conversation_id,
            turn_index=0,
            user_message="Show revenue",
            answer_text="Revenue is 100",
            status="ok",
            latency_ms=50,
            llm_plan=None,
            thought_summary=None,
            semantic_query=None,
            routed_sql=None,
            route=None,
            citations=None,
            user_feedback=None,
            judge_verdict=None,
            judge_reasoning=None,
            judge_metrics=None,
            guardrail_actions=None,
            usage_input_tokens=80,
            usage_output_tokens=20,
            rendered_output=None,
        )

        conv_obj = types.SimpleNamespace(
            id=conversation_id,
            project_id=project_id,
            persona_id=None,
            last_active_at=None,
        )

        outcome = types.SimpleNamespace(
            status="ok",
            plan={"query": {"model_id": "m1"}},
            answer_text="Revenue is 100",
            result_sample=[{"revenue": 100}],
            result_row_count=1,
            # F-023-04 — the real TurnOutcome carries the answer-LLM provider,
            # which persist_turn now reads to cost the answer turn.
            provider="openai",
        )

        bundle = types.SimpleNamespace(system="## TASK\nTest")

        mock_db = AsyncMock()
        mock_db.get = AsyncMock(return_value=conv_obj)
        mock_db.scalar = AsyncMock(return_value=0)
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        record_cost_mock = AsyncMock()
        background_tasks = MagicMock()
        current_user = types.SimpleNamespace(
            tenant_id="test-tenant",
            user_id=uuid.uuid4(),
            raw_token="fake-jwt",
        )
        async def _gen(*a, **kw):
            yield mock_db

        with (
            _patch("src.api.conversations.get_tenant_db", _gen),
            _patch("src.api.conversations._require_agent_enabled",
                   AsyncMock(return_value=cfg_row)),
            _patch("src.api.conversations.run_turn",
                   AsyncMock(return_value=outcome)),
            _patch("src.api.conversations.persist_turn",
                   AsyncMock(return_value=turn_obj)),
            _patch("src.prompt.assembler.assemble_prompt",
                   AsyncMock(return_value=bundle)),
            _patch("src.api.conversations.run_judge",
                   AsyncMock(return_value=judge_outcome)),
            _patch("src.api.conversations._prior_turns_for_judge",
                   AsyncMock(return_value=[])),
            _patch("src.api.conversations.record_turn_cost", record_cost_mock),
            _patch("src.api.conversations.apply_judge_block"),
            _patch("src.api.conversations.dispatch_event", AsyncMock()),
            _patch("src.api.conversations._redact_trace",
                   lambda t, c: t),
        ):
            await send_message(
                project_id=project_id,
                conversation_id=conversation_id,
                body=MessageSend(text="Show revenue"),
                background_tasks=background_tasks,
                current_user=current_user,
            )

        record_cost_mock.assert_called_once()
        call_kwargs = record_cost_mock.call_args
        assert call_kwargs.kwargs.get("provider") == "openai"
        assert call_kwargs.kwargs.get("input_tokens") == 200
        assert call_kwargs.kwargs.get("output_tokens") == 75

    @pytest.mark.asyncio
    async def test_background_judge_passes_provider_to_record_turn_cost(self):
        from unittest.mock import patch as _patch, call as _call
        from src.judge.judge import JudgeOutcome
        from src.api.conversations import _judge_turn_background

        project_id = uuid.uuid4()
        turn_id = uuid.uuid4()
        judge_llm_config_id = uuid.uuid4()
        answer_llm_config_id = uuid.uuid4()

        judge_outcome = JudgeOutcome(
            verdict="pass",
            reasoning="ok",
            metrics={"accuracy": 1.0},
            usage_input_tokens=150,
            usage_output_tokens=50,
            provider="anthropic",
        )

        cfg_row = types.SimpleNamespace(
            project_id=project_id,
            judge_llm_config_id=judge_llm_config_id,
            answer_llm_config_id=answer_llm_config_id,
            judge_mode="async",
            judge_rubric_id=None,
        )

        turn = types.SimpleNamespace(
            id=turn_id,
            conversation_id=uuid.uuid4(),
            judge_verdict=None,
            judge_reasoning=None,
            judge_metrics=None,
            usage_input_tokens=100,
            usage_output_tokens=30,
            status="ok",
        )

        mock_db = AsyncMock()
        cfg_result = MagicMock()
        cfg_result.scalar_one_or_none.return_value = cfg_row
        mock_db.execute = AsyncMock(return_value=cfg_result)
        mock_db.get = AsyncMock(return_value=turn)
        mock_db.commit = AsyncMock()

        record_cost_mock = AsyncMock()

        async def _gen(*a, **kw):
            yield mock_db

        with (
            _patch("src.api.conversations.get_tenant_db", _gen),
            _patch("src.api.conversations.run_judge",
                   AsyncMock(return_value=judge_outcome)),
            _patch("src.api.conversations._prior_turns_for_judge",
                   AsyncMock(return_value=[])),
            _patch("src.api.conversations.record_turn_cost", record_cost_mock),
            _patch("src.api.conversations.apply_judge_block"),
            _patch("src.api.conversations.dispatch_event",
                   AsyncMock()),
        ):
            await _judge_turn_background(
                tenant_id="test-tenant",
                project_id=project_id,
                turn_id=turn_id,
                conversation_id=turn.conversation_id,
                system_prompt="## TASK\nTest",
                user_message="question",
                plan=None,
                answer_text="answer",
                sample_rows=[{"a": 1}],
                result_row_count=1,
            )

        record_cost_mock.assert_called_once()
        call_kwargs = record_cost_mock.call_args
        assert call_kwargs.kwargs.get("provider") == "anthropic"
        assert call_kwargs.kwargs.get("input_tokens") == 150
        assert call_kwargs.kwargs.get("output_tokens") == 50
