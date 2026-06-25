"""Tenant-admin maintenance endpoints for the agent service.

Currently exposes:

  GET  /admin/agent/retention        — read per-project retention days
  POST /admin/agent/retention/cleanup — soft-delete inactive conversations

Both require tenant-admin auth. The cleanup endpoint is idempotent and
safe to wire into a cron job (scheduler service or external) without
extra coordination — it only touches rows whose `last_active_at` is
older than the resolved retention window and whose `deleted_at` is null.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, func as sa_func, select

from shared.agent.retention import (
    DEFAULT_RETENTION_DAYS as _DEFAULT_RETENTION_DAYS,
    soft_delete_inactive_conversations,
)
from shared.db.models import AgentConversation, AgentTurn, ProjectAgentConfig
from shared.db.session import get_tenant_db
from src.auth.middleware import CurrentUser, require_tenant_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/agent", tags=["agent-admin"])


class RetentionInfo(BaseModel):
    retention_days: int


class CleanupResult(BaseModel):
    retention_days: int
    cutoff_utc: datetime
    soft_deleted: int


async def _resolve_retention(tenant_id: str, project_id: UUID) -> int:
    """Read retention from ProjectAgentConfig for the given project."""
    async for db in get_tenant_db(tenant_id):
        result = await db.execute(
            select(ProjectAgentConfig.conversation_retention_days).where(
                ProjectAgentConfig.project_id == project_id
            )
        )
        row = result.scalar_one_or_none()
        days = row if row is not None else _DEFAULT_RETENTION_DAYS
        if days <= 0:
            raise HTTPException(
                status_code=500,
                detail=f"conversation_retention_days must be > 0 (got {days})",
            )
        return days
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.get("/retention", response_model=RetentionInfo)
async def get_retention(
    project_id: UUID = Query(...),
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> RetentionInfo:
    days = await _resolve_retention(current_user.tenant_id, project_id)
    return RetentionInfo(retention_days=days)


@router.post("/retention/cleanup", response_model=CleanupResult)
async def cleanup_inactive_conversations(
    project_id: UUID = Query(...),
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> CleanupResult:
    days = await _resolve_retention(current_user.tenant_id, project_id)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    soft_deleted = 0
    # F-023-27: the soft-delete logic is shared with the scheduler sweep so
    # both run identical behaviour (one source of truth).
    async for db in get_tenant_db(current_user.tenant_id):
        soft_deleted = await soft_delete_inactive_conversations(db, project_id, days)
        if soft_deleted:
            await db.commit()
        logger.info(
            "agent retention cleanup tenant=%s project=%s retention_days=%s cutoff=%s soft_deleted=%s",
            current_user.tenant_id, project_id, days, cutoff.isoformat(), soft_deleted,
        )
    return CleanupResult(
        retention_days=days,
        cutoff_utc=cutoff,
        soft_deleted=soft_deleted,
    )


# ---------------------------------------------------------------------------
# Conversation history purge
# ---------------------------------------------------------------------------


class PurgeResponse(BaseModel):
    deleted_conversations: int
    older_than_days: int


class ConversationStats(BaseModel):
    total: int
    oldest_at: datetime | None


@router.get("/conversations/stats", response_model=ConversationStats)
async def conversation_stats(
    project_id: UUID = Query(...),
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> ConversationStats:
    async for db in get_tenant_db(current_user.tenant_id):
        total = await db.scalar(
            select(sa_func.count(AgentConversation.id)).where(
                AgentConversation.project_id == project_id,
                AgentConversation.deleted_at.is_(None),
            )
        )
        oldest = await db.scalar(
            select(sa_func.min(AgentConversation.started_at)).where(
                AgentConversation.project_id == project_id,
                AgentConversation.deleted_at.is_(None),
            )
        )
        return ConversationStats(total=total or 0, oldest_at=oldest)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.delete("/conversations/purge", response_model=PurgeResponse)
async def purge_conversations(
    project_id: UUID = Query(...),
    older_than_days: int = Query(..., ge=0),
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> PurgeResponse:
    """Hard-delete conversations and their turns.

    older_than_days=0 deletes ALL conversations for the project.
    older_than_days>0 deletes conversations whose last activity is older
    than the threshold.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        conditions = [AgentConversation.project_id == project_id]
        if older_than_days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
            conditions.append(AgentConversation.last_active_at < cutoff)

        conv_ids_q = await db.execute(
            select(AgentConversation.id).where(*conditions)
        )
        conv_ids = [row[0] for row in conv_ids_q.all()]

        if conv_ids:
            await db.execute(
                sa_delete(AgentTurn).where(
                    AgentTurn.conversation_id.in_(conv_ids)
                )
            )
            await db.execute(
                sa_delete(AgentConversation).where(
                    AgentConversation.id.in_(conv_ids)
                )
            )
            await db.commit()

        logger.info(
            "agent purge tenant=%s project=%s older_than_days=%s deleted=%s",
            current_user.tenant_id,
            project_id,
            older_than_days,
            len(conv_ids),
        )
        return PurgeResponse(
            deleted_conversations=len(conv_ids),
            older_than_days=older_than_days,
        )
    raise HTTPException(status_code=500, detail="DB session exhausted")
