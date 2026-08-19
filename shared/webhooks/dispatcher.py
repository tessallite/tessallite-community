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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config.settings import get_settings
from shared.db.models import WebhookDelivery, WebhookEndpoint
from shared.db.session import get_tenant_db
from shared.webhooks.event_types import WEBHOOK_EVENT_TYPES, WILDCARD
# Bug-8350-sibling (R2 MED-4) — the agent-service dispatcher's Bug-8350 fix
# scrubs any embedded URL out of ``last_error`` before persisting; this,
# the ORIGINAL Bug-8056 platform-wide dispatcher, has the exact same leak
# class on a different vector: up to 500 bytes of receiver-echoed response
# text (or a raised exception's message) land in ``error_message``
# unscrubbed, and a hostile or misconfigured receiver commonly echoes the
# request URL — including any bearer token or API key embedded in it — back
# in its own error page. Reuse the same shared helper so both dispatcher
# families scrub identically instead of drifting.
from shared.webhooks.redact import redact_url_for_display, scrub_url_from_text
from shared.webhooks.ssrf import (
    ssrf_safe_transport,
    validate_webhook_url,
)

logger = logging.getLogger(__name__)


class WebhookPersistError(RuntimeError):
    """Raised by :func:`emit_webhook` when delivery rows cannot be persisted.

    F-022-05: a persistence failure must NOT be swallowed and must NOT look like
    "zero matching subscriptions". This exception is raised so the caller (which
    already wraps ``emit_webhook`` in try/except and logs) records durable
    operator evidence that the event's delivery rows were lost, instead of the
    business operation reporting success with no delivery history and no DLQ
    entry.
    """


@dataclass
class WebhookEmitResult:
    """Structured outcome of an :func:`emit_webhook` call (F-022-05).

    Lets a caller distinguish "no endpoint subscribed" (``endpoints_matched==0``)
    from a successful enqueue (``persisted>0``). A persistence failure never
    reaches here — it raises :class:`WebhookPersistError` instead of returning a
    silently-empty result.
    """

    endpoints_matched: int = 0
    persisted: int = 0
    delivery_ids: list = field(default_factory=list)


_MAX_BODY_BYTES = 64 * 1024
# Backoff schedule indexed by attempt number already made. attempts==1 means
# one POST has failed, so wait _BACKOFF_SCHEDULE_SEC[0] before the next.
_BACKOFF_SCHEDULE_SEC = (10, 60, 300)
_MAX_ATTEMPTS = len(_BACKOFF_SCHEDULE_SEC) + 1  # 1 immediate + 3 retries = 4
_REQUEST_TIMEOUT_SEC = 10
# Bug-7334: cap on the number of bytes we read from the receiver's
# response body. Without this, a hostile receiver can drip-feed a
# multi-gigabyte body and exhaust process memory.
_MAX_RESPONSE_BYTES = 8 * 1024  # 8 KB — enough for a useful error snippet
# Bug-8355 sibling — absolute wall-clock ceiling for one delivery attempt.
# Deliberately larger than _REQUEST_TIMEOUT_SEC so it only fires on the
# trickle case; a slow-but-honest receiver still reports its own error.
# R2 reviewer finding 4 — clamped, not trusted. The knob is shared with the
# agent-service dispatcher, so an operator tightening THAT family could
# otherwise drop this deadline below this dispatcher's own per-phase request
# timeout, at which point every slow-but-honest receiver's real error is
# replaced by ATTEMPT_DEADLINE_REASON. R3 reviewer FIND-1: the floor is one
# second above ONE per-phase timeout, not above connect+read+write, so it does
# not guarantee an honest receiver is never pre-empted -- it guarantees a
# misconfigured knob cannot make that the norm. The default (30s) is what
# production runs, and a wall-clock backstop deliberately is not derived from
# the phase budget.
_ATTEMPT_DEADLINE_SEC = max(
    get_settings().AGENT_WEBHOOK_ATTEMPT_DEADLINE_SEC, _REQUEST_TIMEOUT_SEC + 1,
)
ATTEMPT_DEADLINE_REASON = (
    "Receiver did not complete the response within the delivery deadline "
    "(it may be sending the response body very slowly). The connection was "
    "released and delivery will be retried."
)

# Bug-8557: terminal reason for a delivery row that carries no frozen
# destination. Every row enqueued after migration 0202 pins
# ``destination_url_snapshot``; a row without one is either a legacy row from
# before the column existed or a row written by a producer that bypassed the
# enqueue paths in this module. Either way there is no authoritative target,
# and the ONLY other candidate — the endpoint's CURRENT url — is precisely the
# cross-wire this fix exists to stop. The row is dead-lettered instead of
# guessed. The reason string starts with a stable token so operators and the
# DLQ view can group these, followed by the recovery instruction; a manual DLQ
# retry re-pins the destination from the endpoint, which is an explicit
# operator decision to send to wherever the endpoint points NOW.
INCOHERENT_DELIVERY_ROW_REASON = (
    "incoherent_delivery_row: this delivery has no snapshotted destination "
    "URL, so the receiver it was queued for cannot be determined. It was not "
    "sent. Retry it from the DLQ to re-target it at the endpoint's current "
    "URL, or delete it."
)

# Bug-5951: known placeholder values that must never be used as HMAC signing
# secrets.  Checked case-insensitively after stripping whitespace.
_PLACEHOLDER_SECRETS = frozenset({
    "changeme", "secret", "placeholder", "test", "webhook-secret",
    "signing-secret", "your-secret-here", "xxx", "password",
    "replace-me", "fixme", "todo",
})

# Bug-6004: a bare `asyncio.create_task(...)` result must be referenced
# somewhere or the event loop is free to garbage-collect the Task mid-flight
# (see the "Important" note in the asyncio.create_task docs). Keep every
# background dispatch task alive here until it finishes; the done-callback
# removes it from the set so this never grows unbounded.
_background_tasks: set[asyncio.Task] = set()


def _on_background_task_done(task: asyncio.Task) -> None:
    _background_tasks.discard(task)
    # Bug-8349-sibling, defense in depth — mirrors the same fix in
    # agent-service's src/api/conversations.py::_spawn_background. Every
    # coroutine spawned through this function already wraps its own body in
    # a top-level try/except (e.g. ``_dispatch_queued_deliveries``), so this
    # should be unreachable in practice; it exists so a future coroutine
    # added here that forgets that guard fails loudly instead of vanishing
    # with no log, no DLQ/DB row, and no metric.
    if not task.cancelled():
        exc = task.exception()
        if exc is not None:
            logger.error(
                "Background webhook task failed with an unhandled exception",
                exc_info=exc,
            )


def _spawn_background(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_on_background_task_done)
    return task


def _decrypt_secret(encrypted: Optional[bytes]) -> Optional[str]:
    if not encrypted:
        return None
    try:
        from shared.security.credential_crypto import decrypt_str
        return decrypt_str(encrypted)
    except Exception:
        logger.exception("Failed to decrypt webhook signing secret")
        return None


def _is_valid_signing_secret(secret: Optional[str]) -> bool:
    """Return True only when *secret* provides real HMAC protection.

    Bug-5951: webhook dispatch must never sign with a placeholder or empty
    secret -- an attacker who intercepts the payload could trivially forge
    it.  Rejects None, empty strings, whitespace-only strings, and known
    placeholder values (case-insensitive, after stripping whitespace).
    """
    if not secret:
        return False
    stripped = secret.strip()
    if not stripped:
        return False
    if stripped.lower() in _PLACEHOLDER_SECRETS:
        return False
    return True


# Public alias so other services (e.g. agent-service) can reuse the canonical
# placeholder-secret validator without importing the underscore-private name.
is_valid_signing_secret = _is_valid_signing_secret


# Bug-8349: canonical wording for a terminal "refused to send unsigned"
# outcome. Exported so every outbound-webhook dispatcher in the codebase
# (this platform-wide one, agent-service's, and any future one) records the
# identical reason instead of each hand-rolling its own message and risking
# drift on the underlying fail-closed contract.
UNSIGNED_WEBHOOK_REFUSAL_REASON = (
    "Not sent: missing or invalid webhook signing secret. An unsigned "
    "webhook is never transmitted because the receiver cannot verify its "
    "authenticity. Rotate the signing secret to restore signed delivery."
)


def generate_signing_secret() -> tuple[str, bytes]:
    """Return (plaintext, encrypted_bytes) for a new signing secret."""
    import secrets
    from shared.security.credential_crypto import encrypt_str
    plaintext = secrets.token_urlsafe(32)
    encrypted = encrypt_str(plaintext)
    return plaintext, encrypted


_DEST_ENC_PREFIX = "enc:"


def seal_destination_url(plaintext: str) -> str:
    """Fernet-encrypt a webhook destination for ``destination_url_snapshot`` (F-022-08)."""
    import base64
    from shared.security.credential_crypto import encrypt_str
    token = base64.b64encode(encrypt_str(plaintext)).decode("ascii")
    return f"{_DEST_ENC_PREFIX}{token}"


def reveal_destination_url(stored: str | None) -> str | None:
    """Decrypt a sealed snapshot, or return legacy plaintext unchanged."""
    if not stored:
        return stored
    if not stored.startswith(_DEST_ENC_PREFIX):
        return stored
    import base64
    from shared.security.credential_crypto import decrypt_str
    try:
        return decrypt_str(base64.b64decode(stored[len(_DEST_ENC_PREFIX):]))
    except Exception:
        logger.warning("destination_url_snapshot decrypt failed")
        return None


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

    Bug-5951: the ``X-Tessallite-Signature`` header is only included when
    *sig_header* is non-empty (i.e. a real HMAC was computed).  Sending an
    empty header would mislead the receiver into thinking the payload is
    signed when it is not.
    """
    try:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "X-Tessallite-Event": event_type,
        }
        if sig_header:
            headers["X-Tessallite-Signature"] = sig_header
        # Bug-8355 sibling (R1 reviewer F12) — an absolute wall-clock ceiling
        # on the attempt. This dispatcher opens a fresh client per call, so it
        # cannot starve a shared pool the way agent-service's could; but the
        # underlying exposure is the same, because httpx's read timeout is PER
        # READ and every arriving chunk resets it. A receiver trickling one
        # byte every few seconds stays under both the read timeout and the
        # 8 KB byte cap indefinitely, holding a task and a socket for as long
        # as it likes. Bytes are bounded by _MAX_RESPONSE_BYTES; only a total
        # deadline bounds time.
        return await asyncio.wait_for(
            _stream_post(url, body_bytes, headers), timeout=_ATTEMPT_DEADLINE_SEC,
        )
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning(
            "Webhook POST exceeded the %ds delivery deadline; connection "
            "released (receiver: %s)",
            _ATTEMPT_DEADLINE_SEC, redact_url_for_display(url),
        )
        return False, None, ATTEMPT_DEADLINE_REASON
    except Exception as exc:
        return False, None, scrub_url_from_text(str(exc)[:500], url)


async def _stream_post(
    url: str, body_bytes: bytes, headers: dict[str, str],
) -> tuple[bool, Optional[int], Optional[str]]:
    """One streamed POST, reading at most ``_MAX_RESPONSE_BYTES`` of the body.

    Bug-7334: without the cap, a hostile receiver drip-feeding a multi-GB body
    exhausts process memory. Split out of ``_post_once`` so the deadline in
    Bug-8355's sibling fix has a single coroutine to bound.
    """
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=_REQUEST_TIMEOUT_SEC,
            read=_REQUEST_TIMEOUT_SEC,
            write=_REQUEST_TIMEOUT_SEC,
            pool=_REQUEST_TIMEOUT_SEC,
        ),
        transport=ssrf_safe_transport(),
    ) as client:
        async with client.stream(
            "POST", url, content=body_bytes, headers=headers,
        ) as resp:
            status_code = resp.status_code
            if 200 <= status_code < 300:
                return True, status_code, None
            # Read only enough of the error body for a useful snippet.
            chunks = []
            read_total = 0
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                read_total += len(chunk)
                if read_total >= _MAX_RESPONSE_BYTES:
                    break
            error_text = b"".join(chunks)[:_MAX_RESPONSE_BYTES].decode(
                errors="replace"
            )[:500]
            return False, status_code, error_text


def _event_matches(filters: Optional[list], event_type: str) -> bool:
    """True if an endpoint's stored ``event_filters`` subscribe to ``event_type``.

    Fail-closed on empty/missing filters (Bug-6313): an endpoint whose
    ``event_filters`` is empty (or NULL) subscribes to NOTHING, not everything.
    To receive every event a filter list must explicitly contain the wildcard
    ``"*"`` — which is both the API create default and the DB server_default.

    Previously the dispatch site coerced an empty list to ``["*"]`` (empty ->
    all), disagreeing with this helper (empty -> none) and with validation. An
    endpoint accidentally stored with ``[]`` then silently received every event,
    including sensitive ones (user.created, settings.changed). Empty now
    consistently means "no subscription" across validation, this helper, and
    dispatch.
    """
    if not filters:
        return False
    if WILDCARD in filters:
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
) -> tuple[Optional[str], bytes, str]:
    """Rebuild the (url, body_bytes, signature) for an existing delivery row.

    Captures the timestamp once for both body and signature (F-022-11).

    Bug-5952: generates a fresh ``time.time()`` on every call so each retry
    attempt carries its own timestamp in the HMAC signature, limiting the
    replay window to a single backoff interval rather than the entire
    delivery lifetime.

    Bug-5951: validates the signing secret before computing the HMAC.  If
    the secret is missing or a known placeholder, the signature is omitted
    and a warning is logged.

    F-022-06: the delivery is signed with the secret PINNED to it at enqueue
    time (``delivery.signing_secret_snapshot``), not the endpoint's current
    secret. Rotating the endpoint secret therefore never invalidates a queued
    delivery's signature. Only legacy rows created before the snapshot column
    existed (``signing_secret_snapshot is None``) fall back to the endpoint's
    current secret, preserving prior behaviour for them.

    Bug-8557: the DESTINATION is pinned the same way. The url returned is
    ``delivery.destination_url_snapshot`` — the receiver this row was queued
    for — never the endpoint's live ``url``. Pinning the secret without the
    destination was incoherent: an admin repointing an endpoint at receiver B
    caused rows queued for receiver A to be POSTed to B, carrying A's payload
    and signed with A's pinned secret. B could not verify the signature and
    should never have seen A's payload; and since a URL change also rotates
    the endpoint secret (Bug-8410 parity in ``update_webhook``), B was handed
    a valid HMAC computed under a secret it does not hold.

    A row with no frozen destination returns ``(None, b"", "")``. The caller
    MUST dead-letter it with :data:`INCOHERENT_DELIVERY_ROW_REASON` rather
    than fall back to the live url — that fallback is the defect.
    """
    target_url: Optional[str] = reveal_destination_url(
        getattr(delivery, "destination_url_snapshot", None)
    )
    if not target_url:
        return None, b"", ""
    pinned = getattr(delivery, "signing_secret_snapshot", None)
    secret = _decrypt_secret(pinned if pinned else endpoint.signing_secret)
    ts = int(time.time())
    body = {
        "event_type": delivery.event_type,
        "payload": delivery.payload,
        "emitted_at": ts,
    }
    body_bytes = json.dumps(body, default=str).encode()
    sig_header = ""
    if _is_valid_signing_secret(secret):
        sig_header = compute_signature(secret, ts, body_bytes)
    elif secret is not None:
        logger.warning(
            "Webhook endpoint %s has an invalid signing secret "
            "(empty or placeholder); no signature will be produced. "
            "Bug-8056: the payload will NOT be sent and the delivery is "
            "recorded as failed. Rotate the secret to restore signed delivery.",
            endpoint.id,
        )
    return target_url, body_bytes, sig_header


def _dlq_incoherent_delivery(delivery: WebhookDelivery) -> None:
    """Bug-8557: terminally fail a delivery row that has no frozen destination.

    Shared by every dispatch site so the three callers of
    :func:`rebuild_signed_body` cannot drift on how the ``None`` url is handled
    — the whole point of the fix is that no caller may substitute a URL of its
    own choosing.
    """
    delivery.status = "dlq"
    delivery.next_attempt_at = None
    delivery.error_message = INCOHERENT_DELIVERY_ROW_REASON
    logger.error(
        "Webhook delivery %s has no destination_url_snapshot; dead-lettered "
        "without sending (Bug-8557).",
        getattr(delivery, "id", "<new>"),
    )


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

    Bug-8056 (F-022-07): an outbound webhook MUST carry a valid HMAC signature.
    An empty ``sig_header`` means no signature could be produced — the signing
    secret is missing, a known placeholder, or undecryptable. That is a terminal
    configuration/security failure, not a transient one. The payload is NOT
    transmitted (a receiver could never authenticate it) and the row is recorded
    as a terminal FAILED (``dlq``) state so the outcome is operator-visible.
    Crucially, an unsigned send is NEVER recorded as ``delivered``: without this
    guard, ``_post_once`` would POST the unsigned body and a 2xx would be logged
    as a successful delivery, contrary to the outbound-signing contract.
    """
    if not sig_header:
        delivery.attempts = (delivery.attempts or 0) + 1
        delivery.last_attempt_at = datetime.now(timezone.utc)
        delivery.response_code = None
        delivery.error_message = UNSIGNED_WEBHOOK_REFUSAL_REASON
        delivery.status = "dlq"
        delivery.next_attempt_at = None
        logger.error(
            "Webhook delivery %s refused: no valid signing secret; payload not "
            "sent and recorded as failed (never delivered).",
            getattr(delivery, "id", "<new>"),
        )
        return

    ok, status_code, error = await _post_once(
        url, body_bytes, sig_header, delivery.event_type,
    )

    delivery.attempts = (delivery.attempts or 0) + 1
    delivery.last_attempt_at = datetime.now(timezone.utc)
    delivery.response_code = status_code
    # Bug-8350-sibling (R2 MED-4) — ``error`` is either up to 500 bytes of
    # the receiver's own echoed response body, or an HTTP client exception
    # message; either can embed the request URL (and any secret in it)
    # verbatim. Scrub before this ever reaches a persisted row, GET
    # /webhooks/dlq, or the webhook.dlq_delete audit log
    # (services/model-service/src/api/webhooks.py) that copies this field.
    delivery.error_message = scrub_url_from_text(error, url)

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


async def drain_pending_deliveries(
    tenant_id: str, *, batch_size: int = 100, raise_on_error: bool = False,
) -> int:
    """Retry pending webhook deliveries whose backoff has elapsed.

    Run by the scheduler. Picks ``pending`` rows with ``next_attempt_at`` due,
    makes one POST attempt each, and re-schedules or DLQs per the backoff
    schedule. Returns the number of deliveries attempted.

    B03: by default this NEVER RAISES — a catastrophic tenant-level failure
    (e.g. the tenant DB is unreachable) is logged and the count returned, which
    is the historical contract every existing caller relies on. The scheduler
    drain sweep passes ``raise_on_error=True`` so that catastrophic failure is
    RE-RAISED instead of being swallowed into a ``0`` count that the ledger would
    record as ``success`` — the sweep's per-tenant guard catches it, records a
    truthful ``partial``/``error`` outcome, and continues with the next tenant.
    Per-delivery send failures are unaffected (they are durably DLQ'd, not
    raised).

    Bug-6005 (residual, found on re-review): the initial batch SELECT locks
    every candidate row with ``FOR UPDATE SKIP LOCKED``, but each per-row
    ``await db.commit()`` below ends that transaction and releases the locks
    on every *other* row still queued in the batch (Postgres releases all
    locks held by a transaction at COMMIT, not just the row just committed).
    A concurrent ``_dispatch_queued_deliveries`` run could then claim one of
    those now-unlocked rows and deliver it a second time. Each row is
    therefore re-claimed with its own ``FOR UPDATE SKIP LOCKED`` + a
    ``status == "pending"`` re-check immediately before it is processed,
    mirroring what ``_dispatch_queued_deliveries`` already does. If another
    process has claimed or already finished the row, this skips it.

    Bug-6005 round-2 (found on re-review of the re-lock fix): the per-row
    re-SELECT must repeat the ``next_attempt_at <= now`` predicate too, not
    just ``status == "pending"``. Otherwise a row that a concurrent
    ``_dispatch_queued_deliveries`` run claimed, attempted, failed, and
    rescheduled with a *future* backoff time (between this function's batch
    SELECT and its own per-row re-SELECT) would still look claimable here --
    an early, backoff-violating retry, not a duplicate delivery, but still a
    real bug the row-status-only re-check let slip through.
    """
    attempted = 0
    try:
        async for db in get_tenant_db(tenant_id):
            now = datetime.now(timezone.utc)
            result = await db.execute(
                select(WebhookDelivery.id)
                .where(WebhookDelivery.status == "pending")
                .where(WebhookDelivery.next_attempt_at.is_not(None))
                .where(WebhookDelivery.next_attempt_at <= now)
                .order_by(WebhookDelivery.next_attempt_at)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            due_ids = list(result.scalars().all())
            for delivery_id in due_ids:
                row_result = await db.execute(
                    select(WebhookDelivery)
                    .where(WebhookDelivery.id == delivery_id)
                    .where(WebhookDelivery.status == "pending")
                    .where(WebhookDelivery.next_attempt_at.is_not(None))
                    .where(WebhookDelivery.next_attempt_at <= datetime.now(timezone.utc))
                    .with_for_update(skip_locked=True)
                )
                delivery = row_result.scalar_one_or_none()
                if delivery is None:
                    continue
                ep = await db.get(WebhookEndpoint, delivery.endpoint_id)
                if ep is None or not ep.is_active:
                    delivery.status = "dlq"
                    delivery.next_attempt_at = None
                    delivery.error_message = "Endpoint missing or inactive"
                    await db.commit()
                    continue
                url, body_bytes, sig_header = rebuild_signed_body(ep, delivery)
                if url is None:
                    # Bug-8557: no authoritative destination — never guess.
                    _dlq_incoherent_delivery(delivery)
                    await db.commit()
                    attempted += 1
                    continue
                await attempt_delivery(delivery, url, body_bytes, sig_header)
                await db.commit()
                attempted += 1
    except Exception:
        logger.exception("Webhook drain failed for tenant=%s", tenant_id)
        if raise_on_error:
            raise
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
            # Bug-8557: record the destination this row was for even though it
            # was never sent, so the row is self-describing and a DLQ retry has
            # a coherent target. Never surfaced by the API (DeliveryResponse
            # carries no url field).
            destination_url_snapshot=seal_destination_url(endpoint.url),
            # Bug-8430 (related-defect sweep) -- ssrf.validate_webhook_url
            # interpolates the offending hostname, and can interpolate a
            # urlsplit/port ValueError whose message quotes the whole URL, so
            # this reason string is another way the destination lands in a
            # persisted, API-returned, audit-copied field unscrubbed.
            error_message=scrub_url_from_text(
                f"Rejected unsafe URL: {exc}", endpoint.url,
            ),
        )
        db.add(delivery)
        await db.flush()
        return delivery

    secret = _decrypt_secret(endpoint.signing_secret)
    ts = int(time.time())
    body = {"event_type": "test.ping", "payload": payload, "emitted_at": ts}
    body_bytes = json.dumps(body, default=str).encode()

    # Bug-5951: validate the secret before signing.
    sig_header = ""
    if _is_valid_signing_secret(secret):
        sig_header = compute_signature(secret, ts, body_bytes)
    elif secret is not None:
        logger.warning(
            "Webhook endpoint %s has an invalid signing secret "
            "(empty or placeholder); Bug-8056: the test payload will NOT be "
            "sent and the delivery is recorded as failed, never delivered.",
            endpoint.id,
        )

    delivery = WebhookDelivery(
        endpoint_id=endpoint.id,
        event_type="test.ping",
        payload=payload,
        status="pending",
        # F-022-06: pin the secret used for this single attempt.
        signing_secret_snapshot=endpoint.signing_secret,
        # Bug-8557: pin the destination too, so the row's own history records
        # where it was actually sent even if the endpoint is repointed later.
        destination_url_snapshot=seal_destination_url(endpoint.url),
    )
    db.add(delivery)
    # Bug-8056: attempt_delivery refuses to transmit when sig_header is empty
    # and records a terminal failed (dlq) state — an unsigned test is never
    # reported as delivered.
    await attempt_delivery(
        delivery,
        reveal_destination_url(delivery.destination_url_snapshot),
        body_bytes,
        sig_header,
    )
    # A test never schedules a retry — report the single outcome.
    if delivery.status == "pending":
        delivery.status = "dlq"
    delivery.next_attempt_at = None
    await db.flush()
    return delivery


def endpoint_can_sign(endpoint: WebhookEndpoint) -> bool:
    """True when *endpoint* carries a real (non-placeholder) signing secret.

    Bug-8131 / E-1: lets a caller pick, among endpoints registered for the same
    URL, one that can ACTUALLY sign before refusing — reusing the exact
    decrypt + placeholder validation the signed dispatch path uses, so the
    selection and the sign/refuse decision can never disagree.
    """
    return _is_valid_signing_secret(_decrypt_secret(endpoint.signing_secret))


async def enqueue_signed_callback(
    db: AsyncSession,
    *,
    endpoint: WebhookEndpoint,
    event_type: str,
    payload: dict[str, Any],
) -> WebhookDelivery:
    """Bug-8131: durably enqueue ONE signed completion callback to a KNOWN endpoint.

    The per-request ``trigger/refresh`` / ``trigger/pocket-refresh`` completion
    callback reuses the subscription path's durable SIGNED contract instead of
    the bespoke unsigned ``_fire_webhook``: it persists a :class:`WebhookDelivery`
    pinned to the endpoint's signing secret + URL (so a retry re-signs with the
    pinned secret through :func:`rebuild_signed_body` -> :func:`compute_signature`,
    never a second signing implementation), leaving it ``pending`` for the same
    background sender + minute-cadence :func:`drain_pending_deliveries` the
    subscription path uses. The caller commits the row and kicks the async
    dispatch (mirroring :func:`emit_webhook`).

    FAIL CLOSED: an endpoint with no valid signing secret yields a TERMINAL
    ``dlq`` refusal row (``UNSIGNED_WEBHOOK_REFUSAL_REASON``) and NOTHING is
    sent — the completion callback is never delivered unsigned. This is the same
    fail-closed rule :func:`attempt_delivery` enforces at dispatch time, applied
    up front so the trigger endpoint can report the refusal to its caller
    synchronously.

    Returns the persisted (flushed) delivery. Does not commit.
    """
    secret = _decrypt_secret(endpoint.signing_secret)
    if not _is_valid_signing_secret(secret):
        delivery = WebhookDelivery(
            endpoint_id=endpoint.id,
            event_type=event_type,
            payload=payload,
            status="dlq",
            attempts=0,
            next_attempt_at=None,
            destination_url_snapshot=seal_destination_url(endpoint.url),
            error_message=UNSIGNED_WEBHOOK_REFUSAL_REASON,
        )
        db.add(delivery)
        await db.flush()
        logger.error(
            "Completion callback refused for endpoint %s: no valid signing "
            "secret; payload not sent (never delivered unsigned) — recorded as "
            "a terminal dlq refusal. Rotate the secret to restore signed "
            "delivery.",
            endpoint.id,
        )
        return delivery

    # E-3 (DeepSeek): apply the same oversize guard :func:`emit_webhook` applies,
    # so this new enqueue path shares the shared body-size contract instead of
    # skipping it. The completion payload is small and fixed-shape today, so this
    # is a symmetry guard against a future larger one, never a live limit.
    body_bytes = json.dumps(
        {"event_type": event_type, "payload": payload, "emitted_at": int(time.time())},
        default=str,
    ).encode()
    if len(body_bytes) > _MAX_BODY_BYTES:
        delivery = WebhookDelivery(
            endpoint_id=endpoint.id,
            event_type=event_type,
            payload=payload,
            status="dlq",
            attempts=0,
            next_attempt_at=None,
            destination_url_snapshot=seal_destination_url(endpoint.url),
            error_message=f"Payload exceeds {_MAX_BODY_BYTES} byte limit",
        )
        db.add(delivery)
        await db.flush()
        logger.error(
            "Completion callback refused for endpoint %s: payload exceeds the "
            "%d byte limit; not sent.",
            endpoint.id, _MAX_BODY_BYTES,
        )
        return delivery

    delivery = WebhookDelivery(
        endpoint_id=endpoint.id,
        event_type=event_type,
        payload=payload,
        status="pending",
        next_attempt_at=datetime.now(timezone.utc),
        # F-022-06 parity: pin the secret so a later rotation never re-signs
        # this in-flight callback with the new secret.
        signing_secret_snapshot=endpoint.signing_secret,
        # Bug-8557 parity: pin the destination so the row is self-describing and
        # a repoint of the endpoint cannot redirect this queued callback.
        destination_url_snapshot=seal_destination_url(endpoint.url),
    )
    db.add(delivery)
    await db.flush()
    return delivery


async def dispatch_persisted_deliveries(
    tenant_id: str, delivery_ids: list,
) -> None:
    """Kick async delivery for rows a caller has already committed (Bug-8131).

    A thin public seam over the same background dispatch :func:`emit_webhook`
    uses, so the per-request completion callback dispatches through the exact
    signed sender + retry/DLQ path rather than a parallel one.
    """
    if delivery_ids:
        _spawn_background(_dispatch_queued_deliveries(tenant_id, delivery_ids))


async def emit_webhook(
    tenant_id: str, event_type: str, payload: dict[str, Any],
) -> WebhookEmitResult:
    """Durably persist webhook delivery rows, then dispatch asynchronously.

    Delivery rows are committed before this function returns so events survive
    process restarts. Actual HTTP delivery still happens in a background task.

    F-022-05: a persistence failure is NO LONGER swallowed. If the delivery
    rows cannot be committed, this raises :class:`WebhookPersistError` so the
    lost event is loud and durable operator evidence is recorded — instead of
    the failure being indistinguishable from "no endpoint subscribed". Returns a
    :class:`WebhookEmitResult` describing what was matched and persisted.
    """
    if event_type not in WEBHOOK_EVENT_TYPES and event_type != "test.ping":
        logger.error(
            "emit_webhook called with unknown event_type=%s (not in WEBHOOK_EVENT_TYPES). "
            "Add it to shared/webhooks/event_types.py.",
            event_type,
        )

    result = WebhookEmitResult()
    delivery_ids: list = result.delivery_ids
    try:
        async for db in get_tenant_db(tenant_id):
            ep_result = await db.execute(
                select(WebhookEndpoint).where(WebhookEndpoint.is_active == True)  # noqa: E712
            )
            endpoints = ep_result.scalars().all()

            for ep in endpoints:
                # Bug-6313: no empty->all coercion here. Empty/NULL filters mean
                # "no subscription" (fail-closed); _event_matches owns that rule.
                if not _event_matches(ep.event_filters, event_type):
                    continue

                result.endpoints_matched += 1

                # F-022-05: reject unsafe URLs up-front.
                try:
                    validate_webhook_url(ep.url)
                except ValueError as exc:
                    delivery = WebhookDelivery(
                        endpoint_id=ep.id,
                        event_type=event_type,
                        payload=payload,
                        status="dlq",
                        attempts=0,
                        # Bug-8430 (related-defect sweep) -- see
                        # deliver_test_event above: the rejection reason can
                        # quote the destination URL.
                        error_message=scrub_url_from_text(
                            f"Rejected unsafe URL: {exc}", ep.url,
                        ),
                        next_attempt_at=None,
                        # Bug-8557: self-describing terminal row (see
                        # deliver_test_event).
                        destination_url_snapshot=seal_destination_url(ep.url),
                    )
                    db.add(delivery)
                    await db.commit()
                    continue

                ts = int(time.time())
                body = {
                    "event_type": event_type,
                    "payload": payload,
                    "emitted_at": ts,
                }
                body_bytes = json.dumps(body, default=str).encode()

                if len(body_bytes) > _MAX_BODY_BYTES:
                    delivery = WebhookDelivery(
                        endpoint_id=ep.id,
                        event_type=event_type,
                        payload=payload,
                        status="dlq",
                        error_message=f"Payload exceeds {_MAX_BODY_BYTES} byte limit",
                        # Bug-8557: self-describing terminal row.
                        destination_url_snapshot=seal_destination_url(ep.url),
                    )
                    db.add(delivery)
                    await db.commit()
                    continue

                # Persist the delivery row as "pending" so it survives crashes.
                # F-022-06: pin the endpoint's signing secret to this delivery
                # at enqueue time so a later secret rotation never re-signs the
                # in-flight retry with the new secret.
                # Bug-8557: pin the DESTINATION alongside it. The url below was
                # just validated by validate_webhook_url; freezing it here is
                # what makes the pinned secret coherent, and stops an endpoint
                # edit from redirecting rows already queued for the previous
                # receiver.
                delivery = WebhookDelivery(
                    endpoint_id=ep.id,
                    event_type=event_type,
                    payload=payload,
                    status="pending",
                    next_attempt_at=datetime.now(timezone.utc),
                    signing_secret_snapshot=ep.signing_secret,
                    destination_url_snapshot=seal_destination_url(ep.url),
                )
                db.add(delivery)
                await db.flush()
                delivery_ids.append(delivery.id)

            await db.commit()
            result.persisted = len(delivery_ids)
    except Exception as exc:
        # F-022-05: do NOT swallow. A lost delivery row is a lost event; raise
        # so the caller's try/except records durable operator evidence instead
        # of the event vanishing while the business op reports success.
        logger.exception(
            "Webhook persist failed for tenant=%s event=%s", tenant_id, event_type,
        )
        raise WebhookPersistError(
            f"Failed to persist webhook deliveries for event {event_type!r}"
        ) from exc

    # Kick off async delivery for the durably-queued rows.
    if delivery_ids:
        # Bug-6004: keep a strong reference so the task can't be GC'd mid-flight.
        _spawn_background(_dispatch_queued_deliveries(tenant_id, delivery_ids))

    return result


async def emit_webhook_logged(
    tenant_id: str, event_type: str, payload: dict[str, Any],
) -> Optional[WebhookEmitResult]:
    """Fire-and-forget wrapper for post-commit request-handler emitters.

    F-022-05: the underlying :func:`emit_webhook` now RAISES on a persistence
    failure so the loss is never silent. A request handler that has ALREADY
    committed its mutation must not turn a webhook-persistence failure into a
    500 for the user (the mutation succeeded) — but it also must not lose the
    failure. This wrapper records the failure as durable, high-severity operator
    evidence in the application log and returns ``None``, while a healthy emit
    returns its :class:`WebhookEmitResult`.

    Use this ONLY when the originating mutation has already been durably
    committed. Do not use it where the webhook must be atomic with the mutation.
    """
    try:
        return await emit_webhook(tenant_id, event_type, payload)
    except WebhookPersistError:
        logger.error(
            "LOST WEBHOOK EVENT: tenant=%s event=%s could not be persisted after "
            "the originating operation committed. Delivery rows were not created; "
            "downstream consumers will not receive this event. Investigate tenant "
            "webhook storage.",
            tenant_id, event_type,
        )
        return None


async def _dispatch_queued_deliveries(
    tenant_id: str, delivery_ids: list,
) -> None:
    """Attempt immediate HTTP delivery for durably-persisted rows.

    Runs as a background task after ``emit_webhook`` has committed the
    delivery rows.  On failure the row stays ``pending`` with a backoff
    ``next_attempt_at`` for the scheduler drain job.  Never raises.
    """
    try:
        async for db in get_tenant_db(tenant_id):
            for did in delivery_ids:
                # Bug-6005: take the same row lock the drain sweep uses
                # (SELECT ... FOR UPDATE SKIP LOCKED) instead of a plain
                # `db.get`. Without this, the drain job's own SKIP LOCKED
                # sweep could claim the same row concurrently (it saw no
                # lock from this path) and POST the same webhook twice. If
                # the row is already locked by a concurrent drain, skip it
                # here -- the drain sweep owns it now.
                result = await db.execute(
                    select(WebhookDelivery)
                    .where(WebhookDelivery.id == did)
                    .with_for_update(skip_locked=True)
                )
                delivery = result.scalar_one_or_none()
                if delivery is None or delivery.status != "pending":
                    continue
                ep = await db.get(WebhookEndpoint, delivery.endpoint_id)
                if ep is None or not ep.is_active:
                    delivery.status = "dlq"
                    delivery.next_attempt_at = None
                    delivery.error_message = "Endpoint missing or inactive"
                    await db.commit()
                    continue
                url, body_bytes, sig_header = rebuild_signed_body(ep, delivery)
                if url is None:
                    # Bug-8557: no authoritative destination — never guess.
                    _dlq_incoherent_delivery(delivery)
                    await db.commit()
                    continue
                await attempt_delivery(delivery, url, body_bytes, sig_header)
                await db.commit()
    except Exception:
        logger.exception(
            "Async webhook delivery failed for tenant=%s", tenant_id,
        )


async def backfill_empty_event_filters(tenant_id: str) -> int:
    """Bug-6849: one-time sweep that logs and backfills endpoints stored with
    ``event_filters=[]`` (empty list) before the fail-closed semantic change
    in Bug-6313.

    Before Bug-6313, an empty ``event_filters`` list meant "receive everything".
    After the change, empty means "receive nothing" (fail-closed). Endpoints
    that were created under the old semantic and stored with ``[]`` silently
    stopped receiving any events with no operator signal.

    This sweep:
    1. Finds all active endpoints whose ``event_filters`` is empty (``[]``)
       or NULL.
    2. Logs each one as a warning so operators can audit the change.
    3. Backfills ``event_filters`` to ``["*"]`` (wildcard = all events) to
       restore the original intent.

    Returns the number of endpoints backfilled. Idempotent: a second call
    finds zero matches.
    """
    backfilled = 0
    try:
        async for db in get_tenant_db(tenant_id):
            result = await db.execute(
                select(WebhookEndpoint).where(
                    WebhookEndpoint.is_active == True  # noqa: E712
                )
            )
            endpoints = result.scalars().all()
            for ep in endpoints:
                # An endpoint with event_filters that is None, empty list, or
                # missing should be backfilled to ["*"].
                if ep.event_filters is None or ep.event_filters == []:
                    logger.warning(
                        "Bug-6849: webhook endpoint %s (%s) has empty "
                        "event_filters — backfilling to ['*'] (wildcard). "
                        "Under the pre-Bug-6313 semantic this endpoint "
                        "received ALL events; the fail-closed change silently "
                        "stopped delivery. Review and narrow filters if the "
                        "wildcard is not intended.",
                        ep.id,
                        # Bug-8430 (related-defect sweep) -- application logs
                        # are shipped, retained and searched far beyond this
                        # process; a webhook URL commonly embeds a bearer
                        # token, so log the sanitised host hint, never the
                        # full URL.
                        redact_url_for_display(ep.url),
                    )
                    ep.event_filters = [WILDCARD]
                    backfilled += 1
            if backfilled:
                await db.commit()
    except Exception:
        logger.exception(
            "Bug-6849 backfill sweep failed for tenant=%s", tenant_id,
        )
    return backfilled
