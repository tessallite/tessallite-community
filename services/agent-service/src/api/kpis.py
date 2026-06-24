"""Agent KPI dashboard surface — Phase C3 + D1.4 + D2.4.

Returns aggregate metrics over a rolling 30-day window for a project's
agent. Mirrors §10 of the conversational-agent plan:
  - citations_rate    — fraction of OK turns with ≥1 citation
  - aggregate_route_rate — fraction of OK turns routed via aggregate
  - judge_block_rate  — fraction of answered turns with status='judge_blocked'
  - feedback_up/down  — feedback vote counts
  - dlq_depth         — open webhook DLQ rows

Phase D adds two read-only views consumed by the Metrics tab:
  - GET /calibration  — recent judged turns for side-by-side review
  - GET /cost         — per-day token + $ estimate; per-provider $ split
                        read from the cost ledger's provider column (F-023-28)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import and_, desc, func, select

from shared.db.models import (
    AgentConversation,
    AgentCostEntry,
    AgentTurn,
    AgentWebhookDlq,
)
from shared.db.session import get_tenant_db
from src.api.agent_config import _require_blocked_original_access
from src.auth.middleware import CurrentUser, forbid_embed_user

router = APIRouter(prefix="/projects/{project_id}/agent", tags=["agent-kpis"])


class AgentKpis(BaseModel):
    window_days: int
    total_turns: int
    ok_turns: int
    refused_turns: int
    citations_rate: float
    aggregate_route_rate: float
    judge_block_rate: float
    feedback_up: int
    feedback_down: int
    dlq_depth: int


@router.get("/kpis", response_model=AgentKpis)
async def get_kpis(
    project_id: UUID,
    window_days: int = 30,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> AgentKpis:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    async for db in get_tenant_db(current_user.tenant_id):
        # Restrict turns to this project via conversation join.
        base_q = (
            select(AgentTurn)
            .join(
                AgentConversation,
                AgentTurn.conversation_id == AgentConversation.id,
            )
            .where(
                AgentConversation.project_id == project_id,
                AgentTurn.created_at >= cutoff,
            )
        )

        total_q = await db.execute(
            select(func.count()).select_from(base_q.subquery())
        )
        total = int(total_q.scalar() or 0)

        ok_q = await db.execute(
            select(func.count()).select_from(
                base_q.where(AgentTurn.status == "ok").subquery()
            )
        )
        ok = int(ok_q.scalar() or 0)

        refused_q = await db.execute(
            select(func.count()).select_from(
                base_q.where(AgentTurn.status == "refused").subquery()
            )
        )
        refused = int(refused_q.scalar() or 0)

        citations_q = await db.execute(
            select(func.count()).select_from(
                base_q.where(
                    AgentTurn.status == "ok",
                    AgentTurn.citations.isnot(None),
                    func.jsonb_array_length(AgentTurn.citations) > 0,
                ).subquery()
            )
        )
        cites = int(citations_q.scalar() or 0)

        agg_q = await db.execute(
            select(func.count()).select_from(
                base_q.where(
                    AgentTurn.status == "ok",
                    AgentTurn.route == "aggregate",
                ).subquery()
            )
        )
        agg = int(agg_q.scalar() or 0)

        judge_block_q = await db.execute(
            select(func.count()).select_from(
                base_q.where(
                    AgentTurn.status == "judge_blocked",
                ).subquery()
            )
        )
        judge_blocks = int(judge_block_q.scalar() or 0)

        # Feedback counts via JSONB ->> 'vote'.
        feedback_q = await db.execute(
            select(
                AgentTurn.user_feedback["vote"].astext.label("v"),
                func.count(),
            )
            .select_from(
                AgentTurn.__table__.join(
                    AgentConversation.__table__,
                    AgentTurn.conversation_id == AgentConversation.id,
                )
            )
            .where(
                and_(
                    AgentConversation.project_id == project_id,
                    AgentTurn.created_at >= cutoff,
                    AgentTurn.user_feedback.isnot(None),
                )
            )
            .group_by("v")
        )
        feedback_up = 0
        feedback_down = 0
        for vote, count in feedback_q.all():
            if vote == "up":
                feedback_up = int(count)
            elif vote == "down":
                feedback_down = int(count)

        dlq_q = await db.execute(
            select(func.count()).select_from(
                select(AgentWebhookDlq)
                .where(
                    AgentWebhookDlq.project_id == project_id,
                    AgentWebhookDlq.resolved_at.is_(None),
                )
                .subquery()
            )
        )
        dlq_depth = int(dlq_q.scalar() or 0)

        return AgentKpis(
            window_days=window_days,
            total_turns=total,
            ok_turns=ok,
            refused_turns=refused,
            citations_rate=(cites / ok) if ok else 0.0,
            aggregate_route_rate=(agg / ok) if ok else 0.0,
            judge_block_rate=(judge_blocks / (ok + judge_blocks)) if (ok + judge_blocks) else 0.0,
            feedback_up=feedback_up,
            feedback_down=feedback_down,
            dlq_depth=dlq_depth,
        )
    raise HTTPException(status_code=500, detail="DB session exhausted")


# ---------------------------------------------------------------------------
# D1.4 — Calibration view
# ---------------------------------------------------------------------------


class CalibrationRow(BaseModel):
    turn_id: UUID
    conversation_id: UUID
    created_at: datetime
    user_message: str
    answer_text: Optional[str]
    status: str
    judge_verdict: Optional[str]
    judge_reasoning: Optional[str]
    judge_metrics: Optional[dict]
    original_answer_blocked: Optional[str]


@router.get("/calibration", response_model=list[CalibrationRow])
async def get_calibration(
    project_id: UUID,
    limit: int = 20,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[CalibrationRow]:
    """Recent judged turns for human calibration of the rubric.

    Returns up to ``limit`` rows ordered newest-first, where the judge
    has emitted a verdict. The original (pre-block) answer is surfaced
    from ``llm_plan.original_answer_blocked`` when the judge blocked.

    F-023-01 (round 2) — this surface discloses blocked originals, so it
    uses the strict fail-closed gate: tenant admin role or an explicit
    Modeller/Admin binding, with NO zero-bindings bootstrap bypass.
    """
    await _require_blocked_original_access(project_id, current_user)
    limit = max(1, min(limit, 100))
    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(
            select(AgentTurn)
            .join(
                AgentConversation,
                AgentTurn.conversation_id == AgentConversation.id,
            )
            .where(
                AgentConversation.project_id == project_id,
                AgentTurn.judge_verdict.isnot(None),
            )
            .order_by(desc(AgentTurn.created_at))
            .limit(limit)
        )
        rows = result.scalars().all()
        out: list[CalibrationRow] = []
        for t in rows:
            plan = t.llm_plan or {}
            original = plan.get("original_answer_blocked") if isinstance(plan, dict) else None
            out.append(
                CalibrationRow(
                    turn_id=t.id,
                    conversation_id=t.conversation_id,
                    created_at=t.created_at,
                    user_message=t.user_message,
                    answer_text=t.answer_text,
                    status=t.status,
                    judge_verdict=t.judge_verdict,
                    judge_reasoning=t.judge_reasoning,
                    judge_metrics=t.judge_metrics,
                    original_answer_blocked=original if isinstance(original, str) else None,
                )
            )
        return out
    raise HTTPException(status_code=500, detail="DB session exhausted")


# ---------------------------------------------------------------------------
# D2.4 — Cost dashboard
# ---------------------------------------------------------------------------


class CostDay(BaseModel):
    date: str
    input_tokens: int
    output_tokens: int
    estimated_usd: float


class CostProvider(BaseModel):
    """Per-provider spend split, sourced from the cost ledger (F-023-28).

    ``provider`` is the string the spend was costed against; rows written
    before the ledger had a provider column, or with an empty provider,
    surface as ``"unknown"``.
    """

    provider: str
    input_tokens: int
    output_tokens: int
    estimated_usd: float


class CostReport(BaseModel):
    window_days: int
    total_input_tokens: int
    total_output_tokens: int
    estimated_usd: float
    per_day: list[CostDay]
    # F-023-28 — per-provider spend split read from the ledger's own
    # provider column + recorded estimated_cost_usd, not inferred from
    # answer-vs-judge turn shape.
    per_provider: list[CostProvider]
    # Sum of the ledger's recorded estimated_cost_usd over the window. May
    # differ from estimated_usd (which re-estimates from turn tokens at the
    # report's default rate); the ledger figure reflects the actual rate
    # each turn was costed at, including the unmapped-provider fallback.
    ledger_estimated_usd: float


# Conservative defaults — used only when LLMConfig has no per-token
# pricing recorded. Values are USD per 1M tokens.
_DEFAULT_INPUT_USD_PER_M = 3.0
_DEFAULT_OUTPUT_USD_PER_M = 15.0


def _estimate_usd(input_tokens: int, output_tokens: int) -> float:
    return round(
        (input_tokens / 1_000_000.0) * _DEFAULT_INPUT_USD_PER_M
        + (output_tokens / 1_000_000.0) * _DEFAULT_OUTPUT_USD_PER_M,
        4,
    )


@router.get("/cost", response_model=CostReport)
async def get_cost(
    project_id: UUID,
    window_days: int = 30,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> CostReport:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    async for db in get_tenant_db(current_user.tenant_id):
        date_col = func.date_trunc("day", AgentTurn.created_at).label("d")
        result = await db.execute(
            select(
                date_col,
                func.sum(AgentTurn.usage_input_tokens).label("in_tok"),
                func.sum(AgentTurn.usage_output_tokens).label("out_tok"),
            )
            .join(
                AgentConversation,
                AgentTurn.conversation_id == AgentConversation.id,
            )
            .where(
                AgentConversation.project_id == project_id,
                AgentTurn.created_at >= cutoff,
            )
            .group_by(date_col)
            .order_by(date_col)
        )
        per_day: list[CostDay] = []
        total_in = 0
        total_out = 0
        for day, in_tok, out_tok in result.all():
            in_t = int(in_tok or 0)
            out_t = int(out_tok or 0)
            total_in += in_t
            total_out += out_t
            per_day.append(
                CostDay(
                    date=day.date().isoformat() if day else "",
                    input_tokens=in_t,
                    output_tokens=out_t,
                    estimated_usd=_estimate_usd(in_t, out_t),
                )
            )
        # F-023-28 — per-provider split from the cost ledger. The ledger
        # carries the provider the spend was costed against and the
        # estimated_cost_usd it was costed at, so the split is read from
        # data instead of inferred from turn shape. NULL/empty provider
        # rows (pre-migration or empty-string writes) collapse to "unknown".
        provider_col = func.coalesce(
            AgentCostEntry.provider, "unknown"
        ).label("provider")
        provider_result = await db.execute(
            select(
                provider_col,
                func.coalesce(func.sum(AgentCostEntry.input_tokens), 0).label("in_tok"),
                func.coalesce(func.sum(AgentCostEntry.output_tokens), 0).label("out_tok"),
                func.coalesce(
                    func.sum(AgentCostEntry.estimated_cost_usd), 0
                ).label("usd"),
            )
            .where(
                AgentCostEntry.project_id == project_id,
                AgentCostEntry.created_at >= cutoff,
            )
            .group_by(provider_col)
            .order_by(provider_col)
        )
        per_provider: list[CostProvider] = []
        ledger_total_usd = 0.0
        for prov, in_tok, out_tok, usd in provider_result.all():
            usd_f = float(usd or 0.0)
            ledger_total_usd += usd_f
            per_provider.append(
                CostProvider(
                    provider=prov or "unknown",
                    input_tokens=int(in_tok or 0),
                    output_tokens=int(out_tok or 0),
                    estimated_usd=round(usd_f, 4),
                )
            )

        return CostReport(
            window_days=window_days,
            total_input_tokens=total_in,
            total_output_tokens=total_out,
            estimated_usd=_estimate_usd(total_in, total_out),
            per_day=per_day,
            per_provider=per_provider,
            ledger_estimated_usd=round(ledger_total_usd, 4),
        )
    raise HTTPException(status_code=500, detail="DB session exhausted")
