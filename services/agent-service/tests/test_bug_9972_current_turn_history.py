"""Bug-9972 guards for reserved current-turn prompt history."""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.pipeline import run_turn
from src.prompt.assembler import _conversation_history


def _history_db(turns):
    result = MagicMock()
    result.scalars.return_value.all.return_value = turns
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_Bug_9972_history_excludes_current_placeholder_and_keeps_prior_turn():
    conversation_id = uuid.uuid4()
    previous_id = uuid.uuid4()
    current_id = uuid.uuid4()
    previous = types.SimpleNamespace(
        id=previous_id,
        conversation_id=conversation_id,
        turn_index=0,
        user_message="Show revenue by account type",
        answer_text="The totals are ...",
    )
    current_placeholder = types.SimpleNamespace(
        id=current_id,
        conversation_id=conversation_id,
        turn_index=1,
        user_message="Which account type has processed the most money in 2026?",
        answer_text=None,
    )

    history = await _conversation_history(
        _history_db([previous, current_placeholder]),
        conversation_id,
        exclude_turn_id=current_id,
    )

    assert [turn.id for turn in history] == [previous_id]
    assert history[0].user_message == "Show revenue by account type"


@pytest.mark.asyncio
async def test_Bug_9972_pipeline_passes_reserved_turn_to_assembler():
    model_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    bundle = types.SimpleNamespace(
        system="system",
        user="user",
        narration_system="narration",
        allow_list_model_ids=[model_id],
        model_profiles=[],
        persona_scopes=None,
        prior_questions=[],
        previous_plan=None,
    )
    assembler = AsyncMock(return_value=bundle)
    adapter = MagicMock()
    adapter.complete = AsyncMock(return_value=(
        '{"refuse": {"reason": "out_of_scope", '
        '"message": "This test does not run a query."}}'
    ))
    adapter.last_usage = {"input_tokens": 1, "output_tokens": 1}
    cfg = types.SimpleNamespace(
        project_id=uuid.uuid4(),
        judge_mode="async",
        safety_policy="",
        max_query_complexity=0,
        max_compound_steps=3,
        chart_type_selector="none",
        agent_output_format="plain",
    )
    conversation = types.SimpleNamespace(
        id=uuid.uuid4(), persona_id=None, pinned_model_id=None,
    )

    with patch("src.pipeline.assemble_prompt", assembler), \
         patch("src.pipeline.resolve_agent_llm_failover_configs", AsyncMock(return_value=[
             types.SimpleNamespace(provider="test", model_name="planner", timeout_seconds=30)
         ])), \
         patch("src.pipeline.build_adapter", return_value=adapter), \
         patch("src.pipeline.RetryingAdapter", side_effect=lambda value: value), \
         patch("src.pipeline.check_budget", AsyncMock(return_value=None)):
        outcome = await run_turn(
            db=AsyncMock(),
            cfg=cfg,
            conversation=conversation,
            user_message="This test does not run a query.",
            jwt_token="token",
            turn_id=turn_id,
        )

    assert outcome.status == "refused"
    assert assembler.await_args.kwargs["exclude_turn_id"] == turn_id
