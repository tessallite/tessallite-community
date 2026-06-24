"""Outbound webhook dispatcher — Phase C2.

Public entry: ``dispatch_event(tenant_id, project_id, event_type, payload, *, conversation_id=None, turn_id=None)``.

Behaviour:
  - Reads the project's webhook URL + signing secret from ProjectAgentConfig.
  - Signs payload with HMAC-SHA256 over ``"<unix>.<json_body>"``;
    sets ``X-Tessallite-Signature: t=<unix>,v1=<hex>``.
  - Retries up to 3 times with 10 / 60 / 300 second back-off on
    network failure or 5xx response.
  - Drops bodies larger than 32 KB straight to the DLQ table without
    any HTTP attempt.
  - On exhausted retries, writes a row to ``agent_webhook_dlq``.

The dispatcher is invoked as a fire-and-forget asyncio task from the
turn pipeline / feedback endpoint; it opens its own DB session.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Optional
from uuid import UUID

import httpx
from sqlalchemy import select

from shared.db.models import AgentWebhookDlq, ProjectAgentConfig
from shared.db.session import get_tenant_db

logger = logging.getLogger(__name__)


_MAX_BODY_BYTES = 32 * 1024
_BACKOFF_SCHEDULE_SEC = (10, 60, 300)
_REQUEST_TIMEOUT_SEC = 10


def _decrypt_secret(encrypted: Optional[bytes]) -> Optional[str]:
    if not encrypted:
        return None
    try:
        # Rotation-aware decrypt: current key first, then any previous key.
        from shared.security.credential_crypto import decrypt_str

        return decrypt_str(encrypted)
    except Exception:
        logger.exception("Failed to decrypt webhook signing secret")
        return None


def _sign(secret: str, unix_ts: int, body_bytes: bytes) -> str:
    msg = f"{unix_ts}.".encode() + body_bytes
    digest = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return f"t={unix_ts},v1={digest}"


async def _post_once(
    target_url: str,
    body_bytes: bytes,
    sig_header: str,
    event_type: str,
) -> tuple[bool, Optional[int], Optional[str]]:
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SEC) as client:
            resp = await client.post(
                target_url,
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Tessallite-Signature": sig_header,
                    "X-Tessallite-Event": event_type,
                },
            )
        if 200 <= resp.status_code < 300:
            return True, resp.status_code, None
        return False, resp.status_code, f"HTTP {resp.status_code}"
    except httpx.HTTPError as exc:
        return False, None, str(exc)


async def _persist_dlq(
    tenant_id: str,
    project_id: UUID,
    conversation_id: Optional[UUID],
    turn_id: Optional[UUID],
    event_type: str,
    target_url: str,
    payload: dict[str, Any],
    attempts: int,
    last_status: Optional[int],
    last_error: Optional[str],
) -> None:
    try:
        async for db in get_tenant_db(tenant_id):
            row = AgentWebhookDlq(
                project_id=project_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                event_type=event_type,
                target_url=target_url,
                payload=payload,
                attempt_count=attempts,
                last_status_code=last_status,
                last_error=last_error,
            )
            db.add(row)
            await db.commit()
            return
    except Exception:
        logger.exception("Failed to persist webhook DLQ row")


async def dispatch_event(
    tenant_id: str,
    project_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    *,
    conversation_id: Optional[UUID] = None,
    turn_id: Optional[UUID] = None,
) -> None:
    """Fire-and-forget webhook dispatch. Safe to call from a background
    task. Never raises — logs and DLQs on failure."""
    target_url: Optional[str] = None
    secret: Optional[str] = None
    try:
        async for db in get_tenant_db(tenant_id):
            cfg_q = await db.execute(
                select(ProjectAgentConfig).where(
                    ProjectAgentConfig.project_id == project_id
                )
            )
            cfg = cfg_q.scalar_one_or_none()
            if cfg is None or not cfg.enabled:
                return
            target_url = cfg.webhook_url
            secret = _decrypt_secret(cfg.webhook_signing_secret)
            break
    except Exception:
        logger.exception("Webhook dispatch could not load config")
        return

    if not target_url:
        return  # No webhook configured.

    body = {
        "event_type": event_type,
        "project_id": str(project_id),
        "conversation_id": str(conversation_id) if conversation_id else None,
        "turn_id": str(turn_id) if turn_id else None,
        "payload": payload,
        "emitted_at": int(time.time()),
    }
    body_bytes = json.dumps(body, separators=(",", ":"), default=str).encode()

    if len(body_bytes) > _MAX_BODY_BYTES:
        await _persist_dlq(
            tenant_id, project_id, conversation_id, turn_id,
            event_type, target_url, body,
            attempts=0, last_status=None,
            last_error=f"payload too large ({len(body_bytes)} bytes)",
        )
        return

    unix_ts = int(time.time())
    sig_header = (
        _sign(secret, unix_ts, body_bytes) if secret else f"t={unix_ts},v1="
    )

    attempts = 0
    last_status: Optional[int] = None
    last_error: Optional[str] = None
    for delay in (0, *_BACKOFF_SCHEDULE_SEC):
        if delay:
            await asyncio.sleep(delay)
        attempts += 1
        ok, status_code, error = await _post_once(
            target_url, body_bytes, sig_header, event_type
        )
        last_status = status_code
        last_error = error
        if ok:
            return
        # 4xx (except 408/429) is a permanent client error — don't retry.
        if status_code is not None and 400 <= status_code < 500 and status_code not in (408, 429):
            break

    await _persist_dlq(
        tenant_id, project_id, conversation_id, turn_id,
        event_type, target_url, body,
        attempts=attempts, last_status=last_status, last_error=last_error,
    )
