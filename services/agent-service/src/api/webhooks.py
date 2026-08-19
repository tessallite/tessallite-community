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
)
from shared.db.session import get_tenant_db
# Bug-8350 R2 MED-2 — read-time redaction backstop for GET /dlq (see
# list_dlq below): last_error is scrubbed at write time already, but a row
# written before that fix shipped, or a future write-path regression,
# should never be able to leak a URL through this endpoint.
from shared.webhooks.redact import scrub_url_from_text
# Bug-8411 — single source of truth for the agent event catalogue the
# Settings event-subscription checkboxes render.
from shared.webhooks.agent_event_types import agent_event_catalogue
# Bug-8356 — reuse the service's canonical STRICT project-scoped gate rather
# than adding a sixth hand-rolled copy of the binding lookup. See
# ``_require_webhook_project_access`` below for why the strict tier applies.
from src.api.agent_config import _require_blocked_original_access
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.webhooks.dispatcher import dispatch_event

logger = logging.getLogger(__name__)


async def _require_webhook_project_access(
    project_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    """Bug-8356 — authorize a webhook management call against the REQUESTED
    project, and do it once for the whole router.

    Two things were wrong before, and the second one is why this is a
    router-level dependency rather than a corrected per-handler call:

    1. The gate this module used to define (``_require_modeller``) queried
       ``UserAccessBinding`` with no ``project_id`` predicate at all, even
       though every route here is declared under
       ``/projects/{project_id}/agent/webhook``. Any user holding an
       ``admin``/``modeler`` binding on ANY project in the tenant could
       therefore rotate a DIFFERENT project's signing secret (silently
       breaking that project's receiver), read its dead-letter queue, replay
       it, or delete it — a cross-project IDOR.

    2. The reason a fifth copy of that gate existed at all is that this
       service re-applies authorization by hand in every handler, so a copy
       can drift (as this one did) and a newly added route can ship with no
       gate whatsoever and nothing fails. Attaching the gate to the
       ``APIRouter`` via ``dependencies=[...]`` closes the whole prefix by
       construction: a future route added to this router is authorized
       whether or not its author remembers, and ``TestWebhookRouterGate``
       enumerates the app's routes by PATH (not by module) so a second
       router mounted under the same prefix cannot slip past either.

    The STRICT tier (``_require_blocked_original_access``) is used, not the
    bootstrap-open configuration tier:

    * ``GET /dlq`` returns ``DlqRow.payload``, and the agent's ``turn.*``
      events carry ``user_message`` and ``answer_text``
      (``conversations._emit_turn_webhook``) — this is a CONTENT surface.
      F-023-01 round 2 already settled the rule for this service: the
      zero-bindings bootstrap-open posture (decision D2) "covers
      configuration endpoints during first-run setup only — it must not gate
      content disclosure, because a binding-less tenant would expose
      [content] to every authenticated user."
    * ``rotate-secret`` invalidates a credential the operator has already
      shared with their receiver, and ``DELETE /dlq/{id}`` destroys evidence
      of undelivered events. Neither is a first-run convenience.

    Tenant admins and system admins still pass by role, so genuine first-run
    setup of a brand-new tenant is unaffected.
    """
    await _require_blocked_original_access(project_id, current_user)


router = APIRouter(
    prefix="/projects/{project_id}/agent/webhook",
    tags=["agent-webhook"],
    # Bug-8356 — every route under this prefix is authorized here, once.
    dependencies=[Depends(_require_webhook_project_access)],
)


class RotateSecretResponse(BaseModel):
    signing_secret: str  # Plaintext — shown only on rotation.


class EventTypeRow(BaseModel):
    value: str
    label: str


@router.get("/event-types", response_model=list[EventTypeRow])
async def list_event_types(
    project_id: UUID,
    # Declared (unused in the body) so the embed-token refusal is stated on
    # the route itself, not only inherited from the router dependency.
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[EventTypeRow]:
    """Bug-8411 — the catalogue of agent events a webhook can subscribe to.

    Served from ``shared/webhooks/agent_event_types.py``, the same module the
    dispatcher filters on and the config API validates against, so the
    Settings checkboxes can never offer (or omit) an event the backend does
    not actually emit. Mirrors model-service's
    ``GET /admin/webhooks/event-types`` for the platform-wide catalogue.
    """
    return [EventTypeRow(**row) for row in agent_event_catalogue()]


class DlqRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_type: str
    # Bug-8350 — the raw destination URL (which may embed a bearer token or
    # API key, in the path as much as the query string) is never returned.
    # `target_host` is a sanitised `scheme://host[:port]` hint only (no
    # path); a retry reloads the live URL from ProjectAgentConfig, so the
    # API surface never needs the plaintext.
    target_host: Optional[str]
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
    plaintext exactly once.

    Authorization is enforced by the router-level
    ``_require_webhook_project_access`` dependency (Bug-8356), which runs
    before this handler.
    """
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
    # Authorization: router-level `_require_webhook_project_access` (Bug-8356).
    async for db in get_tenant_db(current_user.tenant_id):
        # Bug-8357 — the backstop needs the project's configured URL, not just
        # a scheme-anchored sweep: a receiver that echoed only the
        # credential-bearing PATH (``/hooks/<token>``) carries the same secret
        # with no scheme for a pattern to anchor on. One extra row read.
        cfg_q = await db.execute(
            select(ProjectAgentConfig.webhook_url).where(
                ProjectAgentConfig.project_id == project_id
            )
        )
        configured_url = cfg_q.scalar_one_or_none()
        result = await db.execute(
            select(AgentWebhookDlq)
            .where(
                AgentWebhookDlq.project_id == project_id,
                AgentWebhookDlq.resolved_at.is_(None),
            )
            .order_by(desc(AgentWebhookDlq.last_attempted_at))
            .limit(200)
        )
        out: list[DlqRow] = []
        for r in result.scalars().all():
            dlq_row = DlqRow.model_validate(r)
            # Bug-8350 R2 MED-2 — read-time backstop, defense in depth on
            # top of the write-time scrub in dispatcher._persist_dlq.
            dlq_row.last_error = scrub_url_from_text(
                dlq_row.last_error, configured_url
            )
            out.append(dlq_row)
        return out
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.post("/dlq/{dlq_id}/retry", status_code=status.HTTP_204_NO_CONTENT)
async def retry_dlq(
    project_id: UUID,
    dlq_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    """Re-attempt one DLQ entry.

    Bug-8349 — the row is deliberately NOT marked resolved here. Resolution
    happens only inside ``dispatch_event`` (via ``source_dlq_id``), and only
    after a confirmed signed 2xx. The prior behaviour resolved the row
    before the background retry even ran, so a retry that failed again still
    looked "resolved" to the operator — the failure was invisible.

    Reviewer follow-up: an already-resolved row is rejected outright (409)
    rather than silently spawning a phantom resend. This does not close the
    full concurrent-duplicate-click race (two near-simultaneous retries of a
    still-unresolved row can both pass this check) -- that requires a row
    lock and is tracked separately as a low-severity follow-up; this check
    only rejects the case that is decidable up front (a row that is already
    known-resolved).

    Authorization is enforced by the router-level
    ``_require_webhook_project_access`` dependency (Bug-8356).
    """
    async for db in get_tenant_db(current_user.tenant_id):
        row = await db.get(AgentWebhookDlq, dlq_id)
        if row is None or row.project_id != project_id:
            raise HTTPException(status_code=404, detail="DLQ row not found")
        if row.resolved_at is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="DLQ entry is already resolved",
            )
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
                source_dlq_id=row.id,
            )
        )
        return


@router.delete("/dlq/{dlq_id}", status_code=status.HTTP_204_NO_CONTENT)
async def discard_dlq(
    project_id: UUID,
    dlq_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    # Authorization: router-level `_require_webhook_project_access` (Bug-8356).
    async for db in get_tenant_db(current_user.tenant_id):
        row = await db.get(AgentWebhookDlq, dlq_id)
        if row is None or row.project_id != project_id:
            raise HTTPException(status_code=404, detail="DLQ row not found")
        await db.delete(row)
        await db.commit()
