"""Platform-wide outbound webhook dispatcher.

HMAC-SHA256 signing, bounded retries with exponential backoff, DLQ on
exhausted retries, oversize guard, and SSRF protection (F-022-05).

Delivery model (F-022-06): a dispatch does a single immediate POST attempt.
On failure the delivery row is left ``pending`` with ``next_attempt_at`` set
to the next backoff time and a scheduler drain job (``drain_pending_deliveries``)
picks it up later. Nothing sleeps inside a request or holds a DB session across
the backoff schedule, so the test/retry admin endpoints return promptly and a
failing endpoint never pins a connection through minutes of inline sleeps.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import WebhookDelivery, WebhookEndpoint
from shared.db.session import get_tenant_db
from shared.webhooks.event_types import WEBHOOK_EVENT_TYPES
from shared.webhooks.ssrf import (
    ssrf_safe_transport,
    validate_webhook_url,
)

logger = logging.getLogger(__name__)

_MAX_BODY_BYTES = 64 * 1024
# Backoff schedule indexed by attempt number already made. attempts==1 means
# one POST has failed, so wait _BACKOFF_SCHEDULE_SEC[0] before the next.
_BACKOFF_SCHEDULE_SEC = (10, 60, 300)
_MAX_ATTEMPTS = len(_BACKOFF_SCHEDULE_SEC) + 1  # 1 immediate + 3 retries = 4
_REQUEST_TIMEOUT_SEC = 10


def _decrypt_secret(encrypted: Optional[bytes]) -> Optional[str]:
    if not encrypted:
        return None
    try:
        from shared.security.credential_crypto import decrypt_str
        return decrypt_str(encrypted)
    except Exception:
        logger.exception("Failed to decrypt webhook signing secret")
        return None


def generate_signing_secret() -> tuple[str, bytes]:
    """Return (plaintext, encrypted_bytes) for a new signing secret."""
    import secrets
    from shared.security.credential_crypto import encrypt_str
    plaintext = secrets.token_urlsafe(32)
    encrypted = encrypt_str(plaintext)
    return plaintext, encrypted


def compute_signature(secret: str, unix_ts: int, body_bytes: bytes) -> str:
    msg = f"{unix_ts}.".encode() + body_bytes
    digest = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return f"t={unix_ts},v1={digest}"


async def _post_once(
    url: str, body_bytes: bytes, sig_header: str, event_type: str,
) -> tuple[bool, Optional[int], Optional[str]]:
    """One POST attempt through the SSRF-safe transport.

    A non-global resolved address (SSRF) surfaces as a connect error and is
    treated as a non-retryable failure by the caller (it will never succeed).
    """
    try:
        async with httpx.AsyncClient(
            timeout=_REQUEST_TIMEOUT_SEC, transport=ssrf_safe_transport(),
        ) as client:
            resp = await client.post(
                url,
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Tessallite-Signature": sig_header,
                    "X-Tessallite-Event": event_type,
                },
            )
            if 200 <= resp.status_code < 300:
                return True, resp.status_code, None
            return False, resp.status_code, resp.text[:500]
    except Exception as exc:
        return False, None, str(exc)[:500]


def _event_matches(filters: list, event_type: str) -> bool:
    if "*" in filters:
        return True
    return event_type in filters


def _backoff_delay(attempts_made: int) -> Optional[int]:
    """Seconds to wait before the next attempt, or None if retries exhausted."""
    idx = attempts_made - 1
    if idx < 0 or idx >= len(_BACKOFF_SCHEDULE_SEC):
        return None
    return _BACKOFF_SCHEDULE_SEC[idx]


def _is_non_retryable(status_code: Optional[int]) -> bool:
    """4xx (except 408/429) responses will not succeed on retry."""
    return (
        status_code is not None
        and 400 <= status_code < 500
        and status_code not in (408, 429)
    )


def rebuild_signed_body(
    endpoint: WebhookEndpoint, delivery: WebhookDelivery,
) -> tuple[str, bytes, str]:
    """Rebuild the (url, body_bytes, signature) for an existing delivery row.

    Captures the timestamp once for both body and signature (F-022-11).
    """
    secret = _decrypt_secret(endpoint.signing_secret)
    ts = int(time.time())
    body = {
        "event_type": delivery.event_type,
        "payload": delivery.payload,
        "emitted_at": ts,
    }
    body_bytes = json.dumps(body, default=str).encode()
    sig_header = compute_signature(secret, ts, body_bytes) if secret else ""
    return endpoint.url, body_bytes, sig_header


async def attempt_delivery(
    delivery: WebhookDelivery,
    url: str,
    body_bytes: bytes,
    sig_header: str,
) -> None:
    """Make one POST attempt and update ``delivery`` in place.

    Transitions the row to ``delivered`` on success, ``dlq`` when retries are
    exhausted or the failure is non-retryable, or leaves it ``pending`` with
    ``next_attempt_at`` set for the drain job to pick up later. Never sleeps.
    """
    ok, status_code, error = await _post_once(
        url, body_bytes, sig_header, delivery.event_type,
    )

    delivery.attempts = (delivery.attempts or 0) + 1
    delivery.last_attempt_at = datetime.now(timezone.utc)
    delivery.response_code = status_code
    delivery.error_message = error

    if ok:
        delivery.status = "delivered"
        delivery.next_attempt_at = None
        return

    if _is_non_retryable(status_code):
        delivery.status = "dlq"
        delivery.next_attempt_at = None
        return

    delay = _backoff_delay(delivery.attempts)
    if delay is None:
        delivery.status = "dlq"
        delivery.next_attempt_at = None
    else:
        delivery.status = "pending"
        delivery.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay)


async def dispatch_event(
    tenant_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    single_attempt: bool = False,
) -> None:
    """Fire-and-forget webhook dispatch. Manages its own DB session. Never raises.

    Makes a single immediate attempt per matching endpoint. A failed attempt is
    persisted as ``pending`` with ``next_attempt_at`` and retried later by the
    scheduler drain job — no inline sleeps. ``single_attempt=True`` (used by the
    test endpoint) records the outcome but never schedules a retry.
    """
    if event_type not in WEBHOOK_EVENT_TYPES and event_type != "test.ping":
        logger.error(
            "emit_webhook called with unknown event_type=%s (not in WEBHOOK_EVENT_TYPES). "
            "Add it to shared/webhooks/event_types.py.",
            event_type,
        )
    try:
        async for db in get_tenant_db(tenant_id):
            result = await db.execute(
                select(WebhookEndpoint).where(WebhookEndpoint.is_active == True)  # noqa: E712
            )
            endpoints = result.scalars().all()

            for ep in endpoints:
                if not _event_matches(ep.event_filters or ["*"], event_type):
                    continue

                # F-022-05: refuse delivery to an unsafe URL up front. A stored
                # endpoint that was registered before validation existed, or a
                # name that now resolves internally, is rejected here too.
                try:
                    validate_webhook_url(ep.url)
                except ValueError as exc:
                    delivery = WebhookDelivery(
                        endpoint_id=ep.id,
                        event_type=event_type,
                        payload=payload,
                        status="dlq",
                        attempts=0,
                        error_message=f"Rejected unsafe URL: {exc}",
                        next_attempt_at=None,
                    )
                    db.add(delivery)
                    await db.commit()
                    continue

                secret = _decrypt_secret(ep.signing_secret)
                ts = int(time.time())
                body = {
                    "event_type": event_type,
                    "payload": payload,
                    # F-022-11: capture the timestamp once and use it for both
                    # the body and the signature so they can never disagree.
                    "emitted_at": ts,
                }
                body_bytes = json.dumps(body, default=str).encode()

                delivery = WebhookDelivery(
                    endpoint_id=ep.id,
                    event_type=event_type,
                    payload=payload,
                    status="pending",
                )

                if len(body_bytes) > _MAX_BODY_BYTES:
                    delivery.status = "dlq"
                    delivery.error_message = f"Payload exceeds {_MAX_BODY_BYTES} byte limit"
                    db.add(delivery)
                    await db.commit()
                    continue

                sig_header = ""
                if secret:
                    sig_header = compute_signature(secret, ts, body_bytes)

                db.add(delivery)
                await attempt_delivery(delivery, ep.url, body_bytes, sig_header)
                if single_attempt and delivery.status == "pending":
                    # The test endpoint records the first outcome only.
                    delivery.next_attempt_at = None
                await db.commit()
    except Exception:
        logger.exception("Webhook dispatch failed for tenant=%s event=%s", tenant_id, event_type)


async def drain_pending_deliveries(tenant_id: str, *, batch_size: int = 100) -> int:
    """Retry pending webhook deliveries whose backoff has elapsed.

    Run by the scheduler. Picks ``pending`` rows with ``next_attempt_at`` due,
    makes one POST attempt each, and re-schedules or DLQs per the backoff
    schedule. Returns the number of deliveries attempted. Never raises.
    """
    attempted = 0
    try:
        async for db in get_tenant_db(tenant_id):
            now = datetime.now(timezone.utc)
            result = await db.execute(
                select(WebhookDelivery)
                .where(WebhookDelivery.status == "pending")
                .where(WebhookDelivery.next_attempt_at.is_not(None))
                .where(WebhookDelivery.next_attempt_at <= now)
                .order_by(WebhookDelivery.next_attempt_at)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            due = list(result.scalars().all())
            for delivery in due:
                ep = await db.get(WebhookEndpoint, delivery.endpoint_id)
                if ep is None or not ep.is_active:
                    delivery.status = "dlq"
                    delivery.next_attempt_at = None
                    delivery.error_message = "Endpoint missing or inactive"
                    await db.commit()
                    continue
                url, body_bytes, sig_header = rebuild_signed_body(ep, delivery)
                await attempt_delivery(delivery, url, body_bytes, sig_header)
                await db.commit()
                attempted += 1
    except Exception:
        logger.exception("Webhook drain failed for tenant=%s", tenant_id)
    return attempted


async def deliver_test_event(
    db: AsyncSession,
    endpoint: WebhookEndpoint,
    payload: dict[str, Any],
) -> WebhookDelivery:
    """Single-attempt test delivery to one endpoint, returning the exact row.

    Used by the admin "test webhook" action (F-022-06/07). One POST, no
    backoff, no inline sleeps; the caller gets back the delivery it just fired
    rather than guessing from "the latest delivery for this endpoint".
    """
    # F-022-05: validate before sending so a stored-unsafe URL is reported.
    try:
        validate_webhook_url(endpoint.url)
    except ValueError as exc:
        delivery = WebhookDelivery(
            endpoint_id=endpoint.id,
            event_type="test.ping",
            payload=payload,
            status="dlq",
            attempts=0,
            error_message=f"Rejected unsafe URL: {exc}",
        )
        db.add(delivery)
        await db.flush()
        return delivery

    secret = _decrypt_secret(endpoint.signing_secret)
    ts = int(time.time())
    body = {"event_type": "test.ping", "payload": payload, "emitted_at": ts}
    body_bytes = json.dumps(body, default=str).encode()
    sig_header = compute_signature(secret, ts, body_bytes) if secret else ""

    delivery = WebhookDelivery(
        endpoint_id=endpoint.id,
        event_type="test.ping",
        payload=payload,
        status="pending",
    )
    db.add(delivery)
    await attempt_delivery(delivery, endpoint.url, body_bytes, sig_header)
    # A test never schedules a retry — report the single outcome.
    if delivery.status == "pending":
        delivery.status = "dlq"
    delivery.next_attempt_at = None
    await db.flush()
    return delivery


async def emit_webhook(tenant_id: str, event_type: str, payload: dict[str, Any]) -> None:
    """Create a fire-and-forget background task for webhook dispatch."""
    asyncio.create_task(dispatch_event(tenant_id, event_type, payload))
