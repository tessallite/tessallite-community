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

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError

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
# The service's ONE body-parameter foreign-key scope predicate; see
# src/api/_body_scope.py for why the hand-rolled copies were consolidated.
from src.api._body_scope import ensure_ref_in_project
from src.auth.project_access import require_project_chat_access
from src.auth.middleware import (
    CurrentEmbedUser,
    CurrentUser,
    is_human_tenant_admin_or_system_admin,
    require_capability,
)
from src.guardrails.block import apply_judge_block
from src.guardrails.budget import (
    reconcile_budget_reservation,
    record_turn_cost,
    reserve_budget,
)
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
        # Bug-8349 R2 HIGH, defense in depth — this is the shared
        # fire-and-forget spawner for every background task in this module,
        # including every ``dispatch_event`` webhook call. Before this fix
        # the done-callback only removed the task from the keep-alive set
        # and NEVER retrieved its exception, so a failure that escaped a
        # background coroutine (despite dispatch_event's own "never raises"
        # contract) was silently discarded by asyncio with no log entry, no
        # DLQ row, and no metric — the exact failure mode this whole lane
        # exists to close. ``t.exception()`` is the documented way to
        # retrieve and thereby mark a Task's exception as retrieved (skips
        # asyncio's own separate "Task exception was never retrieved"
        # warning); guard ``cancelled()`` first since ``exception()`` raises
        # ``CancelledError`` on a cancelled task instead of returning one.
        if not t.cancelled():
            exc = t.exception()
            if exc is not None:
                logger.error(
                    "Background task failed with an unhandled exception",
                    exc_info=exc,
                )

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
    # Bug-5952 -- surface the persona context used for query generation.
    persona_id: Optional[UUID] = None
    persona_name: Optional[str] = None


class FeedbackBody(BaseModel):
    vote: str = Field(pattern="^(up|down)$")
    comment: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _redact_trace(
    turn: AgentTurn,
    cfg: ProjectAgentConfig,
    *,
    persona_id: Optional[UUID] = None,
    persona_name: Optional[str] = None,
) -> TurnResponse:
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
    narration-LLM thinking about the blocked answer.

    Bug-5952 — optional persona_id/persona_name are stamped onto the
    response so callers know which persona context was used."""
    resp = TurnResponse.model_validate(turn)
    # Bug-5952 -- attach persona context to the response.
    if persona_id is not None:
        resp.persona_id = persona_id
    if persona_name is not None:
        resp.persona_name = persona_name
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
    # Bug-5957 — strip diagnostic keys from llm_plan that could leak
    # internal details (raw exception strings, debug metadata).
    if isinstance(resp.llm_plan, dict):
        _PLAN_DIAGNOSTIC_KEYS = {"detail", "raw", "traceback", "stack_trace"}
        leaked = _PLAN_DIAGNOSTIC_KEYS & resp.llm_plan.keys()
        if leaked:
            plan = dict(resp.llm_plan)
            for k in leaked:
                plan.pop(k)
            resp.llm_plan = plan
    # Bug-5957 — strip the raw `detail` key from each guardrail action
    # entry so internal exception strings are not exposed to end users.
    if resp.guardrail_actions:
        sanitised = []
        for entry in resp.guardrail_actions:
            if isinstance(entry, dict) and "detail" in entry:
                entry = {k: v for k, v in entry.items() if k != "detail"}
            sanitised.append(entry)
        resp.guardrail_actions = sanitised
    # Bug-8290 — ``judge_pending`` is a pre-verdict sync turn: the answer has
    # not yet been cleared by the judge, so it is withheld EXACTLY as a
    # ``judge_blocked`` turn is. The answer content itself (answer_text and the
    # answer-derived artefacts) must not be user-reachable until the verdict
    # releases the row to ``ok`` (or blocks it). A pending row exposes no answer.
    if turn.status in ("judge_blocked", "judge_pending"):
        resp.rendered_output = None
        resp.chart_type = None
        resp.calculation_steps = None
        resp.query_result_sample = None
        resp.citations = None
        resp.semantic_query = None
        resp.thought_summary = None
        resp.judge_reasoning = None
    if turn.status == "judge_pending":
        # No block message has been written yet (the verdict has not run), so
        # answer_text still holds the unvetted original — withhold it. The
        # caller sees a pending, content-free row until the judge resolves it.
        resp.answer_text = None
    return resp


async def _resolve_persona_name(
    db, project_id: UUID, persona_id: UUID | None
) -> str | None:
    """Bug-5952 -- resolve a ProjectPersona id to its display name.

    Scoped to ``project_id``, which must come from the URL path. The create
    and PATCH handlers prove a SUBMITTED ``persona_id`` belongs to the path
    project, but that says nothing about ids ALREADY STORED: an
    ``agent_conversations`` row carrying a foreign ``persona_id`` — written
    before those guards existed, or restored by a project import bundle —
    still resolved through a bare ``db.get`` here, and the name is stamped
    onto every ``TurnResponse``. Persona names are business-descriptive
    ("EU-Restricted Finance Analyst"), so that was a cross-project disclosure
    on a successful 200 rather than a refusal.

    A persona the path project does not own returns ``None`` — exactly what an
    unknown persona id already returned, so the response simply carries no
    persona name.
    """
    if persona_id is None:
        return None
    result = await db.execute(
        select(ProjectPersona)
        .where(ProjectPersona.project_id == project_id)
        .where(ProjectPersona.id == persona_id)
    )
    persona = result.scalars().one_or_none()
    return persona.name if persona is not None else None


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


async def _require_project_access_and_agent(
    db,
    project_id: UUID,
    current_user: CurrentUser,
) -> ProjectAgentConfig:
    """Bug-5951 -- verify the caller has at least viewer access to the
    project (via ``UserAccessBinding`` or admin bypass) AND that the
    project's agent is enabled.  Previously only embed-token project
    scope was checked; regular tenant users could access any project's
    agent conversations without a binding.

    Bug-8589 HALF A -- the delegate is the service's CHAT tier
    (``src/auth/project_access.require_project_chat_access``), not the shared
    terminal directly. These nine routes gate on ``require_capability("chat")``,
    which has no ``CurrentServiceUser`` branch, so the shared terminal's
    "a service principal was already scope-authorized at the route level"
    posture had no premise here and admitted any service token to any project's
    conversation content. The chat tier refuses a service principal by type
    first, then delegates the (embed-aware) human decision unchanged."""
    await require_project_chat_access(
        db, current_user, project_id=project_id, min_role="viewer",
    )
    return await _require_agent_enabled(db, project_id)


@dataclass
class _TurnReservation:
    """Result of reserving (or deduping) a turn for a send.

    ``turn_index`` is the index the pipeline should persist into. When
    ``is_duplicate`` is True the caller must NOT run the pipeline again — it
    replays/returns the turn identified by ``existing_turn_id`` (Bug-6521).
    ``existing_status`` is the status of that pre-existing turn so the caller
    can distinguish a completed turn (replay its result) from an in-flight
    reservation (still ``streaming``).
    """

    turn_index: int
    existing_turn_id: Optional[UUID]
    existing_status: Optional[str]
    is_duplicate: bool


async def _reserve_turn(
    db,
    conversation_id: UUID,
    *,
    user_message: str,
    idempotency_key: Optional[str],
) -> _TurnReservation:
    """Reserve a turn index for a send, deduping on the idempotency key.

    Bug-6521 — the streaming-retry loop (shared-ui) and the conv-client sync
    fallback both re-POST a state-changing request. Without a key, agent-service
    reserved a fresh turn per POST, so a retry duplicated the turn and its side
    effects. This helper makes the reservation idempotent per
    ``(conversation_id, idempotency_key)``:

    * No key -> reserve a fresh placeholder (legacy behaviour; keyless callers
      are never deduped — fail-safe, a legitimate first turn always runs).
    * Key + a TERMINAL turn already exists (status not ``streaming`` and not
      ``judge_pending``) -> return it as a duplicate; the caller replays the
      persisted result WITHOUT re-running the pipeline. This is the harmful case
      the bug describes: a turn that ran to completion server-side while the
      client missed the events.
    * Key + a ``streaming`` placeholder already exists -> reuse that SAME row
      (same ``turn_index``); the caller re-runs into it. This never mints a
      second row for a key, and it is fail-safe: a dead/orphaned reservation is
      not permanently withheld, and a concurrent retry writes the single row
      (last-writer-wins) rather than a duplicate persisted turn.
    * Key + a ``judge_pending`` turn already exists (Bug-8290) -> the original
      request is committed but its sync-judge verdict has NOT resolved yet, so
      the durable answer is unvetted and NON-releasable. It is returned as a
      duplicate (``is_duplicate=True``) so the retry does NOT re-run the pipeline
      into a row the in-flight original still owns — but the replayed view is run
      through ``_redact_trace``/``_replay_turn_into_publisher``, which now
      withhold every judge_pending answer. So the retry NEVER serves the unvetted
      answer: it sees a content-free pending row until the verdict releases the
      row to ``ok`` (or blocks it), at which point a later fetch/retry resolves.

    Atomicity: the reservation INSERT is protected by the partial unique index
    ``(conversation_id, idempotency_key)``. If two first-attempts race, the loser
    catches ``IntegrityError`` and reloads the winner's placeholder, then reuses
    it — so no duplicate turn escapes even under a tight race.
    """
    if idempotency_key:
        existing = (await db.execute(
            select(AgentTurn).where(
                AgentTurn.conversation_id == conversation_id,
                AgentTurn.idempotency_key == idempotency_key,
            )
        )).scalar_one_or_none()
        if existing is not None:
            if existing.status == "streaming":
                # In-flight (or orphaned) reservation — reuse the same row.
                return _TurnReservation(
                    turn_index=existing.turn_index,
                    existing_turn_id=existing.id,
                    existing_status=existing.status,
                    is_duplicate=False,
                )
            # Completed/terminal — replay, do not re-run.
            return _TurnReservation(
                turn_index=existing.turn_index,
                existing_turn_id=existing.id,
                existing_status=existing.status,
                is_duplicate=True,
            )

    # No key, or no existing turn for the key: reserve a fresh placeholder.
    await db.execute(
        select(AgentConversation.id)
        .where(AgentConversation.id == conversation_id)
        .with_for_update()
    )

    # Bug-7588 -- reject a second concurrent streaming request for the same
    # conversation when no idempotency key is provided.  The FOR UPDATE
    # lock serialises this check-and-insert.  When a key IS present, the
    # idempotency logic above already handles deduplication (reuse or replay),
    # so a concurrent retry with the same key must not be blocked here.
    # Staleness threshold: streaming placeholders older than 10 minutes are
    # considered orphaned (process crash/redeploy) and are ignored so they
    # do not permanently brick the conversation.
    if not idempotency_key:
        from datetime import datetime, timedelta, timezone
        _stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
        active_streaming = await db.scalar(
            select(func.count()).select_from(AgentTurn).where(
                AgentTurn.conversation_id == conversation_id,
                AgentTurn.status == "streaming",
                AgentTurn.created_at > _stale_cutoff,
            )
        )
        if active_streaming and active_streaming > 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "A turn is already being processed for this conversation. "
                    "Please wait for it to complete before sending another message."
                ),
            )

    max_idx = await db.scalar(
        select(func.coalesce(func.max(AgentTurn.turn_index), -1)).where(
            AgentTurn.conversation_id == conversation_id
        )
    )
    next_idx = max_idx + 1
    placeholder = AgentTurn(
        conversation_id=conversation_id,
        turn_index=next_idx,
        user_message=user_message or "(streaming)",
        status="streaming",
        idempotency_key=idempotency_key or None,
    )
    db.add(placeholder)
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent first-attempt won the race for this key. Reload its
        # placeholder and reuse it rather than surfacing an error.
        await db.rollback()
        if idempotency_key:
            winner = (await db.execute(
                select(AgentTurn).where(
                    AgentTurn.conversation_id == conversation_id,
                    AgentTurn.idempotency_key == idempotency_key,
                )
            )).scalar_one_or_none()
            if winner is not None:
                return _TurnReservation(
                    turn_index=winner.turn_index,
                    existing_turn_id=winner.id,
                    existing_status=winner.status,
                    is_duplicate=winner.status != "streaming",
                )
        raise
    await db.refresh(placeholder)
    return _TurnReservation(
        turn_index=next_idx,
        existing_turn_id=placeholder.id,
        existing_status="streaming",
        is_duplicate=False,
    )


# ---------------------------------------------------------------------------
# Ownership guard
# ---------------------------------------------------------------------------


def _is_llm_timeout(exc: BaseException) -> bool:
    """Bug-6010 -- detect LLM provider timeout exceptions.

    Returns True for TimeoutError, asyncio.TimeoutError, httpx timeout
    exceptions, and any exception whose message contains 'timeout' or
    'timed out'."""
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return True
    # httpx.TimeoutException (covers ConnectTimeout, ReadTimeout, etc.)
    try:
        import httpx
        if isinstance(exc, httpx.TimeoutException):
            return True
    except ImportError:
        pass
    detail = str(exc).lower()
    if "timeout" in detail or "timed out" in detail:
        return True
    return False


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
    """Non-admin callers may only access conversations they created."""
    if is_human_tenant_admin_or_system_admin(current_user):
        return
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
    than hard-failing.

    Delegates the predicate to ``_body_scope.ensure_ref_in_project`` — the
    service's one body-FK scope guard — rather than repeating the
    get-then-compare by hand. The status stays 422; the detail is the shared
    guard's message, which additionally names the offending id. No client or
    test matched the previous string (verified by grep across the frontend,
    every service and the seed bundles)."""
    await ensure_ref_in_project(
        db,
        Model,
        ref_id=model_id,
        project_id=project_id,
        field_name="pinned_model_id",
        noun="a model",
    )


async def _validate_persona(db, project_id: UUID, persona_id: UUID) -> None:
    """The conversation's persona must belong to this project.

    The CREATE path has enforced this since Bug-5951; the PATCH path did not,
    so the same submitted id was refused by ``POST /conversations`` and
    accepted by ``PATCH /conversations/{id}``. Both now call THIS function, so
    the symmetry is structural rather than two independent checks that can
    drift apart again.

    A foreign persona is not merely an integrity problem. ``persona_name`` is
    resolved for every turn response by ``_resolve_persona_name``, which loads
    ``ProjectPersona`` by id with no project predicate and returns its NAME —
    so an unguarded PATCH let a chat caller in project A read back project B's
    persona names one id at a time. (The persona's field SCOPES fail closed:
    ``assembler._apply_persona_filter`` drops every model the persona has no
    scope row for, so a foreign persona narrows the agent to nothing rather
    than widening it. The disclosure is the live exposure; the containment
    break is the invariant.)"""
    await ensure_ref_in_project(
        db,
        ProjectPersona,
        ref_id=persona_id,
        project_id=project_id,
        field_name="persona_id",
        noun="a persona",
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
        await _require_project_access_and_agent(db, project_id, current_user)
        if resolved_persona_id:
            await _validate_persona(db, project_id, resolved_persona_id)
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
        await _require_project_access_and_agent(db, project_id, current_user)
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
        await _require_project_access_and_agent(db, project_id, current_user)
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
        cfg = await _require_project_access_and_agent(db, project_id, current_user)
        conv = await db.get(AgentConversation, conversation_id)
        if conv is None or conv.project_id != project_id:
            raise HTTPException(status_code=404, detail="Conversation not found")
        _enforce_conversation_ownership(conv, current_user)
        persona_name = await _resolve_persona_name(db, project_id, conv.persona_id)
        result = await db.execute(
            select(AgentTurn)
            .where(AgentTurn.conversation_id == conversation_id)
            .order_by(AgentTurn.turn_index)
        )
        return [
            _redact_trace(
                t, cfg,
                persona_id=conv.persona_id,
                persona_name=persona_name,
            )
            for t in result.scalars().all()
        ]
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    project_id: UUID,
    conversation_id: UUID,
    current_user: CurrentUser = Depends(require_capability("chat")),
) -> None:
    _enforce_project_scope(project_id, current_user)
    async for db in get_tenant_db(current_user.tenant_id):
        await _require_project_access_and_agent(db, project_id, current_user)
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
        await _require_project_access_and_agent(db, project_id, current_user)
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
        # Persona override is refused for a persona-bound embed token
        # regardless of the value submitted, so it is decided with the other
        # authorization checks rather than mid-mutation.
        if (
            "persona_id" in body.model_fields_set
            and isinstance(current_user, CurrentEmbedUser)
            and current_user.persona_id
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Embed token persona cannot be overridden",
            )

        # EVERY body-supplied foreign key is proven BEFORE the first
        # assignment. The persona check is the one this lane added — the
        # handler used to assign ``conv.persona_id`` straight through, so an id
        # that POST /conversations refused, PATCH accepted; both now call
        # ``_validate_persona``, so the symmetry is structural. Both guards key
        # on the field being PRESENT (``model_fields_set``), never on it being
        # truthy, so an explicit null still clears the pin or the persona back
        # to the project default.
        #
        # Hoisting them above the assignments is not cosmetic. The guards issue
        # a SELECT on a session with autoflush on, so validating in-line would
        # flush the already-assigned title / pinned_at / pinned_model_id to the
        # database on the way to REFUSING the request, leaving correctness
        # resting on the session teardown's rollback rather than on the
        # handler. Nothing is written until every reference has been proven.
        if "pinned_model_id" in body.model_fields_set:
            if body.pinned_model_id is not None:
                await _validate_pinned_model(db, project_id, body.pinned_model_id)
        if "persona_id" in body.model_fields_set:
            if body.persona_id is not None:
                await _validate_persona(db, project_id, body.persona_id)

        if body.title is not None:
            conv.title = body.title
        if body.pinned is not None:
            conv.pinned_at = func.now() if body.pinned else None
        # The model pin IS modifiable by embed users — it only ever narrows the
        # agent's access within the caller's existing scope.
        if "pinned_model_id" in body.model_fields_set:
            conv.pinned_model_id = body.pinned_model_id
        if "persona_id" in body.model_fields_set:
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
    """Emit ``turn.judge_blocked`` whenever a turn was judge-blocked.

    Bug-6332 — keyed on ``status == "judge_blocked"`` (set by
    ``apply_judge_block``) rather than ``verdict == "fail"`` so a sync-mode
    turn withheld on a non-vetted ``unknown`` verdict (judge could not run)
    also raises the audit event, consistent with the fail-closed block.

    F-023-01 (round 2) — the reasoning is withheld from the
    outbound payload, matching what a user surface would show."""
    if turn.status != "judge_blocked":
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


def _blocked_unknown_outcome() -> "JudgeOutcome":
    return JudgeOutcome(
        verdict="unknown",
        reasoning="Judge could not run: internal error.",
        metrics={},
    )


async def _fail_closed_judge_block(db, tenant_id, cfg, turn):
    """Bug-6332 (fail CLOSED) — withhold the answer when the sync-judge
    orchestration itself raises.

    The answer turn is committed as ``judge_pending`` (with the original answer
    text, but NON-releasable) BEFORE the sync judge runs (Bug-8290). ``run_judge``
    is written never to raise, but the surrounding orchestration (history load,
    rubric fetch, narration repair, cost recording, commit) can. If any of that
    raises, the durable row stays ``judge_pending`` — already withheld by
    ``_redact_trace``/``list_turns`` — so the original, unvetted answer never
    reaches the user even on a fully durable failure. This helper additionally
    forces the row to ``judge_blocked`` (the explicit block state) whenever the
    DB is reachable, and blocks the in-memory turn for the immediate
    caller/stream regardless — upholding "no unvetted answer reaches the user"
    guarantee.

    This forces a block: it synthesizes an ``unknown`` verdict and runs
    ``apply_judge_block`` (which blocks any non-releasable verdict in sync mode)
    so the turn becomes ``judge_blocked`` with the original stashed for admin
    trace only.

    The authoritative block is performed on a FRESH tenant DB session — the
    triggering exception may itself be DB-layer (a broken connection / failed
    commit), which would poison the caller's session and make a re-use of it
    raise again (R2 finding). This mirrors the sibling post-pipeline error
    handlers, which also open a fresh session to finalize the row. The caller's
    session is rolled back best-effort so it stays usable for the common
    (non-DB) fault. The function NEVER raises: on total DB failure it still
    mutates the in-memory turn so the response/stream withholds the answer.
    Returns the turn to serve (fresh, blocked when persisted; else the
    in-memory-blocked original)."""
    try:
        await db.rollback()
    except Exception:
        logger.warning("Rollback of the caller session failed during fail-closed block")

    try:
        async for fdb in get_tenant_db(tenant_id):
            fresh = await fdb.get(AgentTurn, turn.id)
            if fresh is None:
                break
            outcome = _blocked_unknown_outcome()
            fresh.judge_verdict = outcome.verdict
            fresh.judge_reasoning = outcome.reasoning
            fresh.judge_metrics = outcome.metrics
            apply_judge_block(cfg, fresh, outcome)
            await fdb.commit()
            await fdb.refresh(fresh)
            return fresh
    except Exception:
        logger.exception("Fail-closed judge block could not persist the block")

    # Last resort: the DB is unusable. Block the in-memory turn so the response
    # and any emitted event still withhold the original answer. Bug-8290 — the
    # durable row was committed ``judge_pending`` (non-releasable) before the
    # judge ran, so even on a total DB outage it stays ``judge_pending`` =
    # withheld by ``_redact_trace``/``list_turns`` (never a retrievable ``ok``);
    # this in-memory block additionally hardens the immediate caller/stream.
    try:
        outcome = _blocked_unknown_outcome()
        turn.judge_verdict = outcome.verdict
        turn.judge_reasoning = outcome.reasoning
        turn.judge_metrics = outcome.metrics
        apply_judge_block(cfg, turn, outcome)
    except Exception:
        logger.exception("Fail-closed in-memory judge block also failed")
    return turn


_DEFAULT_JUDGE_HISTORY_LIMIT = 20


async def _prior_turns_for_judge(
    db,
    conversation_id: UUID,
    exclude_turn_id: UUID | None = None,
    *,
    max_turns: int = _DEFAULT_JUDGE_HISTORY_LIMIT,
) -> list[dict[str, str]]:
    """Return prior turns as a flat list of {role, content} dicts.

    Bug-5957 -- previously the full conversation history was loaded and
    sent to the judge LLM without any bound.  A long-running
    conversation could exceed the model's context window or waste tokens.
    The ``max_turns`` parameter (default 20, aligned with
    ``session_history_depth``) applies a sliding window so only the most
    recent turns are included.

    Bug-6011 follow-up -- the window is also applied at the SQL level
    (``ORDER BY turn_index DESC LIMIT``) instead of fetching every row and
    slicing in Python, so an old, very long conversation no longer forces
    an unbounded DB fetch on every judge call."""
    if max_turns > 0:
        # Fetch one extra row so that, if the excluded (just-persisted)
        # turn falls inside the window, we still end up with max_turns
        # rows after filtering it out below.
        q = await db.execute(
            select(AgentTurn)
            .where(AgentTurn.conversation_id == conversation_id)
            .order_by(AgentTurn.turn_index.desc())
            .limit(max_turns + 1)
        )
        turns = list(q.scalars().all())
        turns.reverse()  # back to chronological order
    else:
        q = await db.execute(
            select(AgentTurn)
            .where(AgentTurn.conversation_id == conversation_id)
            .order_by(AgentTurn.turn_index)
        )
        turns = list(q.scalars().all())
    if exclude_turn_id:
        turns = [t for t in turns if t.id != exclude_turn_id]
    if max_turns > 0 and len(turns) > max_turns:
        turns = turns[-max_turns:]
    history: list[dict[str, str]] = []
    for t in turns:
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
    system_sections: list[tuple[str, str]] | None = None,
    grounding_matches: str | None = None,
    date_anchor: str | None = None,
) -> None:
    """Background-task entry: open a fresh tenant DB session, run the
    judge LLM, write the verdict to the turn row.

    R2 (F2) — ``system_sections`` / ``grounding_matches`` are captured from the
    planner bundle at dispatch time and forwarded to run_judge so the async
    judge scores against the distilled evidence pack (and sees the retrieved
    glossary cards the planner used). Absent them, run_judge falls back to the
    verbatim ``system_prompt`` — never less evidence.

    Integration fix — ``date_anchor`` (the per-turn ``## DATE ANCHOR`` text Lane
    G moved into the user suffix) is likewise captured at dispatch and forwarded
    so the async judge anchors relative-date verification on the same current
    date the planner used.
    """
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
                max_turns=getattr(cfg_row, "session_history_depth", _DEFAULT_JUDGE_HISTORY_LIMIT) or _DEFAULT_JUDGE_HISTORY_LIMIT,
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
                system_sections=system_sections,
                grounding_matches=grounding_matches,
                date_anchor=date_anchor,
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
    system_sections: list[tuple[str, str]] | None = None,
    grounding_matches: str | None = None,
    date_anchor: str | None = None,
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
        system_sections=system_sections,
        grounding_matches=grounding_matches,
        date_anchor=date_anchor,
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
    idempotency_key: Optional[str] = Header(
        default=None, alias="Idempotency-Key", max_length=64
    ),
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
        cfg = await _require_project_access_and_agent(db, project_id, current_user)
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
        # Bug-6521 — reserve idempotently: a retried POST carrying the same
        # Idempotency-Key must not mint a second turn.
        reservation = await _reserve_turn(
            db,
            conversation_id,
            user_message=body.text,
            idempotency_key=idempotency_key,
        )
        if reservation.is_duplicate and reservation.existing_turn_id is not None:
            # The turn already ran to completion for this key. Return the
            # persisted result instead of re-running the pipeline.
            existing_turn = await db.get(AgentTurn, reservation.existing_turn_id)
            if existing_turn is not None:
                persona_name = await _resolve_persona_name(db, project_id, conv.persona_id)
                return _redact_trace(
                    existing_turn, cfg,
                    persona_id=conv.persona_id,
                    persona_name=persona_name,
                )
        next_idx = reservation.turn_index

        # Bug-7366 -- pessimistic budget reservation (committed in its own
        # short transaction so concurrent sessions see it).
        budget_reservation_id = None
        if getattr(cfg, "daily_token_budget", 0) > 0 or getattr(cfg, "daily_cost_budget_usd", 0) > 0:
            budget_reservation_id = await reserve_budget(
                current_user.tenant_id, cfg.project_id,
                provider="unknown",
                max_output_tokens=4096,
            )

        try:
            outcome = await run_turn(
                db=db,
                cfg=cfg,
                conversation=conv,
                user_message=body.text,
                jwt_token=jwt_token,
                embed_model_ids=(
                    current_user.model_ids
                    if isinstance(current_user, CurrentEmbedUser)
                    else None
                ),
                budget_reservation_id=budget_reservation_id,
            )
        except BaseException as exc:
            # F-023-10/11 -- finalize the placeholder as an error row.
            logger.exception("Sync pipeline failed: %s", type(exc).__name__)
            await reconcile_budget_reservation(current_user.tenant_id, budget_reservation_id)
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
                    # Bug-5957 -- do not expose exception class name
                    # in the persisted answer_text.
                    placeholder.answer_text = (
                        "An internal error occurred while processing "
                        "your question. Please try again."
                    )
                    placeholder.latency_ms = int((time.monotonic() - started) * 1000)
                    await err_db.commit()
                break
            # Bug-6010 -- return 503 for LLM timeouts instead of a
            # generic 500 so callers can distinguish transient provider
            # issues from internal server errors.
            if _is_llm_timeout(exc):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=(
                        "The language model provider did not respond in time. "
                        "The service may be temporarily overloaded -- "
                        "please try again shortly."
                    ),
                )
            raise HTTPException(status_code=500, detail="Pipeline error")

        # Bug-7366 -- delete the reservation; record_turn_cost writes real cost.
        await reconcile_budget_reservation(current_user.tenant_id, budget_reservation_id)

        turn = await persist_turn(
            db=db,
            conversation=conv,
            turn_index=next_idx,
            user_message=body.text,
            outcome=outcome,
            started_monotonic=started,
            cfg=cfg,
            llm_provider=outcome.provider or "",
            budget_reservation_id=budget_reservation_id,
        )
        conv.last_active_at = func.now()
        # F-023-01 (round 2) — in sync judge mode the turn.completed
        # webhook must not leave before the verdict: a turn the judge is
        # about to block would otherwise ship the unredacted original to
        # the webhook_url. Deferred to after apply_judge_block below.
        sync_judge_pending = outcome.status == "ok" and cfg.judge_mode == "sync"
        # Bug-8290 — persist the answer turn in the NON-releasable
        # ``judge_pending`` state instead of ``ok`` BEFORE this commit, so the
        # durable row that becomes user-reachable (via list_turns or an
        # idempotency-key replay) during the judge window exposes no unvetted
        # answer. The sync judge path below flips it to ``ok`` (release) on
        # pass/warn or ``judge_blocked`` on block/unknown; any failure leaves it
        # ``judge_pending`` = withheld (durable fail-closed, closing the Bug-6587
        # total-DB-outage residual). Async mode is unchanged (stays ``ok``).
        if sync_judge_pending:
            turn.status = "judge_pending"
        await db.commit()
        await db.refresh(turn)
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
                    # Bug-6575 — the judge's rebuilt system prompt must reflect
                    # the same embed model-scope the planner saw, so restricted
                    # model metadata never reaches the judge LLM either.
                    embed_model_ids=(
                        current_user.model_ids
                        if isinstance(current_user, CurrentEmbedUser)
                        else None
                    ),
                )
                system_prompt = bundle.system
                # R2 (F2) — carry the section breakdown + retrieved glossary
                # cards so run_judge can distil the evidence pack and the judge
                # sees the same grounding the planner did (intake fix).
                judge_sections = bundle.system_sections
                judge_grounding = bundle.grounding_matches
                # Integration fix — carry the per-turn DATE ANCHOR so the judge
                # anchors date-range verification on the planner's current date.
                judge_date_anchor = bundle.date_anchor
            except Exception:
                logger.exception("Could not rebuild prompt for judge")
                system_prompt = ""
                judge_sections = None
                judge_grounding = None
                judge_date_anchor = None

            if cfg.judge_mode == "sync":
              # Bug-6332 / Bug-8290 (fail CLOSED) — the turn is already committed
              # as ``judge_pending`` (durable but NON-releasable) with the
              # original answer; any exception in the sync-judge orchestration
              # below — INCLUDING the judge-history prep — leaves the durable row
              # ``judge_pending`` = withheld, and on failure we additionally force
              # an explicit block instead of surfacing a 500 with a retrievable
              # original. On the success path this branch flips the row to ``ok``
              # (pass/warn, via apply_judge_block) or ``judge_blocked``
              # (block/unknown). (The judge history is computed inside this guard;
              # the async branch recomputes it in ``_judge_turn_background``.)
              try:
                conversation_history = await _prior_turns_for_judge(
                    db, conv.id, exclude_turn_id=turn.id,
                    max_turns=getattr(cfg, "session_history_depth", _DEFAULT_JUDGE_HISTORY_LIMIT) or _DEFAULT_JUDGE_HISTORY_LIMIT,
                )
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
                    system_sections=judge_sections,
                    grounding_matches=judge_grounding,
                    date_anchor=judge_date_anchor,
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
                    system_sections=judge_sections,
                    grounding_matches=judge_grounding,
                    date_anchor=judge_date_anchor,
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
                # D1.1 — sync mode: block before commit if verdict=fail;
                # Bug-6332 — also block a non-vetted (unknown) verdict.
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
              except Exception:
                logger.exception(
                    "Sync judge orchestration failed — withholding answer (fail closed)"
                )
                turn = await _fail_closed_judge_block(
                    db, current_user.tenant_id, cfg, turn
                )
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
                    judge_sections,
                    judge_grounding,
                    judge_date_anchor,
                )

        persona_name = await _resolve_persona_name(db, project_id, conv.persona_id)
        return _redact_trace(
            turn, cfg,
            persona_id=conv.persona_id,
            persona_name=persona_name,
        )
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
        await _require_project_access_and_agent(db, project_id, current_user)
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


async def _replay_turn_into_publisher(
    tenant_id: str,
    project_id: UUID,
    conversation_id: UUID,
    turn_id: UUID,
    publisher: EventPublisher,
) -> None:
    """Bug-6521 — stream an already-completed turn's persisted result over a
    fresh SSE connection, WITHOUT re-running the pipeline.

    Used when a retried stream POST carries an Idempotency-Key whose turn ran to
    completion server-side (the client just missed the events). Emits the same
    redacted ``turn.completed`` shape the live path emits so the retry renders
    the identical answer; a persisted error turn is surfaced as ``turn.error``.
    Never re-executes and never mutates the turn — this is a pure replay."""
    try:
        async for db in get_tenant_db(tenant_id):
            cfg_row = await db.execute(
                select(ProjectAgentConfig).where(
                    ProjectAgentConfig.project_id == project_id
                )
            )
            cfg = cfg_row.scalar_one_or_none()
            turn = await db.get(AgentTurn, turn_id)
            if cfg is None or turn is None or turn.conversation_id != conversation_id:
                await publisher.emit("turn.error", reason="turn_not_found")
                return
            if turn.status == "error":
                await publisher.emit(
                    "turn.error",
                    reason="pipeline_error",
                    detail=(
                        "An internal error occurred while processing "
                        "your question. Please try again."
                    ),
                )
                return
            conv = await db.get(AgentConversation, conversation_id)
            persona_id = conv.persona_id if conv is not None else None
            persona_name = await _resolve_persona_name(db, project_id, persona_id)
            redacted = _redact_trace(
                turn, cfg, persona_id=persona_id, persona_name=persona_name,
            )
            # Bug-8290 — a retried stream POST whose keyed turn is still
            # ``judge_pending`` (the in-flight original's sync-judge verdict has
            # not resolved) must NOT replay the unvetted answer. Emit the redacted
            # answer_text (withheld to None for judge_pending) and surface the
            # true pending state so the client keeps waiting for the verdict
            # rather than rendering the original. The verdict is delivered by the
            # in-flight original's own post-judge events / a later fetch.
            await publisher.emit(
                "turn.completed",
                turn_id=str(turn.id),
                turn_index=turn.turn_index,
                status=turn.status,
                answer_text=redacted.answer_text,
                latency_ms=turn.latency_ms,
                citations=redacted.citations,
                thought_summary=redacted.thought_summary,
                rendered_output=redacted.rendered_output,
                chart_type=redacted.chart_type,
                semantic_query=redacted.semantic_query,
                routed_sql=redacted.routed_sql,
                route=turn.route,
                judge_pending=(turn.status == "judge_pending"),
                result_sample=redacted.query_result_sample,
                provider=None,
                persona_id=str(persona_id) if persona_id else None,
                persona_name=persona_name,
                calculation_steps=redacted.calculation_steps,
            )
            return
    except Exception:
        logger.exception("Idempotent replay of turn %s failed", turn_id)
        try:
            await publisher.emit(
                "turn.error",
                reason="pipeline_error",
                detail=(
                    "An internal error occurred while processing "
                    "your question. Please try again."
                ),
            )
        except Exception:
            logger.warning("Failed to emit replay error event for turn %s", turn_id)
    finally:
        await publisher.close()


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
    embed_model_ids: list[str] | None = None,
) -> None:
    """Open a fresh tenant DB session, run the pipeline against the
    publisher, persist the turn, run the judge (sync or scheduled),
    finally close the publisher.

    Bug-6575 — ``embed_model_ids`` carries the embed token's model_ids
    allow-list (captured on the request thread from ``current_user`` before
    this background task starts, since ``current_user`` is not available
    here) so prompt assembly can keep restricted model metadata out of the
    LLM prompt."""
    # Bug-7366 -- initialize before try so the error handler can always
    # reference it (Fable R2 finding 1: UnboundLocalError).
    _stream_budget_res_id = None
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

                # Bug-7366 -- pessimistic budget reservation (streaming).
                if getattr(cfg, "daily_token_budget", 0) > 0 or getattr(cfg, "daily_cost_budget_usd", 0) > 0:
                    _stream_budget_res_id = await reserve_budget(
                        tenant_id, cfg.project_id,
                        provider="unknown",
                        max_output_tokens=4096,
                    )

                outcome = await run_turn(
                    db=db,
                    cfg=cfg,
                    conversation=conv,
                    user_message=user_message,
                    jwt_token=jwt_token,
                    publisher=publisher,
                    embed_model_ids=embed_model_ids,
                    budget_reservation_id=_stream_budget_res_id,
                )

                await reconcile_budget_reservation(tenant_id, _stream_budget_res_id)

                turn = await persist_turn(
                    db=db,
                    conversation=conv,
                    turn_index=turn_index,
                    user_message=user_message,
                    outcome=outcome,
                    started_monotonic=started,
                    cfg=cfg,
                    llm_provider=outcome.provider or "",
                    budget_reservation_id=_stream_budget_res_id,
                )
                conv.last_active_at = func.now()
                if turn_index == 0 and not conv.title:
                    conv.title = user_message[:200]

                # In sync judge mode we must run the judge BEFORE emitting
                # turn.completed — otherwise the streaming UI flashes the
                # original answer, then swaps it for the block message. In
                # async mode the judge runs after the stream closes, so we
                # emit completed immediately.
                run_sync_judge = (
                    outcome.status == "ok" and cfg.judge_mode == "sync"
                )
                # Bug-8290 — commit the answer turn in the NON-releasable
                # ``judge_pending`` state instead of ``ok`` so the durable row is
                # never user-reachable (list_turns / idempotency replay) with an
                # unvetted answer during the judge window. The sync-judge block
                # below flips it to ``ok`` (release) on pass/warn or
                # ``judge_blocked`` on block/unknown; any failure leaves it
                # ``judge_pending`` = withheld (durable fail-closed). The stream
                # itself still buffers the answer via _SyncVerdictGate, so this
                # only hardens the persisted row. Async mode is unchanged.
                if run_sync_judge:
                    turn.status = "judge_pending"
                await db.commit()
                await db.refresh(turn)

                # Bug-5952 -- resolve persona name once, used in both
                # sync and async judge branches below.
                _persona_name = await _resolve_persona_name(db, project_id, conv.persona_id)

                if not run_sync_judge:
                    # F-023-01 (round 2) — in sync mode the turn.completed
                    # webhook must not leave before the verdict; it is
                    # deferred to after apply_judge_block in that branch.
                    _emit_turn_webhook(tenant_id, project_id, turn, cfg)
                    judge_pending = (
                        outcome.status == "ok"
                        and cfg.judge_mode == "async"
                    )
                    redacted = _redact_trace(
                        turn, cfg,
                        persona_id=conv.persona_id,
                        persona_name=_persona_name,
                    )
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
                        persona_id=str(conv.persona_id) if conv.persona_id else None,
                        persona_name=_persona_name,
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
                        # Bug-6575 — keep the streaming judge's system prompt on
                        # the same embed model-scope as the planner.
                        embed_model_ids=embed_model_ids,
                    )
                    system_prompt = bundle.system
                    # R2 (F2) — section breakdown + retrieved cards for the
                    # distilled evidence pack (intake fix).
                    judge_sections = bundle.system_sections
                    judge_grounding = bundle.grounding_matches
                    # Integration fix — per-turn DATE ANCHOR for the judge.
                    judge_date_anchor = bundle.date_anchor
                except Exception:
                    logger.exception("Could not rebuild prompt for judge")
                    system_prompt = ""
                    judge_sections = None
                    judge_grounding = None
                    judge_date_anchor = None

                if run_sync_judge:
                  # Bug-6332 / Bug-8290 (fail CLOSED) — the turn is already
                  # committed as ``judge_pending`` (durable but NON-releasable);
                  # a sync-judge exception leaves that durable row withheld and
                  # must not surface the unvetted answer. The judge-history prep
                  # is computed INSIDE this guard so a history-load failure also
                  # fails closed. On success this flips the row to ``ok``
                  # (pass/warn) or ``judge_blocked``; on failure, force a block
                  # and emit only redacted/blocked events rather than surfacing
                  # the original. (The async branch
                  # recomputes history in ``_judge_turn_background``.)
                  try:
                    conversation_history = await _prior_turns_for_judge(
                        db, conv.id, exclude_turn_id=turn.id,
                        max_turns=getattr(cfg, "session_history_depth", _DEFAULT_JUDGE_HISTORY_LIMIT) or _DEFAULT_JUDGE_HISTORY_LIMIT,
                    )
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
                        system_sections=judge_sections,
                        grounding_matches=judge_grounding,
                        date_anchor=judge_date_anchor,
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
                    redacted = _redact_trace(
                        turn, cfg,
                        persona_id=conv.persona_id,
                        persona_name=_persona_name,
                    )
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
                        persona_id=str(conv.persona_id) if conv.persona_id else None,
                        persona_name=_persona_name,
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
                  except Exception:
                    # Bug-6332 / Bug-8290 (fail CLOSED) — withhold the original
                    # answer and emit a blocked turn.completed instead of a bare
                    # error. The durable row was committed ``judge_pending``, so
                    # a failure here leaves it withheld (never a retrievable
                    # unvetted answer); the block additionally hardens the stream.
                    logger.exception(
                        "Sync judge orchestration failed (stream) — "
                        "withholding answer (fail closed)"
                    )
                    turn = await _fail_closed_judge_block(db, tenant_id, cfg, turn)
                    _emit_turn_webhook(tenant_id, project_id, turn, cfg)
                    _emit_judge_webhook(tenant_id, project_id, turn, cfg)
                    redacted = _redact_trace(
                        turn, cfg,
                        persona_id=conv.persona_id,
                        persona_name=_persona_name,
                    )
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
                        persona_id=str(conv.persona_id) if conv.persona_id else None,
                        persona_name=_persona_name,
                    )
                    await publisher.emit(
                        "turn.judged",
                        turn_id=str(turn.id),
                        verdict="unknown",
                        reasoning=redacted.judge_reasoning,
                        metrics={},
                        status=turn.status,
                        answer_text=turn.answer_text,
                    )
                    return
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
                            system_sections=judge_sections,
                            grounding_matches=judge_grounding,
                            date_anchor=judge_date_anchor,
                        )
                    )
                return
            except BaseException as exc:
                logger.exception("Streaming pipeline failed: %s", type(exc).__name__)
                # Bug-7366 -- reconcile the reservation on the error path.
                await reconcile_budget_reservation(tenant_id, _stream_budget_res_id)
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
                            # Bug-5957 — do not expose exception class name
                            # in the persisted answer_text.
                            placeholder.answer_text = (
                                "An internal error occurred while processing "
                                "your question. Please try again."
                            )
                            placeholder.latency_ms = int((time.monotonic() - started) * 1000)
                            await err_db.commit()
                        break
                except Exception:
                    logger.warning("Failed to finalize streaming placeholder for turn_index=%d", turn_index)
                # Bug-6010 -- distinguish LLM provider timeouts from
                # other pipeline errors in SSE events.
                # Bug-5957 — do not expose raw exception strings or
                # class names in SSE events sent to end users.
                if _is_llm_timeout(exc):
                    await publisher.emit(
                        "turn.error",
                        reason="llm_timeout",
                        detail=(
                            "The language model provider did not respond "
                            "in time. Please try again shortly."
                        ),
                    )
                else:
                    logger.error(
                        "Streaming pipeline error detail (not sent to user): %s: %s",
                        type(exc).__name__, exc,
                    )
                    await publisher.emit(
                        "turn.error",
                        reason="pipeline_error",
                        detail=(
                            "An internal error occurred while processing "
                            "your question. Please try again."
                        ),
                    )
    finally:
        await publisher.close()


@router.post("/{conversation_id}/messages/stream")
async def send_message_stream(
    project_id: UUID,
    conversation_id: UUID,
    body: MessageSend,
    current_user: CurrentUser = Depends(require_capability("chat")),
    idempotency_key: Optional[str] = Header(
        default=None, alias="Idempotency-Key", max_length=64
    ),
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
    # Bug-6521 — reserve idempotently so a retried stream POST (or the
    # conv-client sync fallback) carrying the same Idempotency-Key does not mint
    # a second turn. A completed turn for the key is replayed over a fresh
    # stream instead of re-running the pipeline.
    next_idx: int = 0
    replay_turn_id: Optional[UUID] = None
    async for db in get_tenant_db(current_user.tenant_id):
        await _require_project_access_and_agent(db, project_id, current_user)
        conv = await db.get(AgentConversation, conversation_id)
        if conv is None or conv.project_id != project_id:
            raise HTTPException(status_code=404, detail="Conversation not found")
        _enforce_conversation_ownership(conv, current_user)
        reservation = await _reserve_turn(
            db,
            conversation_id,
            user_message=body.text,
            idempotency_key=idempotency_key,
        )
        next_idx = reservation.turn_index
        if reservation.is_duplicate:
            replay_turn_id = reservation.existing_turn_id
        break

    if replay_turn_id is not None:
        # Duplicate of a completed turn: stream the persisted result without
        # re-running the pipeline.
        publisher = EventPublisher()
        replay_task = asyncio.create_task(
            _replay_turn_into_publisher(
                tenant_id=current_user.tenant_id,
                project_id=project_id,
                conversation_id=conversation_id,
                turn_id=replay_turn_id,
                publisher=publisher,
            )
        )

        async def _replay_stream():
            try:
                async for item in publisher.drain(
                    heartbeat_interval=_HEARTBEAT_INTERVAL_SEC
                ):
                    if item is _HEARTBEAT_SENTINEL:
                        yield format_heartbeat()
                        continue
                    yield format_sse(item)
            finally:
                if not replay_task.done():
                    replay_task.cancel()
                yield format_raw("turn.stream.closed")

        return StreamingResponse(
            _replay_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

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
            embed_model_ids=(
                current_user.model_ids
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
