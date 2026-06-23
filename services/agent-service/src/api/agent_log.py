"""Agent turn log — admin audit endpoint.

Endpoint:
  GET /projects/{project_id}/agent/log

Returns a paginated, filterable list of all turns across all
conversations for a project. Joins AgentTurn with AgentConversation
to include caller_ref (user email). Accessible to tenant admins and
project admins/modellers only.

Default ordering: most-recently-active conversation first, then turns
in chronological order within each conversation.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import desc, func, select

from shared.db.models import AgentConversation, AgentTurn, ProjectAgentConfig
from shared.db.session import get_tenant_db
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.api.agent_config import _require_blocked_original_access

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/projects/{project_id}/agent", tags=["agent-log"])


class LogTurnRow(BaseModel):
    turn_id: UUID
    conversation_id: UUID
    turn_index: int
    created_at: datetime
    caller_ref: str
    caller_kind: str
    user_message: str
    thought_summary: Optional[str] = None
    llm_plan: Optional[dict] = None
    semantic_query: Optional[dict] = None
    routed_sql: Optional[str] = None
    route: Optional[str] = None
    query_result_rows: Optional[int] = None
    answer_text: Optional[str] = None
    citations: Optional[list] = None
    judge_verdict: Optional[str] = None
    judge_reasoning: Optional[str] = None
    judge_metrics: Optional[dict] = None
    guardrail_actions: Optional[list] = None
    usage_input_tokens: int = 0
    usage_output_tokens: int = 0
    latency_ms: int = 0
    status: str = "ok"
    user_feedback: Optional[dict] = None
    prompt_messages: Optional[dict] = None
    llm_raw_response: Optional[str] = None


class LogResponse(BaseModel):
    items: list[LogTurnRow]
    total: int
    page: int
    page_size: int


@router.get("/log", response_model=LogResponse)
async def get_agent_log(
    project_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    caller_ref: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
) -> LogResponse:
    # F-023-01 (round 2) — the log row carries llm_plan (which holds
    # original_answer_blocked for blocked turns), so this surface uses the
    # strict fail-closed gate with NO zero-bindings bootstrap bypass.
    await _require_blocked_original_access(project_id, current_user)

    async for db in get_tenant_db(current_user.tenant_id):
        cfg_result = await db.execute(
            select(ProjectAgentConfig).where(
                ProjectAgentConfig.project_id == project_id
            )
        )
        cfg = cfg_result.scalar_one_or_none()
        if cfg is None or not cfg.enable_agent_log_screen:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Agent log screen not enabled",
            )

        base = (
            select(
                AgentTurn.id.label("turn_id"),
                AgentTurn.conversation_id,
                AgentTurn.turn_index,
                AgentTurn.created_at,
                AgentConversation.caller_ref,
                AgentConversation.caller_kind,
                AgentTurn.user_message,
                AgentTurn.thought_summary,
                AgentTurn.llm_plan,
                AgentTurn.semantic_query,
                AgentTurn.routed_sql,
                AgentTurn.route,
                AgentTurn.query_result_rows,
                AgentTurn.answer_text,
                AgentTurn.citations,
                AgentTurn.judge_verdict,
                AgentTurn.judge_reasoning,
                AgentTurn.judge_metrics,
                AgentTurn.guardrail_actions,
                AgentTurn.usage_input_tokens,
                AgentTurn.usage_output_tokens,
                AgentTurn.latency_ms,
                AgentTurn.status,
                AgentTurn.user_feedback,
                AgentTurn.prompt_messages,
                AgentTurn.llm_raw_response,
            )
            .join(
                AgentConversation,
                AgentTurn.conversation_id == AgentConversation.id,
            )
            .where(AgentConversation.project_id == project_id)
        )

        if date_from is not None:
            base = base.where(
                AgentTurn.created_at >= datetime.combine(
                    date_from, time.min, tzinfo=timezone.utc
                )
            )
        if date_to is not None:
            base = base.where(
                AgentTurn.created_at <= datetime.combine(
                    date_to, time.max, tzinfo=timezone.utc
                )
            )
        if caller_ref is not None:
            base = base.where(AgentConversation.caller_ref == caller_ref)
        if q is not None:
            base = base.where(AgentTurn.user_message.ilike(f"%{q}%"))

        count_q = select(func.count()).select_from(base.subquery())
        total = await db.scalar(count_q) or 0

        rows = await db.execute(
            base.order_by(
                desc(AgentConversation.last_active_at),
                AgentTurn.turn_index,
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )

        items = [LogTurnRow(**dict(row._mapping)) for row in rows.all()]

        is_tenant_admin = current_user.role == "tenant_admin"
        if not is_tenant_admin:
            for item in items:
                item.answer_text = None
                item.prompt_messages = None
                item.llm_raw_response = None
                item.routed_sql = None

        return LogResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
        )
    raise HTTPException(status_code=500, detail="DB session exhausted")
