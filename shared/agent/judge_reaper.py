"""Judge-pending durability — reap turns stranded by a process death (ws2#27).

A validated-first ("sync") answer turn is committed in the non-releasable
``judge_pending`` state BEFORE the judge runs (Bug-8290): the answer is durable
but withheld on every read path until the judge flips it to ``ok`` (released) or
``judge_blocked`` (withheld). That is the correct fail-closed shape — but if the
process dies AFTER the ``judge_pending`` commit and BEFORE the verdict commit
(SIGKILL, OOM, container restart, task cancellation), the turn is STRANDED:
it stays ``judge_pending`` forever, so the user's answer is never released and
never explicitly refused — it silently hangs.

This module resolves such strandings the same way a non-vettable verdict does in
sync mode (``guardrails/block.apply_judge_block`` for an ``unknown`` verdict):
fail CLOSED. A stranded turn older than ``JUDGE_PENDING_MAX_AGE_MINUTES`` is
flipped to ``judge_blocked`` with the original (unvetted) answer stashed under
``llm_plan["original_answer_blocked"]`` for the admin trace and ``answer_text``
replaced by a withheld message. The unvetted answer is NEVER released — the
recovery preserves the sync-mode guarantee while unsticking the turn.

Placement (USER-DECIDED, Option A): this logic lives in the SHARED package and
is invoked from the scheduler's existing per-tenant agent-retention sweep
(``scheduler/src/jobs/sweep.py``). Agent-service is NOT given its own scheduler,
and the scheduler does NOT import agent-service ``src`` — the transition below is
re-derived from the shared ``AgentTurn`` model only, so it runs in the scheduler
process without any cross-service dependency.

The operation is idempotent (only ``judge_pending`` rows are touched; a second
pass reaps nothing) and does not commit — the caller owns the transaction,
mirroring ``shared/agent/retention.py``.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import AgentTurn

logger = logging.getLogger(__name__)

# A live sync-judge turn resolves in seconds (one extra LLM call). This bound
# only has to exceed the slowest legitimate judge window so a still-running
# verdict is never reaped, while a crashed turn is unstuck the same hour rather
# than hanging forever. Overridable via JUDGE_PENDING_MAX_AGE_MINUTES.
_DEFAULT_JUDGE_PENDING_MAX_AGE_MINUTES = 30

# User-facing withheld text for a reaped turn. Mirrors the opaque judge-block
# wording (guardrails/block._OPAQUE_TEXT) — kept as a module constant here so the
# shared reaper has no dependency on agent-service ``src``.
_REAPED_WITHHELD_TEXT = (
    "Answer withheld for review. This response could not be verified before "
    "the review completed, so it is held back for safety. Please ask the "
    "question again."
)


def _judge_pending_max_age_minutes() -> int:
    """Age past which a ``judge_pending`` turn is presumed stranded.

    Read per call so a deployment can retune it without a code change; a
    non-numeric or non-positive value falls back to the default rather than
    reaping every live turn (a 0 or negative cutoff would classify an
    in-flight sync-judge turn as stranded and withhold a legitimate answer).
    """
    raw = os.environ.get("JUDGE_PENDING_MAX_AGE_MINUTES")
    if raw is None:
        return _DEFAULT_JUDGE_PENDING_MAX_AGE_MINUTES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "JUDGE_PENDING_MAX_AGE_MINUTES=%r is not an integer — using the "
            "default of %d minutes",
            raw, _DEFAULT_JUDGE_PENDING_MAX_AGE_MINUTES,
        )
        return _DEFAULT_JUDGE_PENDING_MAX_AGE_MINUTES
    if value <= 0:
        logger.warning(
            "JUDGE_PENDING_MAX_AGE_MINUTES=%d is not positive — using the "
            "default of %d minutes",
            value, _DEFAULT_JUDGE_PENDING_MAX_AGE_MINUTES,
        )
        return _DEFAULT_JUDGE_PENDING_MAX_AGE_MINUTES
    return value


def _fail_close_stranded_turn(turn: AgentTurn) -> None:
    """Flip one stranded ``judge_pending`` turn to withheld ``judge_blocked``.

    Mirrors ``guardrails/block.apply_judge_block`` for a non-vettable verdict:
    the unvetted answer is stashed for the admin trace and replaced with a
    withheld message; the row is marked ``judge_blocked`` so every read path
    withholds it exactly as a judge-blocked answer.
    """
    plan: dict[str, Any] = dict(turn.llm_plan or {})
    if turn.answer_text:
        plan["original_answer_blocked"] = turn.answer_text
    plan["judge_pending_reaped"] = True
    turn.llm_plan = plan
    turn.answer_text = _REAPED_WITHHELD_TEXT
    turn.status = "judge_blocked"
    turn.judge_verdict = "unknown"
    turn.judge_reasoning = (
        "Turn was stranded in judge_pending (the judging process died before a "
        "verdict landed) and reaped fail-closed by the retention sweep; the "
        "unvetted answer was withheld."
    )
    actions = list(turn.guardrail_actions or [])
    actions.append(
        {
            "layer": "judge",
            "action": "reap_stranded_pending",
            "verdict": "unknown",
        }
    )
    turn.guardrail_actions = actions


async def reap_stranded_judge_pending(
    db: AsyncSession,
    tenant_slug: str = "",
) -> int:
    """Fail-close every ``judge_pending`` turn stranded beyond the max age.

    Selects ``AgentTurn`` rows whose ``status == 'judge_pending'`` and whose
    ``created_at`` predates the ``JUDGE_PENDING_MAX_AGE_MINUTES`` cutoff, then
    applies the withheld ``judge_blocked`` transition to each. Returns the count
    reaped. Does not commit — the caller owns the transaction.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=_judge_pending_max_age_minutes()
    )
    result = await db.execute(
        select(AgentTurn).where(
            AgentTurn.status == "judge_pending",
            AgentTurn.created_at < cutoff,
        )
    )
    stranded = list(result.scalars().all())
    if not stranded:
        return 0

    for turn in stranded:
        _fail_close_stranded_turn(turn)

    logger.warning(
        "Reaped %d stranded judge_pending turn(s) tenant=%s (fail-closed to "
        "judge_blocked; unvetted answers withheld)",
        len(stranded), tenant_slug,
    )
    return len(stranded)
