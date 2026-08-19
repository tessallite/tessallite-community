"""Judge-block rendering — Phase D1.

When the judge blocks an answer and judge_mode is ``sync``, the user must not
see the original answer. We mutate the turn to replace ``answer_text`` with a
block message, stash the original under ``llm_plan["original_answer_blocked"]``
for admin trace, and set ``status='judge_blocked'``.

Block decision (Bug-6332 — fail CLOSED):
  - An explicit ``fail`` verdict is always blocked (sync and async).
  - In ``sync`` mode ANY non-releasable verdict is blocked — including
    ``unknown``, which is what the judge returns when it could not run or
    returned malformed output. Sync mode's whole purpose is "no unvetted answer
    reaches the user", so a judge that could not vet the answer must withhold
    it, not release it. Only ``pass``/``warn`` (the judge ran and cleared the
    answer) are released. Async mode delivers before vetting by design, so it
    keeps blocking on ``fail`` only and does not retroactively withhold an
    already-shown answer on a transient judge outage.

Two visibility flavours per ProjectAgentConfig.judge_block_visibility:
  - transparent (default): show the verdict / reason + suggested rewording.
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

# Verdicts that mean the judge ran and cleared the answer for release. Anything
# else — an explicit ``fail``, or a non-vetted ``unknown``/malformed/unexpected
# value — is withheld in sync mode (Bug-6332, fail CLOSED).
_RELEASABLE_VERDICTS = frozenset({"pass", "warn"})


def _should_block(cfg: ProjectAgentConfig, judge: JudgeOutcome) -> bool:
    """True when the answer must be withheld from the user (Bug-6332)."""
    verdict = (judge.verdict or "").lower()
    if verdict == "fail":
        return True
    # Fail CLOSED in sync mode: a verdict the judge could not vet
    # (``unknown``/malformed/unexpected) must not release the answer.
    # F-023-29 / Bug-8148 — the default is validated-first ("sync"); a cfg
    # MISSING the attribute falls closed to sync here. This gate deliberately
    # keys on ``== "sync"`` to match the answer-delivery boundary in
    # conversations.py; diverging here would create an incoherent half-sync
    # state for a present-but-malformed value (narration gated but the full
    # answer still delivered pre-verdict). Uniform malformed-value normalisation
    # at ingress is deferred — see intake
    # 2026-07-22-judge-mode-ingress-validation.md.
    if getattr(cfg, "judge_mode", "sync") == "sync" and verdict not in _RELEASABLE_VERDICTS:
        return True
    return False


def render_block_message(
    cfg: ProjectAgentConfig,
    judge: JudgeOutcome,
) -> str:
    if cfg.judge_block_visibility == "opaque":
        return _OPAQUE_TEXT
    verdict = (judge.verdict or "").lower()
    if verdict == "fail":
        base = f"Answer withheld. The reviewer flagged it as **{verdict}**."
    else:
        # Judge could not vet the answer (verdict unknown / evaluation error).
        base = (
            "Answer withheld. The reviewer could not verify this response, "
            "so it is held back for safety."
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
    """Mutate ``turn`` in-place when the verdict must be blocked. Returns the
    user-visible block message (also assigned to turn.answer_text) when blocked,
    else the unchanged answer text.

    Bug-8290 — sync-mode answer turns are persisted in the non-releasable
    ``judge_pending`` state before the judge runs (conversations.py), so no
    unvetted answer is user-reachable during the judge window. When the judge
    clears the answer (pass/warn -> ``_should_block`` False) this releases a
    still-pending row to ``ok``. The release is scoped to ``judge_pending`` so
    it never disturbs an async ``ok`` row, a ``refused``/``error`` row, or an
    already-``ok`` sync row (idempotent)."""
    if not _should_block(cfg, judge):
        # Release a pending sync turn the judge just cleared. Only a
        # judge_pending row is flipped — every other status is left as-is.
        if getattr(turn, "status", None) == "judge_pending":
            turn.status = "ok"
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
