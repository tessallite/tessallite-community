"""Conversation + turn endpoints — Phase Agent-A4.

Phase A: canned response only ("Phase B will produce real answers"). The
turn log row is persisted in the full F2 shape regardless, so the
plumbing is exercised end-to-end before any LLM cost lands.

Endpoints:
  POST   /projects/{project_id}/agent/conversations
  GET    /projects/{project_id}/agent/conversations
  GET    /projects/{project_id}/agent/conversations/{id}
  DELETE /projects/{project_id}/agent/conversations/{id}
  POST   /projects/{project_id}/agent/conversations/{id}/messages
  POST   /projects/{project_id}/agent/conversations/{id}/turns/{turn_id}/feedback
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, func, select

from shared.config.settings import get_settings
from shared.db.models import (
    AgentConversation,
    AgentTurn,
    Model,
    ProjectAgentConfig,
    ProjectPersona,
)
from shared.db.session import get_tenant_db
from shared.llm.adapter import build_adapter
from shared.llm.config_resolution import resolve_agent_llm_config
from src.auth.middleware import CurrentEmbedUser, CurrentUser, require_capability
from src.guardrails.block import apply_judge_block
from src.guardrails.budget import record_turn_cost
from src.guardrails.output import apply_output_guardrails
from src.judge.judge import JudgeOutcome, run_judge
from src.pipeline import TurnOutcome, persist_turn, run_turn
from src.sse.events import EventPublisher, _HEARTBEAT_SENTINEL, format_heartbeat, format_raw, format_sse
from src.webhooks import dispatch_event

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter(
    prefix="/projects/{project_id}/agent/conversations",
    tags=["agent-conversations"],
)

_background_tasks: set[asyncio.Task] = set()
_background_tasks_lock = threading.Lock()  # H-03 fix: protect concurrent add/discard


def _spawn_background(coro) -> asyncio.Task:
    """Create an asyncio task with a strong reference to prevent GC."""
    task = asyncio.create_task(coro)
    with _background_tasks_lock:
        _background_tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        with _background_tasks_lock:
            _background_tasks.discard(t)

    task.add_done_callback(_on_done)
    return task


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ConversationCreate(BaseModel):
    persona_id: Optional[UUID] = None
    pinned_model_id: Optional[UUID] = None


class ConversationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    caller_kind: str
    caller_ref: str
    persona_id: Optional[UUID] = None
    pinned_model_id: Optional[UUID] = None
    title: Optional[str] = None
    pinned_at: Optional[object] = None
    started_at: object
    last_active_at: object
    deleted_at: Optional[object] = None


class ConversationPatch(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    pinned: Optional[bool] = None
    persona_id: Optional[UUID] = None
    pinned_model_id: Optional[UUID] = None


class MessageSend(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class TurnResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    turn_index: int
    user_message: str
    answer_text: Optional[str]
    status: str
    latency_ms: int
    llm_plan: Optional[dict] = None
    thought_summary: Optional[str] = None
    semantic_query: Optional[dict] = None
    routed_sql: Optional[str] = None
    route: Optional[str] = None
    citations: Optional[list] = None
    user_feedback: Optional[dict] = None
    judge_verdict: Optional[str] = None
    judge_reasoning: Optional[str] = None
    judge_metrics: Optional[dict] = None
    guardrail_actions: Optional[list] = None
    usage_input_tokens: int = 0
    usage_output_tokens: int = 0
    rendered_output: Optional[str] = None
    chart_type: Optional[str] = None
    calculation_steps: Optional[list] = None
    query_result_sample: Optional[list] = None


class FeedbackBody(BaseModel):
    vote: str = Field(pattern="^(up|down)$")
    comment: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _redact_trace(turn: AgentTurn, cfg: ProjectAgentConfig) -> TurnResponse:
    """Build a TurnResponse honouring the project's visibility toggles.

    D2.5 — caller-facing trace fields are stripped when the matching
    show_* flag is False.

    F-023-01 — the conversation endpoints serve end users and embed
    consumers, so the judge-blocked original answer is NEVER serialised
    here, regardless of caller. Admins read the original via the
    role-gated trace surfaces (`/agent/log`, `/agent/calibration`).
    On a blocked turn every answer-derived artefact (chart HTML,
    calculation steps, result sample, citations) is withheld too, and
    ``judge_block_visibility="opaque"`` strips the judge reasoning.

    Round 2: ``semantic_query`` and ``thought_summary`` are also
    withheld on blocked turns — for compound/recipe/KPI turns the
    semantic trace embeds step ``first_row`` values and the computed
    ``combine_value``/``result``, and the thought summary accumulates
    narration-LLM thinking about the blocked answer."""
    resp = TurnResponse.model_validate(turn)
    if not cfg.show_thought_process:
        resp.thought_summary = None
    if not cfg.show_semantic_query:
        resp.semantic_query = None
    if not cfg.show_physical_query:
        resp.routed_sql = None
    if isinstance(resp.llm_plan, dict) and "original_answer_blocked" in resp.llm_plan:
        plan = dict(resp.llm_plan)
        plan.pop("original_answer_blocked", None)
        resp.llm_plan = plan
    if turn.status == "judge_blocked":
        resp.rendered_output = None
        resp.chart_type = None
        resp.calculation_steps = None
        resp.query_result_sample = None
        resp.citations = None
        resp.semantic_query = None
        resp.thought_summary = None
        resp.judge_reasoning = None
    return resp


async def _require_agent_enabled(db, project_id: UUID) -> ProjectAgentConfig:
    result = await db.execute(
        select(ProjectAgentConfig).where(
            ProjectAgentConfig.project_id == project_id
        )
    )
    config = result.scalar_one_or_none()
    if config is None or not config.enabled:
        # Spec §7.1 — return 404 for disabled to avoid leaking project existence.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )
    return config


async def _next_turn_index(db, conversation_id: UUID, *, reserve: bool = False, user_message: str = "") -> int:
    """Atomically allocate the next turn_index for a conversation.

    Locks the conversation row with FOR UPDATE to serialise concurrent
    inserts — prevents two requests from computing the same index.

    When *reserve* is True, inserts a placeholder ``AgentTurn`` row with
    ``status='streaming'`` and commits so the index is durably claimed
    before the lock is released. This prevents the streaming path from
    racing with another request that could allocate the same index.
    """
    await db.execute(
        select(AgentConversation.id)
        .where(AgentConversation.id == conversation_id)
        .with_for_update()
    )
    max_idx = await db.scalar(
        select(func.coalesce(func.max(AgentTurn.turn_index), -1)).where(
            AgentTurn.conversation_id == conversation_id
        )
    )
    next_idx = max_idx + 1
    if reserve:
        placeholder = AgentTurn(
            conversation_id=conversation_id,
            turn_index=next_idx,
            user_message=user_message or "(streaming)",
            status="streaming",
        )
        db.add(placeholder)
        await db.commit()
    return next_idx


# ---------------------------------------------------------------------------
# Ownership guard
# ---------------------------------------------------------------------------


def _enforce_project_scope(project_id: UUID, current_user: CurrentUser) -> None:
    """Embed users with project_ids claim can only access listed projects."""
    if isinstance(current_user, CurrentEmbedUser):
        if current_user.project_ids is not None and str(project_id).lower() not in current_user.project_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Embed token does not grant access to this project",
            )


def _enforce_conversation_ownership(
    conv: AgentConversation,
    current_user: CurrentUser,
) -> None:
    """Embed users may only access conversations they created."""
    if isinstance(current_user, CurrentEmbedUser):
        if conv.caller_ref != str(current_user.user_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation not found",
            )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


async def _validate_pinned_model(db, project_id: UUID, model_id: UUID) -> None:
    """The pinned model must belong to this project. Allow-list / persona
    membership is intentionally re-checked per turn (see assemble_prompt), so a
    later removal degrades the conversation gracefully to project default rather
    than hard-failing."""
    model = await db.get(Model, model_id)
    if model is None or model.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="pinned_model_id does not exist or does not belong to this project",
        )


@router.post("", response_model=ConversationResponse, status_code=201)
async def create_conversation(
    project_id: UUID,
    body: ConversationCreate,
    current_user: CurrentUser = Depends(require_capability("chat")),
) -> ConversationResponse:
    _enforce_project_scope(project_id, current_user)
    persona_override = None
    if isinstance(current_user, CurrentEmbedUser) and current_user.persona_id:
        persona_override = current_user.persona_id
    resolved_persona_id = persona_override or body.persona_id
    async for db in get_tenant_db(current_user.tenant_id):
        await _require_agent_enabled(db, project_id)
        if resolved_persona_id:
            persona = await db.get(ProjectPersona, resolved_persona_id)
            if persona is None or persona.project_id != project_id:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="persona_id does not exist or does not belong to this project",
                )
        if body.pinned_model_id is not None:
            await _validate_pinned_model(db, project_id, body.pinned_model_id)
        conv = AgentConversation(
            project_id=project_id,
            caller_kind="embed_user" if current_user.is_embed else "tenant_user",
            caller_ref=str(current_user.user_id),
            persona_id=resolved_persona_id,
            pinned_model_id=body.pinned_model_id,
        )
        db.add(conv)
        await db.commit()
        await db.refresh(conv)
        _spawn_background(
            dispatch_event(
                tenant_id=current_user.tenant_id,
                project_id=project_id,
                event_type="conversation.started",
                payload={
                    "caller_kind": conv.caller_kind,
                    "caller_ref": conv.caller_ref,
                    "started_at": conv.started_at.isoformat()
                    if conv.started_at
                    else None,
                },
                conversation_id=conv.id,
            )
        )
        return ConversationResponse.model_validate(conv)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.get("", response_model=list[ConversationResponse])
async def list_conversations(
    project_id: UUID,
    current_user: CurrentUser = Depends(require_capability("chat")),
) -> list[ConversationResponse]:
    _enforce_project_scope(project_id, current_user)
    async for db in get_tenant_db(current_user.tenant_id):
        await _require_agent_enabled(db, project_id)
        result = await db.execute(
            select(AgentConversation)
            .where(
                AgentConversation.project_id == project_id,
                AgentConversation.caller_ref == str(current_user.user_id),
                AgentConversation.deleted_at.is_(None),
            )
            .order_by(
                desc(AgentConversation.pinned_at.isnot(None)),
                desc(AgentConversation.pinned_at),
                desc(AgentConversation.last_active_at),
            )
        )
        return [
            ConversationResponse.model_validate(c)
            for c in result.scalars().all()
        ]
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.get("/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    project_id: UUID,
    conversation_id: UUID,
    current_user: CurrentUser = Depends(require_capability("chat")),
) -> ConversationResponse:
    _enforce_project_scope(project_id, current_user)
    async for db in get_tenant_db(current_user.tenant_id):
        await _require_agent_enabled(db, project_id)
        conv = await db.get(AgentConversation, conversation_id)
        if conv is None or conv.project_id != project_id:
            raise HTTPException(status_code=404, detail="Conversation not found")
        _enforce_conversation_ownership(conv, current_user)
        return ConversationResponse.model_validate(conv)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.get("/{conversation_id}/turns", response_model=list[TurnResponse])
async def list_turns(
    project_id: UUID,
    conversation_id: UUID,
    current_user: CurrentUser = Depends(require_capability("chat")),
) -> list[TurnResponse]:
    """Fetch every turn for a conversation in chronological order."""
    _enforce_project_scope(project_id, current_user)
    async for db in get_tenant_db(current_user.tenant_id):
        cfg = await _require_agent_enabled(db, project_id)
        conv = await db.get(AgentConversation, conversation_id)
        if conv is None or conv.project_id != project_id:
            raise HTTPException(status_code=404, detail="Conversation not found")
        _enforce_conversation_ownership(conv, current_user)
        result = await db.execute(
            select(AgentTurn)
            .where(AgentTurn.conversation_id == conversation_id)
            .order_by(AgentTurn.turn_index)
        )
        return [_redact_trace(t, cfg) for t in result.scalars().all()]
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    project_id: UUID,
    conversation_id: UUID,
    current_user: CurrentUser = Depends(require_capability("chat")),
) -> None:
    _enforce_project_scope(project_id, current_user)
    async for db in get_tenant_db(current_user.tenant_id):
        await _require_agent_enabled(db, project_id)
        conv = await db.get(AgentConversation, conversation_id)
        if conv is None or conv.project_id != project_id:
            raise HTTPException(status_code=404, detail="Conversation not found")
        _enforce_conversation_ownership(conv, current_user)
        conv.deleted_at = func.now()
        await db.commit()


@router.patch("/{conversation_id}", response_model=ConversationResponse)
async def patch_conversation(
    project_id: UUID,
    conversation_id: UUID,
    body: ConversationPatch,
    current_user: CurrentUser = Depends(require_capability("chat")),
) -> ConversationResponse:
    _enforce_project_scope(project_id, current_user)
    async for db in get_tenant_db(current_user.tenant_id):
        await _require_agent_enabled(db, project_id)
        conv = await db.get(AgentConversation, conversation_id)
        if conv is None or conv.project_id != project_id:
            raise HTTPException(status_code=404, detail="Conversation not found")
        _enforce_conversation_ownership(conv, current_user)
        if isinstance(current_user, CurrentEmbedUser):
            if body.title is not None or body.pinned is not None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Embed users cannot modify conversation metadata",
                )
        if body.title is not None:
            conv.title = body.title
        if body.pinned is not None:
            conv.pinned_at = func.now() if body.pinned else None
        # The model pin IS modifiable by embed users — it only ever narrows the
        # agent's access within the caller's existing scope. Explicit null clears
        # the pin (back to project default).
        if "pinned_model_id" in body.model_fields_set:
            if body.pinned_model_id is not None:
                await _validate_pinned_model(db, project_id, body.pinned_model_id)
            conv.pinned_model_id = body.pinned_model_id
        if "persona_id" in body.model_fields_set:
            if isinstance(current_user, CurrentEmbedUser) and current_user.persona_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Embed token persona cannot be overridden",
                )
            conv.persona_id = body.persona_id
        await db.commit()
        await db.refresh(conv)
        return ConversationResponse.model_validate(conv)
    raise HTTPException(status_code=500, detail="DB session exhausted")


def _emit_turn_webhook(
    tenant_id: str,
    project_id: UUID,
    turn,
    cfg: ProjectAgentConfig,
) -> None:
    """Fire the appropriate ``turn.*`` webhook for a freshly persisted
    turn. Refused turns get ``turn.refused``; everything else gets
    ``turn.completed``.

    F-023-01 (round 2) — the payload goes through the same redaction
    gate as every user surface (``_redact_trace``), and in sync judge
    mode the callers only invoke this AFTER ``apply_judge_block``: a
    blocked turn's webhook carries the block message and redacted
    citations, never the original answer."""
    event = "turn.refused" if turn.status == "refused" else "turn.completed"
    redacted = _redact_trace(turn, cfg)
    payload = {
        "turn_id": str(turn.id),
        "turn_index": turn.turn_index,
        "status": redacted.status,
        "user_message": turn.user_message,
        "answer_text": redacted.answer_text,
        "latency_ms": turn.latency_ms,
        "route": turn.route,
        "citations": redacted.citations,
    }
    _spawn_background(
        dispatch_event(
            tenant_id=tenant_id,
            project_id=project_id,
            event_type=event,
            payload=payload,
            conversation_id=turn.conversation_id,
            turn_id=turn.id,
        )
    )


def _emit_judge_webhook(
    tenant_id: str,
    project_id: UUID,
    turn,
    cfg: ProjectAgentConfig | None = None,
) -> None:
    """Emit ``turn.judge_blocked`` when a judge verdict is ``fail``.

    F-023-01 (round 2) — the reasoning is withheld from the
    outbound payload, matching what a user surface would show."""
    if turn.judge_verdict != "fail":
        return
    _spawn_background(
        dispatch_event(
            tenant_id=tenant_id,
            project_id=project_id,
            event_type="turn.judge_blocked",
            payload={
                "turn_id": str(turn.id),
                "verdict": turn.judge_verdict,
                "reasoning": None,
                "metrics": turn.judge_metrics,
            },
            conversation_id=turn.conversation_id,
            turn_id=turn.id,
        )
    )


async def _prior_turns_for_judge(
    db,
    conversation_id: UUID,
    exclude_turn_id: UUID | None = None,
) -> list[dict[str, str]]:
    """Return prior turns as a flat list of {role, content} dicts."""
    q = await db.execute(
        select(AgentTurn)
        .where(AgentTurn.conversation_id == conversation_id)
        .order_by(AgentTurn.turn_index)
    )
    history: list[dict[str, str]] = []
    for t in q.scalars().all():
        if exclude_turn_id and t.id == exclude_turn_id:
            continue
        if t.user_message:
            history.append({"role": "user", "content": t.user_message})
        if t.answer_text:
            history.append({"role": "assistant", "content": t.answer_text})
    return history


async def _judge_turn_background(
    tenant_id: str,
    project_id: UUID,
    turn_id: UUID,
    conversation_id: UUID,
    system_prompt: str,
    user_message: str,
    plan: Optional[dict],
    answer_text: str,
    sample_rows: Optional[list],
    result_row_count: int | None = None,
) -> None:
    """Background-task entry: open a fresh tenant DB session, run the
    judge LLM, write the verdict to the turn row."""
    try:
        async for db in get_tenant_db(tenant_id):
            cfg = await db.execute(
                select(ProjectAgentConfig).where(
                    ProjectAgentConfig.project_id == project_id
                )
            )
            cfg_row = cfg.scalar_one_or_none()
            if cfg_row is None:
                return
            conversation_history = await _prior_turns_for_judge(
                db, conversation_id, exclude_turn_id=turn_id,
            )
            outcome = await run_judge(
                db=db,
                cfg=cfg_row,
                system_prompt=system_prompt,
                user_message=user_message,
                plan=plan,
                answer_text=answer_text,
                sample_rows=sample_rows,
                result_row_count=result_row_count,
                conversation_history=conversation_history,
            )
            turn = await db.get(AgentTurn, turn_id)
            if turn is None:
                return
            turn.judge_verdict = outcome.verdict
            turn.judge_reasoning = outcome.reasoning
            turn.judge_metrics = outcome.metrics
            turn.usage_input_tokens = (
                (turn.usage_input_tokens or 0) + outcome.usage_input_tokens
            )
            turn.usage_output_tokens = (
                (turn.usage_output_tokens or 0) + outcome.usage_output_tokens
            )
            # D1.1 — async mode: block before commit if verdict=fail.
            apply_judge_block(cfg_row, turn, outcome)
            if outcome.usage_input_tokens or outcome.usage_output_tokens:
                await record_turn_cost(
                    db=db,
                    project_id=project_id,
                    turn_id=turn_id,
                    llm_config_id=cfg_row.judge_llm_config_id or cfg_row.answer_llm_config_id,
                    provider=outcome.provider or "unknown",
                    input_tokens=outcome.usage_input_tokens,
                    output_tokens=outcome.usage_output_tokens,
                )
            await db.commit()
            _emit_judge_webhook(tenant_id, project_id, turn, cfg_row)
            return
    except Exception:
        logger.exception("Background judge task failed")
        try:
            async for db in get_tenant_db(tenant_id):
                turn = await db.get(AgentTurn, turn_id)
                if turn is not None and turn.judge_verdict is None:
                    turn.judge_verdict = "unknown"
                    turn.judge_reasoning = "Judge task failed with an internal error"
                    await db.commit()
        except Exception:
            logger.exception("Failed to persist unknown verdict after judge error")


_REPAIRABLE_JUDGE_METRICS = (
    "factual",
    "accuracy",
    "complete",
    "completeness",
    "presentation",
    "narration",
    "answer quality",
)
_NON_REPAIRABLE_JUDGE_TERMS = (
    "policy",
    "safety",
    "compliance",
    "security",
    "privacy",
    "confidential",
    "authorization",
    "tenant",
    "restricted",
    "pii",
)


def _judge_failure_is_narration_repairable(judge_outcome: JudgeOutcome) -> bool:
    if judge_outcome.verdict != "fail":
        return False
    reasoning = (judge_outcome.reasoning or "").lower()
    if any(term in reasoning for term in _NON_REPAIRABLE_JUDGE_TERMS):
        return False
    low_metric_names: list[str] = []
    for name, value in (judge_outcome.metrics or {}).items():
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if numeric < 0.5:
            low_metric_names.append(str(name).lower())
    if any(
        any(term in metric_name for term in _NON_REPAIRABLE_JUDGE_TERMS)
        for metric_name in low_metric_names
    ):
        return False
    if any(
        any(term in metric_name for term in _REPAIRABLE_JUDGE_METRICS)
        for metric_name in low_metric_names
    ):
        return True
    return any(term in reasoning for term in ("factual", "accuracy", "complete", "omits", "narration", "data returned"))


def _shape_trace_from_outcome(outcome: TurnOutcome) -> dict[str, Any] | None:
    semantic = outcome.semantic_query
    if not isinstance(semantic, dict):
        return None
    shape = semantic.get("shape")
    return shape if isinstance(shape, dict) else None


@dataclass
class _NarrationRepairAttempt:
    repaired_answer: str
    judge_outcome: JudgeOutcome
    repair_input_tokens: int
    repair_output_tokens: int
    repair_provider: str
    accepted: bool
    actions: list[dict[str, Any]]


async def _repair_narration_after_sync_judge_fail(
    *,
    db,
    cfg,
    system_prompt: str,
    user_message: str,
    outcome: TurnOutcome,
    judge_outcome: JudgeOutcome,
    sample_rows: list[dict[str, Any]] | None,
    result_row_count: int | None,
    conversation_history: list[dict[str, str]] | None,
) -> _NarrationRepairAttempt | None:
    if outcome.status != "ok" or not _judge_failure_is_narration_repairable(judge_outcome):
        return None

    llm_config = await resolve_agent_llm_config(cfg.project_id, "answer", db)
    adapter = build_adapter(llm_config)
    payload = {
        "user_question": user_message,
        "original_answer": outcome.answer_text,
        "result_row_count": result_row_count,
        "sample_rows": sample_rows or [],
        "shape": _shape_trace_from_outcome(outcome),
        "conversation_history": conversation_history or [],
    }
    repair_system = (
        "You repair a conversational analytics answer after a judge found a "
        "factual, completeness, presentation, or narration issue. Rewrite the "
        "answer using only the provided result rows and deterministic shape "
        "facts. Do not add policy commentary. Do not compute new percentages, "
        "ratios, sums, differences, or growth rates unless those values already "
        "appear in the rows or facts. Keep the same output style as the original."
    )
    repair_prompt = (
        "The previous answer failed judge review for factual or completeness reasons.\n"
        "Return only the repaired user-facing answer.\n\n"
        + json.dumps(payload, default=str)
    )
    repaired = (await adapter.complete(repair_system, repair_prompt)).strip()
    output = apply_output_guardrails(cfg, repaired)
    repaired = output.text
    repair_input_tokens = int((getattr(adapter, "last_usage", None) or {}).get("input_tokens") or 0)
    repair_output_tokens = int((getattr(adapter, "last_usage", None) or {}).get("output_tokens") or 0)

    repaired_judge = await run_judge(
        db=db,
        cfg=cfg,
        system_prompt=system_prompt,
        user_message=user_message,
        plan=outcome.plan,
        answer_text=repaired,
        sample_rows=sample_rows,
        result_row_count=result_row_count,
        conversation_history=conversation_history,
    )
    actions = list(output.actions)
    accepted = repaired_judge.verdict in {"pass", "warn"}
    if accepted:
        actions.append({
            "layer": "judge",
            "action": "repair",
            "reason": "judge_narration_repair",
            "original_verdict": judge_outcome.verdict,
        })
    return _NarrationRepairAttempt(
        repaired_answer=repaired,
        judge_outcome=repaired_judge,
        repair_input_tokens=repair_input_tokens,
        repair_output_tokens=repair_output_tokens,
        repair_provider=getattr(llm_config, "provider", "unknown") or "unknown",
        accepted=accepted,
        actions=actions,
    )


@router.post("/{conversation_id}/messages", response_model=TurnResponse)
async def send_message(
    project_id: UUID,
    conversation_id: UUID,
    body: MessageSend,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(require_capability("chat")),
) -> TurnResponse:
    """Phase B1: assemble three-layer prompt, call answer LLM, dispatch
    tool call (query/clarify/refuse), narrate query results, persist the
    full F2 turn row.

    Async judge (B4) wraps this pipeline; SSE streaming (C1) replaces
    the synchronous return.
    """
    _enforce_project_scope(project_id, current_user)
    started = time.monotonic()
    jwt_token = current_user.raw_token

    async for db in get_tenant_db(current_user.tenant_id):
        cfg = await _require_agent_enabled(db, project_id)
        conv = await db.get(AgentConversation, conversation_id)
        if conv is None or conv.project_id != project_id:
            raise HTTPException(status_code=404, detail="Conversation not found")
        _enforce_conversation_ownership(conv, current_user)

        # F-023-10 — reserve-and-commit the turn index in its own short
        # transaction (the streaming path already does this). The previous
        # code held a SELECT ... FOR UPDATE row lock on the conversation
        # across the whole LLM pipeline (multiple model calls, potentially
        # minutes), causing idle-in-transaction connections and serialising
        # a user's concurrent requests for the duration of LLM latency.
        # F-023-11 — the reserved placeholder row also gives persist_turn a
        # real turn.id before record_turn_cost runs, so the answer-cost
        # ledger row is no longer written with turn_id=NULL.
        next_idx = await _next_turn_index(
            db, conversation_id, reserve=True, user_message=body.text,
        )

        try:
            outcome = await run_turn(
                db=db,
                cfg=cfg,
                conversation=conv,
                user_message=body.text,
                jwt_token=jwt_token,
            )
        except BaseException as exc:
            # F-023-10/11 — the index is now reserved as a committed
            # placeholder; if the pipeline raises unexpectedly, finalize the
            # placeholder as an error row instead of leaving it stuck in
            # 'streaming'. (run_turn normally returns error outcomes rather
            # than raising; this guards the unexpected path.)
            logger.exception("Sync pipeline failed: %s", type(exc).__name__)
            await db.rollback()
            async for err_db in get_tenant_db(current_user.tenant_id):
                placeholder = (await err_db.execute(
                    select(AgentTurn).where(
                        AgentTurn.conversation_id == conversation_id,
                        AgentTurn.turn_index == next_idx,
                        AgentTurn.status == "streaming",
                    )
                )).scalar_one_or_none()
                if placeholder is not None:
                    placeholder.status = "error"
                    placeholder.answer_text = f"Pipeline error: {type(exc).__name__}"
                    placeholder.latency_ms = int((time.monotonic() - started) * 1000)
                    await err_db.commit()
                break
            raise HTTPException(status_code=500, detail="Pipeline error")

        turn = await persist_turn(
            db=db,
            conversation=conv,
            turn_index=next_idx,
            user_message=body.text,
            outcome=outcome,
            started_monotonic=started,
            cfg=cfg,
            llm_provider=outcome.provider or "",
        )
        conv.last_active_at = func.now()
        await db.commit()
        await db.refresh(turn)
        # F-023-01 (round 2) — in sync judge mode the turn.completed
        # webhook must not leave before the verdict: a turn the judge is
        # about to block would otherwise ship the unredacted original to
        # the webhook_url. Deferred to after apply_judge_block below.
        sync_judge_pending = outcome.status == "ok" and cfg.judge_mode == "sync"
        if not sync_judge_pending:
            _emit_turn_webhook(current_user.tenant_id, project_id, turn, cfg)

        # Judge dispatch — only meaningful when the agent actually answered.
        # We rebuild the system prompt at judge time inside the background
        # task to avoid pickling closures here; pass enough context.
        if outcome.status == "ok":
            sample_rows = outcome.result_sample
            from src.prompt.assembler import assemble_prompt
            try:
                # F-023-16 — exclude the just-persisted turn so the judge's
                # copy of the agent system prompt does not carry the current
                # question/answer as CONVERSATION HISTORY (which
                # _prior_turns_for_judge already excludes).
                bundle = await assemble_prompt(
                    db, cfg, conv.id, body.text,
                    persona_id=conv.persona_id,
                    pinned_model_id=conv.pinned_model_id,
                    exclude_turn_id=turn.id,
                )
                system_prompt = bundle.system
            except Exception:
                logger.exception("Could not rebuild prompt for judge")
                system_prompt = ""

            conversation_history = await _prior_turns_for_judge(
                db, conv.id, exclude_turn_id=turn.id,
            )

            if cfg.judge_mode == "sync":
                judge_outcome = await run_judge(
                    db=db,
                    cfg=cfg,
                    system_prompt=system_prompt,
                    user_message=body.text,
                    plan=outcome.plan,
                    answer_text=outcome.answer_text,
                    sample_rows=sample_rows,
                    result_row_count=outcome.result_row_count,
                    conversation_history=conversation_history,
                )
                cost_records: list[tuple[Any, str, int, int]] = []
                if judge_outcome.usage_input_tokens or judge_outcome.usage_output_tokens:
                    cost_records.append((
                        cfg.judge_llm_config_id or cfg.answer_llm_config_id,
                        judge_outcome.provider or "unknown",
                        judge_outcome.usage_input_tokens,
                        judge_outcome.usage_output_tokens,
                    ))

                repair = await _repair_narration_after_sync_judge_fail(
                    db=db,
                    cfg=cfg,
                    system_prompt=system_prompt,
                    user_message=body.text,
                    outcome=outcome,
                    judge_outcome=judge_outcome,
                    sample_rows=sample_rows,
                    result_row_count=outcome.result_row_count,
                    conversation_history=conversation_history,
                )
                if repair is not None:
                    if repair.repair_input_tokens or repair.repair_output_tokens:
                        cost_records.append((
                            cfg.answer_llm_config_id,
                            repair.repair_provider,
                            repair.repair_input_tokens,
                            repair.repair_output_tokens,
                        ))
                    if repair.judge_outcome.usage_input_tokens or repair.judge_outcome.usage_output_tokens:
                        cost_records.append((
                            cfg.judge_llm_config_id or cfg.answer_llm_config_id,
                            repair.judge_outcome.provider or "unknown",
                            repair.judge_outcome.usage_input_tokens,
                            repair.judge_outcome.usage_output_tokens,
                        ))
                    if repair.accepted:
                        outcome.answer_text = repair.repaired_answer
                        turn.answer_text = repair.repaired_answer
                        turn.guardrail_actions = list(turn.guardrail_actions or []) + repair.actions
                        judge_outcome = repair.judge_outcome
                turn.judge_verdict = judge_outcome.verdict
                turn.judge_reasoning = judge_outcome.reasoning
                turn.judge_metrics = judge_outcome.metrics
                turn.usage_input_tokens = (
                    (turn.usage_input_tokens or 0)
                    + sum(item[2] for item in cost_records)
                )
                turn.usage_output_tokens = (
                    (turn.usage_output_tokens or 0)
                    + sum(item[3] for item in cost_records)
                )
                # D1.1 — sync mode: block before commit if verdict=fail.
                apply_judge_block(cfg, turn, judge_outcome)
                for llm_config_id, provider, input_tokens, output_tokens in cost_records:
                    await record_turn_cost(
                        db=db,
                        project_id=project_id,
                        turn_id=turn.id,
                        llm_config_id=llm_config_id,
                        provider=provider,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    )
                await db.commit()
                await db.refresh(turn)
                _emit_turn_webhook(current_user.tenant_id, project_id, turn, cfg)
                _emit_judge_webhook(current_user.tenant_id, project_id, turn, cfg)
            else:
                background_tasks.add_task(
                    _judge_turn_background,
                    current_user.tenant_id,
                    project_id,
                    turn.id,
                    conv.id,
                    system_prompt,
                    body.text,
                    outcome.plan,
                    outcome.answer_text,
                    sample_rows,
                    outcome.result_row_count,
                )

        return _redact_trace(turn, cfg)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.post(
    "/{conversation_id}/turns/{turn_id}/feedback",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def submit_feedback(
    project_id: UUID,
    conversation_id: UUID,
    turn_id: UUID,
    body: FeedbackBody,
    current_user: CurrentUser = Depends(require_capability("chat")),
) -> None:
    """Per F2 — feedback lands on the same turn row, not a separate turn."""
    _enforce_project_scope(project_id, current_user)
    async for db in get_tenant_db(current_user.tenant_id):
        await _require_agent_enabled(db, project_id)
        conv = await db.get(AgentConversation, conversation_id)
        if conv is None or conv.project_id != project_id:
            raise HTTPException(status_code=404, detail="Conversation not found")
        _enforce_conversation_ownership(conv, current_user)
        turn = await db.get(AgentTurn, turn_id)
        if turn is None or turn.conversation_id != conversation_id:
            raise HTTPException(status_code=404, detail="Turn not found")
        # F-023-23(a) — record the feedback timestamp per the F2 spec.
        from datetime import datetime, timezone
        turn.user_feedback = {
            "vote": body.vote,
            "comment": body.comment,
            "by": str(current_user.user_id),
            "at": datetime.now(timezone.utc).isoformat(),
        }
        await db.commit()
        _spawn_background(
            dispatch_event(
                tenant_id=current_user.tenant_id,
                project_id=project_id,
                event_type="turn.feedback",
                payload={
                    "turn_id": str(turn.id),
                    "vote": body.vote,
                    "comment": body.comment,
                },
                conversation_id=conversation_id,
                turn_id=turn.id,
            )
        )


# ---------------------------------------------------------------------------
# SSE streaming — Phase C1
# ---------------------------------------------------------------------------


_HEARTBEAT_INTERVAL_SEC = 15.0


async def _run_turn_into_publisher(
    tenant_id: str,
    project_id: UUID,
    conversation_id: UUID,
    turn_index: int,
    user_message: str,
    jwt_token: str,
    started: float,
    publisher: EventPublisher,
    *,
    embed_caller_ref: str | None = None,
) -> None:
    """Open a fresh tenant DB session, run the pipeline against the
    publisher, persist the turn, run the judge (sync or scheduled),
    finally close the publisher."""
    try:
        async for db in get_tenant_db(tenant_id):
            try:
                cfg_row = await db.execute(
                    select(ProjectAgentConfig).where(
                        ProjectAgentConfig.project_id == project_id
                    )
                )
                cfg = cfg_row.scalar_one_or_none()
                if cfg is None or not cfg.enabled:
                    await publisher.emit(
                        "turn.error", reason="agent_disabled"
                    )
                    return
                conv = await db.get(AgentConversation, conversation_id)
                if conv is None or conv.project_id != project_id:
                    await publisher.emit(
                        "turn.error", reason="conversation_not_found"
                    )
                    return
                if embed_caller_ref and conv.caller_ref != embed_caller_ref:
                    await publisher.emit(
                        "turn.error", reason="conversation_not_found"
                    )
                    return

                outcome = await run_turn(
                    db=db,
                    cfg=cfg,
                    conversation=conv,
                    user_message=user_message,
                    jwt_token=jwt_token,
                    publisher=publisher,
                )

                turn = await persist_turn(
                    db=db,
                    conversation=conv,
                    turn_index=turn_index,
                    user_message=user_message,
                    outcome=outcome,
                    started_monotonic=started,
                    cfg=cfg,
                    llm_provider=outcome.provider or "",
                )
                conv.last_active_at = func.now()
                if turn_index == 0 and not conv.title:
                    conv.title = user_message[:200]
                await db.commit()
                await db.refresh(turn)

                # In sync judge mode we must run the judge BEFORE emitting
                # turn.completed — otherwise the streaming UI flashes the
                # original answer, then swaps it for the block message. In
                # async mode the judge runs after the stream closes, so we
                # emit completed immediately.
                run_sync_judge = (
                    outcome.status == "ok" and cfg.judge_mode == "sync"
                )

                if not run_sync_judge:
                    # F-023-01 (round 2) — in sync mode the turn.completed
                    # webhook must not leave before the verdict; it is
                    # deferred to after apply_judge_block in that branch.
                    _emit_turn_webhook(tenant_id, project_id, turn, cfg)
                    judge_pending = (
                        outcome.status == "ok"
                        and cfg.judge_mode == "async"
                    )
                    redacted = _redact_trace(turn, cfg)
                    await publisher.emit(
                        "turn.completed",
                        turn_id=str(turn.id),
                        turn_index=turn.turn_index,
                        status=turn.status,
                        answer_text=turn.answer_text,
                        latency_ms=turn.latency_ms,
                        citations=turn.citations,
                        thought_summary=redacted.thought_summary,
                        rendered_output=turn.rendered_output,
                        chart_type=turn.chart_type,
                        semantic_query=redacted.semantic_query,
                        routed_sql=redacted.routed_sql,
                        route=turn.route,
                        judge_pending=judge_pending,
                        result_sample=outcome.result_sample,
                        provider=outcome.provider,
                        calculation_steps=outcome.calculation_steps,
                    )

                if outcome.status != "ok":
                    return

                # Judge — sync over the same stream, async after stream close.
                sample_rows = outcome.result_sample
                from src.prompt.assembler import assemble_prompt
                try:
                    # F-023-16 — exclude the just-persisted turn from the
                    # judge's rebuilt system prompt (kept consistent with
                    # _prior_turns_for_judge below).
                    bundle = await assemble_prompt(
                        db, cfg, conv.id, user_message,
                        persona_id=conv.persona_id,
                        pinned_model_id=conv.pinned_model_id,
                        exclude_turn_id=turn.id,
                    )
                    system_prompt = bundle.system
                except Exception:
                    logger.exception("Could not rebuild prompt for judge")
                    system_prompt = ""

                conversation_history = await _prior_turns_for_judge(
                    db, conv.id, exclude_turn_id=turn.id,
                )

                if run_sync_judge:
                    judge_outcome = await run_judge(
                        db=db,
                        cfg=cfg,
                        system_prompt=system_prompt,
                        user_message=user_message,
                        plan=outcome.plan,
                        answer_text=outcome.answer_text,
                        sample_rows=sample_rows,
                        result_row_count=outcome.result_row_count,
                        conversation_history=conversation_history,
                    )
                    turn.judge_verdict = judge_outcome.verdict
                    turn.judge_reasoning = judge_outcome.reasoning
                    turn.judge_metrics = judge_outcome.metrics
                    turn.usage_input_tokens = (
                        (turn.usage_input_tokens or 0)
                        + judge_outcome.usage_input_tokens
                    )
                    turn.usage_output_tokens = (
                        (turn.usage_output_tokens or 0)
                        + judge_outcome.usage_output_tokens
                    )
                    # D1.1 — sync mode: block before commit if verdict=fail.
                    apply_judge_block(cfg, turn, judge_outcome)
                    if judge_outcome.usage_input_tokens or judge_outcome.usage_output_tokens:
                        await record_turn_cost(
                            db=db,
                            project_id=project_id,
                            turn_id=turn.id,
                            llm_config_id=cfg.judge_llm_config_id or cfg.answer_llm_config_id,
                            provider=judge_outcome.provider or "unknown",
                            input_tokens=judge_outcome.usage_input_tokens,
                            output_tokens=judge_outcome.usage_output_tokens,
                        )
                    await db.commit()
                    await db.refresh(turn)
                    _emit_turn_webhook(tenant_id, project_id, turn, cfg)
                    _emit_judge_webhook(tenant_id, project_id, turn, cfg)
                    # F-023-01 — emit only redacted fields: on a fail
                    # verdict answer_text is already the block message
                    # (apply_judge_block), and the redacted response
                    # withholds the chart/sample/citations artefacts and
                    # the reasoning in opaque mode.
                    redacted = _redact_trace(turn, cfg)
                    await publisher.emit(
                        "turn.completed",
                        turn_id=str(turn.id),
                        turn_index=turn.turn_index,
                        status=turn.status,
                        answer_text=turn.answer_text,
                        latency_ms=turn.latency_ms,
                        citations=redacted.citations,
                        thought_summary=redacted.thought_summary,
                        rendered_output=redacted.rendered_output,
                        chart_type=redacted.chart_type,
                        semantic_query=redacted.semantic_query,
                        routed_sql=redacted.routed_sql,
                        route=turn.route,
                        result_sample=redacted.query_result_sample,
                        calculation_steps=redacted.calculation_steps,
                    )
                    await publisher.emit(
                        "turn.judged",
                        turn_id=str(turn.id),
                        verdict=judge_outcome.verdict,
                        reasoning=redacted.judge_reasoning,
                        metrics=judge_outcome.metrics,
                        status=turn.status,
                        answer_text=turn.answer_text,
                    )
                else:
                    _spawn_background(
                        _judge_turn_background(
                            tenant_id=tenant_id,
                            project_id=project_id,
                            turn_id=turn.id,
                            conversation_id=conv.id,
                            system_prompt=system_prompt,
                            user_message=user_message,
                            plan=outcome.plan,
                            answer_text=outcome.answer_text,
                            sample_rows=sample_rows,
                            result_row_count=outcome.result_row_count,
                        )
                    )
                return
            except BaseException as exc:
                logger.exception("Streaming pipeline failed: %s", type(exc).__name__)
                try:
                    async for err_db in get_tenant_db(tenant_id):
                        placeholder = (await err_db.execute(
                            select(AgentTurn).where(
                                AgentTurn.conversation_id == conversation_id,
                                AgentTurn.turn_index == turn_index,
                                AgentTurn.status == "streaming",
                            )
                        )).scalar_one_or_none()
                        if placeholder is not None:
                            placeholder.status = "error"
                            placeholder.answer_text = f"Pipeline error: {type(exc).__name__}"
                            placeholder.latency_ms = int((time.monotonic() - started) * 1000)
                            await err_db.commit()
                        break
                except Exception:
                    logger.warning("Failed to finalize streaming placeholder for turn_index=%d", turn_index)
                if isinstance(exc, Exception):
                    await publisher.emit("turn.error", reason="pipeline_error", detail=str(exc))
                else:
                    await publisher.emit("turn.error", reason="pipeline_error", detail=f"{type(exc).__name__}: {exc}")
    finally:
        await publisher.close()


@router.post("/{conversation_id}/messages/stream")
async def send_message_stream(
    project_id: UUID,
    conversation_id: UUID,
    body: MessageSend,
    current_user: CurrentUser = Depends(require_capability("chat")),
) -> StreamingResponse:
    """SSE variant of POST /messages — emits per-phase events as they
    happen rather than waiting for the full turn to finish.

    Event names per spec §5.1: turn.started, plan.tool, recipe.step,
    query.rows, narration.delta, turn.completed, turn.judged,
    turn.blocked, turn.error.
    """
    _enforce_project_scope(project_id, current_user)
    started = time.monotonic()
    jwt_token = current_user.raw_token

    # Pre-flight: validate config + conversation, allocate next turn_index.
    next_idx: int = 0
    async for db in get_tenant_db(current_user.tenant_id):
        await _require_agent_enabled(db, project_id)
        conv = await db.get(AgentConversation, conversation_id)
        if conv is None or conv.project_id != project_id:
            raise HTTPException(status_code=404, detail="Conversation not found")
        _enforce_conversation_ownership(conv, current_user)
        next_idx = await _next_turn_index(db, conversation_id, reserve=True, user_message=body.text)
        break

    publisher = EventPublisher()
    pipeline_task = asyncio.create_task(
        _run_turn_into_publisher(
            tenant_id=current_user.tenant_id,
            project_id=project_id,
            conversation_id=conversation_id,
            turn_index=next_idx,
            user_message=body.text,
            jwt_token=jwt_token,
            started=started,
            publisher=publisher,
            embed_caller_ref=(
                str(current_user.user_id)
                if isinstance(current_user, CurrentEmbedUser)
                else None
            ),
        )
    )

    async def _stream():
        try:
            # F-023-26(a) — consume through the public drain() generator
            # (which owns the heartbeat timeout) instead of reaching into
            # publisher._queue directly.
            async for item in publisher.drain(
                heartbeat_interval=_HEARTBEAT_INTERVAL_SEC
            ):
                if item is _HEARTBEAT_SENTINEL:
                    yield format_heartbeat()
                    continue
                yield format_sse(item)
        finally:
            if not pipeline_task.done():
                pipeline_task.cancel()
            yield format_raw("turn.stream.closed")

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
