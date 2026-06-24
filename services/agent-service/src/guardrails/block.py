"""Judge-block rendering — Phase D1.

When the judge LLM returns a ``fail`` verdict and judge_mode is ``sync``,
the user must not see the original answer. We mutate the turn to replace
``answer_text`` with a block message, stash the original under
``llm_plan["original_answer_blocked"]`` for admin trace, and set
``status='judge_blocked'``.

Two visibility flavours per ProjectAgentConfig.judge_block_visibility:
  - transparent (default): show the failed verdict + suggested rewording.
  - opaque: generic "Answer withheld for review" + suggested rewording.
"""
from __future__ import annotations

from typing import Any

from shared.db.models import AgentTurn, ProjectAgentConfig
from src.judge.judge import JudgeOutcome


_OPAQUE_TEXT = (
    "Answer withheld for review. The reviewer flagged this response, so "
    "I cannot share it as written. Try rephrasing the question, naming a "
    "specific metric and a time window, or ask a project admin to review "
    "the rubric."
)


def render_block_message(
    cfg: ProjectAgentConfig,
    judge: JudgeOutcome,
) -> str:
    if cfg.judge_block_visibility == "opaque":
        return _OPAQUE_TEXT
    base = (
        f"Answer withheld. The reviewer flagged it as **{judge.verdict}**."
    )
    base += (
        "\n\nTry rephrasing — name a specific metric, model, and time window."
    )
    return base


def apply_judge_block(
    cfg: ProjectAgentConfig,
    turn: AgentTurn,
    judge: JudgeOutcome,
) -> str:
    """Mutate ``turn`` in-place when the verdict is fail. Returns the
    user-visible block message (also assigned to turn.answer_text)."""
    if judge.verdict != "fail":
        return turn.answer_text or ""

    block_message = render_block_message(cfg, judge)
    plan: dict[str, Any] = dict(turn.llm_plan or {})
    if turn.answer_text:
        plan["original_answer_blocked"] = turn.answer_text
    turn.llm_plan = plan
    turn.answer_text = block_message
    turn.status = "judge_blocked"
    actions = list(turn.guardrail_actions or [])
    actions.append(
        {
            "layer": "judge",
            "action": "block",
            "verdict": judge.verdict,
            "visibility": cfg.judge_block_visibility,
        }
    )
    turn.guardrail_actions = actions
    return block_message
