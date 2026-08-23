from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.judge.judge import JudgeOutcome


def test_judge_failure_repair_predicate_allows_narration_failures():
    from src.api.conversations import _judge_failure_is_narration_repairable

    outcome = JudgeOutcome(
        verdict="fail",
        reasoning="The answer omits rows from the data returned.",
        metrics={"Completeness": 0.2, "Factual accuracy": 0.8},
    )

    assert _judge_failure_is_narration_repairable(outcome) is True


def test_judge_failure_repair_predicate_blocks_policy_failures():
    from src.api.conversations import _judge_failure_is_narration_repairable

    outcome = JudgeOutcome(
        verdict="fail",
        reasoning="The answer violates the safety policy.",
        metrics={"Compliance": 0.0, "Factual accuracy": 1.0},
    )

    assert _judge_failure_is_narration_repairable(outcome) is False


@pytest.mark.asyncio
async def test_sync_judge_narration_repair_uses_rows_and_shape_facts_once():
    from src.api.conversations import _repair_narration_after_sync_judge_fail
    from src.pipeline import TurnOutcome

    cfg = types.SimpleNamespace(
        project_id=uuid.uuid4(),
        answer_llm_config_id=uuid.uuid4(),
        judge_llm_config_id=uuid.uuid4(),
    )
    outcome = TurnOutcome(
        answer_text="Only GB is covered.",
        status="ok",
        plan={"tool": "query"},
        semantic_query={
            "shape": {
                "shape": "multi_series_time",
                "output_mode": "chart_table",
                "narration_facts": {
                    "series_coverage": {
                        "GB": {"first_period": "2026-01", "last_period": "2026-02"},
                        "DE": {"first_period": "2026-01", "last_period": "2026-01"},
                    }
                },
            }
        },
        routed_sql=None,
        route="source",
        rows_returned=3,
        guardrail_actions=[],
        result_sample=[
            {"period": "2026-01", "country": "GB", "amount": 10},
            {"period": "2026-02", "country": "GB", "amount": 20},
            {"period": "2026-01", "country": "DE", "amount": 30},
        ],
        result_row_count=3,
    )
    first_judge = JudgeOutcome(
        verdict="fail",
        reasoning="Completeness failure: the answer omits DE.",
        metrics={"Completeness": 0.2, "Compliance": 1.0},
    )
    adapter = MagicMock()
    adapter.complete = AsyncMock(return_value="GB covers January to February; DE covers January.")
    adapter.last_usage = {"input_tokens": 7, "output_tokens": 5}
    llm_config = types.SimpleNamespace(provider="anthropic")
    repaired_judge = JudgeOutcome(
        verdict="pass",
        reasoning="ok",
        metrics={"Completeness": 1.0},
        usage_input_tokens=11,
        usage_output_tokens=3,
        provider="openai",
    )

    with (
        patch("src.api.conversations.resolve_agent_llm_config", AsyncMock(return_value=llm_config)),
        patch("src.api.conversations.build_adapter", return_value=adapter),
        patch("src.api.conversations.apply_output_guardrails", return_value=types.SimpleNamespace(text="GB covers January to February; DE covers January.", actions=[])),
        patch("src.api.conversations.run_judge", AsyncMock(return_value=repaired_judge)),
    ):
        result = await _repair_narration_after_sync_judge_fail(
            db=AsyncMock(),
            cfg=cfg,
            system_prompt="system",
            user_message="show trend by country",
            outcome=outcome,
            judge_outcome=first_judge,
            sample_rows=outcome.result_sample,
            result_row_count=outcome.result_row_count,
            conversation_history=[],
        )

    assert result is not None
    assert "DE covers January" in result.repaired_answer
    assert result.judge_outcome.verdict == "pass"
    assert result.repair_input_tokens == 7
    assert result.repair_output_tokens == 5
    assert result.repair_provider == "anthropic"
    assert result.accepted is True
    assert result.actions[-1]["reason"] == "judge_narration_repair"
    repair_payload = adapter.complete.await_args.args[1]
    assert "series_coverage" in repair_payload
    assert "sample_rows" in repair_payload


@pytest.mark.asyncio
async def test_sync_judge_failed_repair_reports_costs_but_is_not_accepted():
    from src.api.conversations import _repair_narration_after_sync_judge_fail
    from src.pipeline import TurnOutcome

    cfg = types.SimpleNamespace(project_id=uuid.uuid4(), answer_llm_config_id=None)
    outcome = TurnOutcome(
        answer_text="Wrong answer.",
        status="ok",
        plan={"tool": "query"},
        semantic_query={"shape": {"shape": "breakdown", "narration_facts": {"row_count": 2}}},
        routed_sql=None,
        route="source",
        rows_returned=2,
        guardrail_actions=[],
        result_sample=[{"country": "GB", "amount": 10}, {"country": "DE", "amount": 20}],
        result_row_count=2,
    )
    first_judge = JudgeOutcome(
        verdict="fail",
        reasoning="Factual accuracy failure.",
        metrics={"Factual accuracy": 0.1},
    )
    adapter = MagicMock()
    adapter.complete = AsyncMock(return_value="Still wrong.")
    adapter.last_usage = {"input_tokens": 4, "output_tokens": 2}
    repaired_judge = JudgeOutcome(
        verdict="fail",
        reasoning="Still inaccurate.",
        metrics={"Factual accuracy": 0.1},
        usage_input_tokens=5,
        usage_output_tokens=1,
        provider="openai",
    )

    with (
        patch("src.api.conversations.resolve_agent_llm_config", AsyncMock(return_value=types.SimpleNamespace(provider="openai"))),
        patch("src.api.conversations.build_adapter", return_value=adapter),
        patch("src.api.conversations.apply_output_guardrails", return_value=types.SimpleNamespace(text="Still wrong.", actions=[])),
        patch("src.api.conversations.run_judge", AsyncMock(return_value=repaired_judge)),
    ):
        result = await _repair_narration_after_sync_judge_fail(
            db=AsyncMock(),
            cfg=cfg,
            system_prompt="system",
            user_message="question",
            outcome=outcome,
            judge_outcome=first_judge,
            sample_rows=outcome.result_sample,
            result_row_count=outcome.result_row_count,
            conversation_history=[],
        )

    assert result is not None
    assert result.accepted is False
    assert result.repair_input_tokens == 4
    assert result.repair_output_tokens == 2
    assert result.judge_outcome.verdict == "fail"
    assert result.actions == []


@pytest.mark.asyncio
async def test_narration_repair_forwards_date_anchor_to_judge():
    # Integration fix — the narration-repair path re-runs the judge on the
    # repaired answer. It must forward the per-turn DATE ANCHOR so the re-judge
    # anchors relative-date verification on the same current date the planner
    # used (the same gap the four main dispatch paths had).
    #
    # Test escape: repair rebuilt the judge call from system_prompt/sections
    # only and dropped the date anchor. Guard: assert run_judge receives the
    # date_anchor kwarg here. Tier: T1 (producer/consumer contract).
    from src.api.conversations import _repair_narration_after_sync_judge_fail
    from src.pipeline import TurnOutcome

    cfg = types.SimpleNamespace(project_id=uuid.uuid4(), answer_llm_config_id=None)
    outcome = TurnOutcome(
        answer_text="Wrong answer.",
        status="ok",
        plan={"tool": "query"},
        semantic_query={"shape": {"shape": "kpi", "narration_facts": {}}},
        routed_sql=None,
        route="source",
        rows_returned=1,
        guardrail_actions=[],
        result_sample=[{"amount": 10}],
        result_row_count=1,
    )
    first_judge = JudgeOutcome(
        verdict="fail",
        reasoning="Completeness failure.",
        metrics={"Completeness": 0.2},
    )
    adapter = MagicMock()
    adapter.complete = AsyncMock(return_value="Repaired.")
    adapter.last_usage = {"input_tokens": 1, "output_tokens": 1}
    run_judge_mock = AsyncMock(
        return_value=JudgeOutcome(verdict="pass", reasoning="ok", metrics={})
    )
    anchor = "CURRENT_DATE: 2026-07-21 (Tuesday)."

    with (
        patch("src.api.conversations.resolve_agent_llm_config",
              AsyncMock(return_value=types.SimpleNamespace(provider="anthropic"))),
        patch("src.api.conversations.build_adapter", return_value=adapter),
        patch("src.api.conversations.apply_output_guardrails",
              return_value=types.SimpleNamespace(text="Repaired.", actions=[])),
        patch("src.api.conversations.run_judge", run_judge_mock),
    ):
        await _repair_narration_after_sync_judge_fail(
            db=AsyncMock(),
            cfg=cfg,
            system_prompt="system",
            user_message="revenue last month?",
            outcome=outcome,
            judge_outcome=first_judge,
            sample_rows=outcome.result_sample,
            result_row_count=outcome.result_row_count,
            conversation_history=[],
            date_anchor=anchor,
        )

    assert run_judge_mock.await_args.kwargs["date_anchor"] == anchor
