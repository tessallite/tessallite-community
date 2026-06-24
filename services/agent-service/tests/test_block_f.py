"""Tests for Block F — Agent Guardrails and Cost Limits."""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .conftest import (
    TEST_TENANT,
    TEST_USER_ID,
    TEST_PROJECT_ID,
    NOW,
    async_gen_from,
    make_mock_db,
    make_agent_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cfg(**kwargs):
    base = dict(
        id=uuid.uuid4(),
        project_id=TEST_PROJECT_ID,
        enabled=True,
        session_history_depth=20,
        conversation_retention_days=30,
        daily_token_budget=0,
        daily_cost_budget_usd=0.0,
        max_query_complexity=0,
        answer_llm_config_id=None,
    )
    base.update(kwargs)
    return types.SimpleNamespace(**base)


def _make_query_call(measures=("revenue",), dimensions=("month",), where=(), having=(), sort=()):
    return types.SimpleNamespace(
        model_id=str(uuid.uuid4()),
        measures=list(measures),
        dimensions=list(dimensions),
        where=list(where),
        having=list(having),
        sort=list(sort),
    )


# ---------------------------------------------------------------------------
# F.1 — Cost recording helpers
# ---------------------------------------------------------------------------

def test_estimate_cost_usd_anthropic():
    from src.guardrails.budget import estimate_cost_usd
    cost = estimate_cost_usd("anthropic", input_tokens=1000, output_tokens=1000)
    assert cost > 0


def test_estimate_cost_usd_unknown_provider():
    """Bug-1103 — an unmapped provider must NOT silently zero the cost; it is
    charged at the conservative fallback rate so spend is never lost from the
    budget sum."""
    from src.guardrails import budget
    cost = budget.estimate_cost_usd("unknown_provider", input_tokens=1000, output_tokens=1000)
    expected = (
        1000 * budget._FALLBACK_PER_1K["input"]
        + 1000 * budget._FALLBACK_PER_1K["output"]
    ) / 1000.0
    assert cost == expected
    assert cost > 0


@pytest.mark.asyncio
async def test_record_turn_cost_writes_entry():
    from src.guardrails.budget import record_turn_cost

    mock_db = make_mock_db()
    await record_turn_cost(
        db=mock_db,
        project_id=TEST_PROJECT_ID,
        turn_id=uuid.uuid4(),
        llm_config_id=None,
        provider="anthropic",
        input_tokens=500,
        output_tokens=200,
    )
    mock_db.add.assert_called_once()


# ---------------------------------------------------------------------------
# F.2 — Budget check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_budget_no_limits_skips_db():
    from src.guardrails.budget import check_budget
    cfg = _make_cfg(daily_token_budget=0, daily_cost_budget_usd=0.0)
    mock_db = make_mock_db()
    result = await check_budget(mock_db, cfg)
    assert result is None
    mock_db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_check_budget_token_limit_exceeded():
    from src.guardrails.budget import check_budget
    cfg = _make_cfg(daily_token_budget=1000, daily_cost_budget_usd=0.0)
    mock_db = make_mock_db()

    row = MagicMock()
    row.in_tok = 600
    row.out_tok = 500
    row.cost = 0.0
    result_mock = MagicMock()
    result_mock.one.return_value = row
    mock_db.execute = AsyncMock(return_value=result_mock)

    reason = await check_budget(mock_db, cfg)
    assert reason == "daily_token_budget_exceeded"


@pytest.mark.asyncio
async def test_check_budget_within_limits_returns_none():
    from src.guardrails.budget import check_budget
    cfg = _make_cfg(daily_token_budget=10000, daily_cost_budget_usd=5.0)
    mock_db = make_mock_db()

    row = MagicMock()
    row.in_tok = 200
    row.out_tok = 100
    row.cost = 0.5
    result_mock = MagicMock()
    result_mock.one.return_value = row
    mock_db.execute = AsyncMock(return_value=result_mock)

    reason = await check_budget(mock_db, cfg)
    assert reason is None


@pytest.mark.asyncio
async def test_check_budget_cost_limit_exceeded():
    from src.guardrails.budget import check_budget
    cfg = _make_cfg(daily_token_budget=0, daily_cost_budget_usd=1.0)
    mock_db = make_mock_db()

    row = MagicMock()
    row.in_tok = 0
    row.out_tok = 0
    row.cost = 2.5
    result_mock = MagicMock()
    result_mock.one.return_value = row
    mock_db.execute = AsyncMock(return_value=result_mock)

    reason = await check_budget(mock_db, cfg)
    assert reason == "daily_cost_budget_exceeded"


@pytest.mark.asyncio
async def test_check_budget_cost_exactly_at_limit_refuses():
    """Boundary — spend exactly equal to the cost budget must refuse (`>=`),
    not allow one more turn over the line."""
    from src.guardrails.budget import check_budget
    cfg = _make_cfg(daily_token_budget=0, daily_cost_budget_usd=1.0)
    mock_db = make_mock_db()

    row = MagicMock()
    row.in_tok = 0
    row.out_tok = 0
    row.cost = 1.0
    result_mock = MagicMock()
    result_mock.one.return_value = row
    mock_db.execute = AsyncMock(return_value=result_mock)

    reason = await check_budget(mock_db, cfg)
    assert reason == "daily_cost_budget_exceeded"


@pytest.mark.asyncio
async def test_check_budget_token_exactly_at_limit_refuses():
    """Boundary — total tokens exactly equal to the token budget must refuse."""
    from src.guardrails.budget import check_budget
    cfg = _make_cfg(daily_token_budget=1000, daily_cost_budget_usd=0.0)
    mock_db = make_mock_db()

    row = MagicMock()
    row.in_tok = 600
    row.out_tok = 400
    row.cost = 0.0
    result_mock = MagicMock()
    result_mock.one.return_value = row
    mock_db.execute = AsyncMock(return_value=result_mock)

    reason = await check_budget(mock_db, cfg)
    assert reason == "daily_token_budget_exceeded"


@pytest.mark.asyncio
async def test_check_budget_just_under_limit_allows():
    """Boundary — one unit under each limit must still allow the turn."""
    from src.guardrails.budget import check_budget
    cfg = _make_cfg(daily_token_budget=1000, daily_cost_budget_usd=1.0)
    mock_db = make_mock_db()

    row = MagicMock()
    row.in_tok = 500
    row.out_tok = 499
    row.cost = 0.999
    result_mock = MagicMock()
    result_mock.one.return_value = row
    mock_db.execute = AsyncMock(return_value=result_mock)

    reason = await check_budget(mock_db, cfg)
    assert reason is None


# ---------------------------------------------------------------------------
# F.3 — Complexity check
# ---------------------------------------------------------------------------

def test_complexity_check_no_limit():
    from src.guardrails.budget import check_query_complexity
    cfg = _make_cfg(max_query_complexity=0)
    call = _make_query_call(measures=["a", "b"], dimensions=["c", "d", "e"], where=["f"])
    assert check_query_complexity(cfg, call) is None


def test_complexity_check_within_limit():
    from src.guardrails.budget import check_query_complexity
    cfg = _make_cfg(max_query_complexity=10)
    call = _make_query_call(measures=["a"], dimensions=["b"])
    assert check_query_complexity(cfg, call) is None


def test_complexity_check_exceeds_limit():
    from src.guardrails.budget import check_query_complexity
    cfg = _make_cfg(max_query_complexity=3)
    call = _make_query_call(measures=["a", "b"], dimensions=["c", "d"])
    assert check_query_complexity(cfg, call) == "query_too_complex"


# ---------------------------------------------------------------------------
# F.4 — Pipeline budget refusal integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pipeline_refuses_when_budget_exceeded():
    """run_turn returns a 'refused' status when budget check fails."""
    from src.pipeline import run_turn

    cfg = _make_cfg(daily_token_budget=100, daily_cost_budget_usd=0.0)
    conversation = types.SimpleNamespace(id=uuid.uuid4(), project_id=TEST_PROJECT_ID)
    mock_db = AsyncMock()

    row = MagicMock()
    row.in_tok = 200
    row.out_tok = 0
    row.cost = 0.0
    result_mock = MagicMock()
    result_mock.one.return_value = row

    with (
        patch("src.pipeline.scan_input_message") as mock_scan,
        patch("src.pipeline.assemble_prompt") as mock_assemble,
        patch("src.pipeline.resolve_agent_llm_failover_configs", AsyncMock(return_value=[MagicMock()])),
        patch("src.pipeline.build_adapter", return_value=MagicMock()),
        patch("src.pipeline.check_budget", AsyncMock(return_value="daily_token_budget_exceeded")),
    ):
        mock_scan.return_value = MagicMock(ok=True)
        mock_assemble.return_value = MagicMock(
            system="sys", user="usr", narration_system="narr_sys",
            allow_list_model_ids=[uuid.UUID(cfg.project_id.__str__() if hasattr(cfg.project_id, '__str__') else str(TEST_PROJECT_ID))]
        )
        mock_assemble.return_value.allow_list_model_ids = [uuid.uuid4()]

        outcome = await run_turn(
            db=mock_db,
            cfg=cfg,
            conversation=conversation,
            user_message="show me revenue",
            jwt_token="tok",
        )

    assert outcome.status == "refused"
    assert any(
        a.get("reason") == "daily_token_budget_exceeded"
        for a in outcome.guardrail_actions
    )
