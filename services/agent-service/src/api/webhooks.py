"""Webhook configuration + DLQ management — Phase C2.

Endpoints:
  POST   /projects/{project_id}/agent/webhook/rotate-secret  — generate a
         fresh signing secret, return plaintext once (the only time).
  GET    /projects/{project_id}/agent/webhook/dlq            — list DLQ rows.
  POST   /projects/{project_id}/agent/webhook/dlq/{dlq_id}/retry
                                                              — re-attempt one entry.
  DELETE /projects/{project_id}/agent/webhook/dlq/{dlq_id}    — discard.

The webhook URL itself is configured via PUT /agent/config (already
exposed); only the signing-secret lifecycle and the DLQ surface live
here.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import desc, select

from shared.db.models import (
    AgentWebhookDlq,
    ProjectAgentConfig,
    UserAccessBinding,
)
from shared.db.session import get_tenant_db
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.webhooks.dispatcher import dispatch_event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/projects/{project_id}/agent/webhook", tags=["agent-webhook"])


async def _require_modeller(current_user: CurrentUser) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(
            select(UserAccessBinding).where(
                UserAccessBinding.user_identity == current_user.user_id,
                # Bug-1082 — canonical role spelling only ("modeler"); the
                # interim accept-both was removed after confirming no tenant
                # schema stores a "modeller" binding.
                UserAccessBinding.role.in_(("admin", "modeler")),
            ).limit(1)
        )
        if result.scalar_one_or_none() is not None:
            return
        any_binding = await db.execute(select(UserAccessBinding).limit(1))
        if any_binding.scalar_one_or_none() is None:
            return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Modeller or Admin access required",
    )


class RotateSecretResponse(BaseModel):
    signing_secret: str  # Plaintext — shown only on rotation.


class DlqRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_type: str
    target_url: str
    attempt_count: int
    last_status_code: Optional[int]
    last_error: Optional[str]
    first_attempted_at: Any
    last_attempted_at: Any
    resolved_at: Any
    payload: dict


@router.post("/rotate-secret", response_model=RotateSecretResponse)
async def rotate_signing_secret(
    project_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> RotateSecretResponse:
    """Generate and persist a fresh HMAC signing secret. Returns the
    plaintext exactly once."""
    await _require_modeller(current_user)
    plaintext = secrets.token_urlsafe(32)
    # Rotation-aware: encrypts under the current key (the first rotation key).
    from shared.security.credential_crypto import encrypt_str
    encrypted = encrypt_str(plaintext)

    async for db in get_tenant_db(current_user.tenant_id):
        cfg_q = await db.execute(
            select(ProjectAgentConfig).where(
                ProjectAgentConfig.project_id == project_id
            )
        )
        cfg = cfg_q.scalar_one_or_none()
        if cfg is None:
            raise HTTPException(status_code=404, detail="Agent not configured")
        cfg.webhook_signing_secret = encrypted
        await db.commit()
        return RotateSecretResponse(signing_secret=plaintext)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.get("/dlq", response_model=list[DlqRow])
async def list_dlq(
    project_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[DlqRow]:
    await _require_modeller(current_user)
    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(
            select(AgentWebhookDlq)
            .where(
                AgentWebhookDlq.project_id == project_id,
                AgentWebhookDlq.resolved_at.is_(None),
            )
            .order_by(desc(AgentWebhookDlq.last_attempted_at))
            .limit(200)
        )
        return [DlqRow.model_validate(r) for r in result.scalars().all()]
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.post("/dlq/{dlq_id}/retry", status_code=status.HTTP_204_NO_CONTENT)
async def retry_dlq(
    project_id: UUID,
    dlq_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    await _require_modeller(current_user)
    async for db in get_tenant_db(current_user.tenant_id):
        row = await db.get(AgentWebhookDlq, dlq_id)
        if row is None or row.project_id != project_id:
            raise HTTPException(status_code=404, detail="DLQ row not found")
        from datetime import datetime, timezone

        row.resolved_at = datetime.now(timezone.utc)
        await db.commit()
        payload = row.payload.get("payload") if isinstance(row.payload, dict) else {}
        from src.api.conversations import _spawn_background
        _spawn_background(
            dispatch_event(
                tenant_id=current_user.tenant_id,
                project_id=project_id,
                event_type=row.event_type,
                payload=payload or {},
                conversation_id=row.conversation_id,
                turn_id=row.turn_id,
            )
        )


@router.delete("/dlq/{dlq_id}", status_code=status.HTTP_204_NO_CONTENT)
async def discard_dlq(
    project_id: UUID,
    dlq_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    await _require_modeller(current_user)
    async for db in get_tenant_db(current_user.tenant_id):
        row = await db.get(AgentWebhookDlq, dlq_id)
        if row is None or row.project_id != project_id:
            raise HTTPException(status_code=404, detail="DLQ row not found")
        await db.delete(row)
        await db.commit()
