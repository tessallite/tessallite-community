"""Outbound webhook dispatcher — Phase C2.

Public entry: ``dispatch_event(tenant_id, project_id, event_type, payload, *, conversation_id=None, turn_id=None, source_dlq_id=None)``.

Behaviour:
  - Reads the project's webhook URL + signing secret from ProjectAgentConfig.
  - Signs payload with HMAC-SHA256 over ``"<unix>.<json_body>"``;
    sets ``X-Tessallite-Signature: t=<unix>,v1=<hex>``. The timestamp,
    body ``emitted_at`` and signature are recomputed fresh for every
    attempt (Bug-5952 pattern from shared/webhooks/dispatcher.py) so
    the replay window is one backoff interval, not the whole delivery
    lifetime, and ``emitted_at`` always equals the signature ``t``.
  - Bug-8349: when no valid signing secret is configured, the payload is
    NEVER transmitted. This is a terminal configuration failure (the
    receiver could never authenticate an unsigned payload), not a transient
    one — one DLQ row is written with a clear missing-secret reason and no
    HTTP attempt is made at all. Mirrors the already-shipped fix in
    ``shared/webhooks/dispatcher.py`` (Bug-8056); a signing secret is also
    now auto-generated the first time a webhook URL is configured
    (``src/api/agent_config.py``), so this path should only be reachable for
    secrets that predate that change or an undecryptable/rotated-away key.
  - Retries up to 3 times with 10 / 60 / 300 second back-off on
    network failure or 5xx response.
  - Drops bodies larger than 32 KB straight to the DLQ table without
    any HTTP attempt.
  - On exhausted retries, writes a row to ``agent_webhook_dlq`` (or updates
    the row identified by ``source_dlq_id`` in place, when this call is a
    manual retry of an existing DLQ entry).
  - Bug-8350: the DLQ row never stores the raw destination URL (it may embed
    a bearer token / API key, in the path as much as the query string) —
    only a sanitised ``scheme://host[:port]`` hint (``target_host``). Manual
    retry reloads the live URL from ``ProjectAgentConfig``, never from the DLQ
    row.
  - Bug-8355: delivery is multi-tenant but the httpx connection pool is
    process-wide, so each tenant gets a bounded slice of it
    (``AGENT_WEBHOOK_MAX_CONNECTIONS_PER_TENANT``) and each attempt gets an
    absolute wall-clock deadline (``AGENT_WEBHOOK_ATTEMPT_DEADLINE_SEC``). One
    tenant's slow-trickling receiver can therefore delay only that tenant's
    own deliveries, never everyone else's.

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
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

import httpx
from sqlalchemy import select

from shared.config.settings import get_settings
from shared.db.models import AgentWebhookDlq, ProjectAgentConfig
from shared.db.session import get_tenant_db
# Bug-5951 — reuse the canonical placeholder-secret validator so the
# placeholder list cannot drift between the platform-wide and the
# agent-service dispatchers.
# Bug-8349 — reuse the canonical terminal-refusal reason so this dispatcher's
# DLQ record reads identically to the platform-wide one instead of drifting.
from shared.webhooks.dispatcher import (
    UNSIGNED_WEBHOOK_REFUSAL_REASON,
    is_valid_signing_secret,
)
# Bug-8411 — per-project event subscription. The same catalogue the config
# API validates against, so a name that can be stored is always a name this
# filter understands.
from shared.webhooks.agent_event_types import agent_event_subscribed
# Bug-8350 — reuse the shared URL-redaction helpers so the DLQ table never
# retains a raw, possibly secret-bearing destination URL. Bug-8407: the
# ``target_url_hash`` fingerprint (and its bcrypt call on every DLQ write) is
# gone — nothing ever read it, so it was a persisted derivative of a secret
# with no product value. ``target_host`` remains for operator correlation.
from shared.webhooks.redact import (
    redact_url_for_display,
    scrub_url_from_text,
)
# Bug-6045 — reuse the shared SSRF guard so the agent-service dispatcher
# cannot drift from the platform-wide one. `validate_webhook_url` is a
# name-level pre-flight; `ssrf_safe_transport` pins the resolved IP at
# connect time (closes the DNS-rebinding TOCTOU gap). Both are used so an
# agent webhook can never reach loopback / private / cloud-metadata hosts.
from shared.webhooks.ssrf import ssrf_safe_transport, validate_webhook_url

logger = logging.getLogger(__name__)


_MAX_BODY_BYTES = 32 * 1024
_BACKOFF_SCHEDULE_SEC = (10, 60, 300)
_REQUEST_TIMEOUT_SEC = 10
# Bug-7334 class (Bug-8349 R2 gate residual) — cap on the number of response
# bytes read from the receiver for a failure snippet. Without this, a
# hostile or misbehaving receiver drip-feeding a multi-GB body exhausts the
# shared agent-service process's memory: the previous ``client.post()`` call
# materialised the entire response body before returning, even though this
# dispatcher only ever reads the status code. Mirrors the already-correct
# guard in the platform-wide sibling (``shared/webhooks/dispatcher.py``); a
# per-read timeout alone is not sufficient because each arriving chunk
# resets it.
_MAX_RESPONSE_BYTES = 8 * 1024

# Bug-8355 — outbound webhook delivery is multi-tenant, and the pool is not.
# Every knob is settings-driven (shared/config/settings.py) rather than a
# literal here, because the right per-tenant share depends on how many tenants
# a deployment packs into one process.
_settings = get_settings()
_MAX_POOL_CONNECTIONS = _settings.AGENT_WEBHOOK_MAX_CONNECTIONS
_MAX_CONNECTIONS_PER_TENANT = _settings.AGENT_WEBHOOK_MAX_CONNECTIONS_PER_TENANT
# R2 reviewer finding 4 — clamped rather than trusted, so a misconfigured knob
# cannot turn the backstop into the primary timeout. R3 reviewer FIND-1: the
# floor is "one second above ONE per-phase timeout", not above their sum --
# connect/read/write are each 10s, so an honest receiver can legitimately take
# longer than the floor guarantees. That is deliberate: the default (30s) is
# what production runs, and a deadline is a wall-clock backstop, not a budget
# derived from the phase timeouts. The floor exists only to stop a hostile or
# fat-fingered value making every slow-but-honest receiver report
# ATTEMPT_DEADLINE_REASON instead of its own error.
_ATTEMPT_DEADLINE_SEC = max(
    _settings.AGENT_WEBHOOK_ATTEMPT_DEADLINE_SEC, _REQUEST_TIMEOUT_SEC + 1,
)
_POOL_ACQUIRE_TIMEOUT_SEC = _settings.AGENT_WEBHOOK_POOL_ACQUIRE_TIMEOUT_SEC

# Bug-8355 — these two failure modes are OURS, not the receiver's. Recording
# them with the receiver's own vocabulary ("connection error") is what made
# the original report say a starved delivery is "retried/backed-off/DLQ'd
# exactly like a real receiver-side failure, with no way to distinguish the
# two". An operator reading a DLQ row must be able to tell "your receiver
# broke" from "we throttled you" from "we gave up waiting on your receiver".
TENANT_BUDGET_EXHAUSTED_REASON = (
    "Not sent on this attempt: this project's tenant already has the maximum "
    "number of webhook deliveries in flight. A slow or unresponsive receiver "
    "holds its connection until it responds; delivery will be retried."
)
ATTEMPT_DEADLINE_REASON = (
    "Receiver did not complete the response within the delivery deadline "
    "(it may be sending the response body very slowly). The connection was "
    "released and delivery will be retried."
)

# Bug-5756 — shared httpx.AsyncClient with connection pooling, managed by
# the FastAPI app lifecycle (init_client / close_client called from main.py
# lifespan). Falls back to creating a one-shot client if the shared client
# is not yet initialised (e.g. during tests or edge cases).
_shared_client: Optional[httpx.AsyncClient] = None

# Bug-8355 — per-tenant concurrency budget in FRONT of the shared pool.
# Keyed by tenant, each entry pinned to the event loop that created it so a
# semaphore is never awaited from a different loop (tests create many).
#
# Growth (R1 reviewer F8): one entry per tenant that has ever dispatched a
# webhook in this process, each a small tuple + Semaphore (order of a hundred
# bytes), cleared by ``close_client`` at shutdown. Bounded by the tenant count
# of the deployment, which is the same order as the tenant schemas the process
# already holds sessions for — so no eviction policy is warranted. If a
# deployment ever reaches a tenant count where this matters, it has bigger
# per-tenant costs than this dict.
#
# Scope (R1 reviewer F9): the budget is PER PROCESS, not per deployment. Under
# horizontal scaling (Cloud Run, multiple replicas) one tenant can hold
# ``AGENT_WEBHOOK_MAX_CONNECTIONS_PER_TENANT`` connections on EACH instance.
# That is still the isolation property that matters here — the failure being
# prevented is one tenant consuming an entire instance's pool and starving
# every other tenant sharing it — but it is not a global rate limit, and
# nothing here should be read as one.
_tenant_budgets: dict[str, tuple[Any, asyncio.Semaphore]] = {}


def _client_timeout() -> httpx.Timeout:
    """Per-phase timeouts rather than one float.

    The previous single-float form applied 10s to connect/read/write AND to
    pool acquisition, so a caller blocked purely because every connection was
    held by another tenant waited the full request timeout before failing, and
    then failed as an indistinguishable transport error. A short, explicit
    ``pool`` timeout surfaces starvation as ``httpx.PoolTimeout`` quickly.
    """
    return httpx.Timeout(
        connect=_REQUEST_TIMEOUT_SEC,
        read=_REQUEST_TIMEOUT_SEC,
        write=_REQUEST_TIMEOUT_SEC,
        pool=_POOL_ACQUIRE_TIMEOUT_SEC,
    )


def _tenant_budget(tenant_id: str) -> asyncio.Semaphore:
    """The calling tenant's slice of the shared pool.

    No lock is needed: this function contains no ``await``, so under asyncio's
    single-threaded scheduling the get/compare/set sequence cannot interleave
    with another coroutine. The loop identity is compared because a semaphore
    created on a closed loop must not be reused on a new one.
    """
    loop = asyncio.get_running_loop()
    entry = _tenant_budgets.get(tenant_id)
    if entry is None or entry[0] is not loop:
        semaphore = asyncio.Semaphore(_MAX_CONNECTIONS_PER_TENANT)
        _tenant_budgets[tenant_id] = (loop, semaphore)
        return semaphore
    return entry[1]


async def init_client() -> None:
    """Initialise the shared httpx client. Called from the app lifespan."""
    global _shared_client
    _shared_client = httpx.AsyncClient(
        timeout=_client_timeout(),
        # Bug-6045 — every outbound webhook POST resolves and pins the target
        # IP through the SSRF-safe transport, so a stored URL that resolves to
        # a private / loopback / metadata address is refused at connect time.
        transport=ssrf_safe_transport(),
        limits=httpx.Limits(
            max_connections=_MAX_POOL_CONNECTIONS,
            # R1 reviewer F11 — derived from the pool size rather than left as
            # a stray literal beside a settings-driven one. A quarter of the
            # pool kept warm is the httpx-conventional ratio; at least one, so
            # a deployment that shrinks the pool to a handful still reuses
            # connections instead of reconnecting on every event.
            max_keepalive_connections=max(1, _MAX_POOL_CONNECTIONS // 4),
        ),
    )


async def close_client() -> None:
    """Close the shared httpx client. Called from the app lifespan."""
    global _shared_client
    if _shared_client is not None:
        await _shared_client.aclose()
        _shared_client = None
    # The budgets are pinned to the lifespan's event loop; drop them with it.
    _tenant_budgets.clear()


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


async def _send_and_read_status(
    client: httpx.AsyncClient,
    target_url: str,
    body_bytes: bytes,
    headers: dict[str, str],
) -> tuple[bool, Optional[int], Optional[str]]:
    """POST once via streaming and report the outcome without ever reading
    more than ``_MAX_RESPONSE_BYTES`` of the receiver's response body."""
    async with client.stream(
        "POST", target_url, content=body_bytes, headers=headers,
    ) as resp:
        status_code = resp.status_code
        if 200 <= status_code < 300:
            return True, status_code, None
        chunks: list[bytes] = []
        read_total = 0
        async for chunk in resp.aiter_bytes():
            chunks.append(chunk)
            read_total += len(chunk)
            if read_total >= _MAX_RESPONSE_BYTES:
                break
        error_text = b"".join(chunks)[:_MAX_RESPONSE_BYTES].decode(errors="replace")
        message = f"HTTP {status_code}: {error_text}" if error_text else f"HTTP {status_code}"
        return False, status_code, message[:500]


async def _bounded_send(
    client: httpx.AsyncClient,
    target_url: str,
    body_bytes: bytes,
    headers: dict[str, str],
) -> tuple[bool, Optional[int], Optional[str]]:
    """``_send_and_read_status`` under an absolute wall-clock deadline.

    Bug-8355 — httpx's ``read`` timeout is PER READ: every arriving chunk
    resets it, so a receiver drip-feeding one byte every few seconds never
    times out, never reaches ``_MAX_RESPONSE_BYTES``, and holds its pooled
    connection for as long as it keeps trickling (live-reproduced against the
    real dispatcher/httpx/httpcore stack with a raw socket server). The byte
    cap bounds MEMORY; only a total deadline bounds TIME.

    The deadline is deliberately larger than ``_REQUEST_TIMEOUT_SEC`` so it
    only ever fires on the trickle case; an ordinary slow-but-honest receiver
    still hits the normal per-phase timeout and reports the normal error.

    Known, accepted window (R1 reviewer F10): if the deadline fires while the
    stream context is closing AFTER a 2xx has already been read, the attempt is
    reported as failed and the retry loop re-sends. That is a DUPLICATE
    delivery, not a lost one, and outbound webhooks are at-least-once by
    construction (the retry schedule and the DLQ replay button both re-send).
    Erring toward a duplicate rather than toward holding the connection is the
    correct trade for the failure this bounds.
    """
    try:
        return await asyncio.wait_for(
            _send_and_read_status(client, target_url, body_bytes, headers),
            timeout=_ATTEMPT_DEADLINE_SEC,
        )
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning(
            "Agent webhook POST to %s exceeded the %ds delivery deadline; "
            "connection released",
            redact_url_for_display(target_url), _ATTEMPT_DEADLINE_SEC,
        )
        return False, None, ATTEMPT_DEADLINE_REASON


async def _post_once(
    tenant_id: str,
    target_url: str,
    body_bytes: bytes,
    sig_header: str,
    event_type: str,
) -> tuple[bool, Optional[int], Optional[str]]:
    # Bug-5951 pattern — only send the signature header when a real HMAC
    # was computed. A ``t=...,v1=`` header with an empty digest would
    # mislead the receiver into thinking the payload is signed.
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "X-Tessallite-Event": event_type,
    }
    if sig_header:
        headers["X-Tessallite-Signature"] = sig_header

    # Bug-8355 — take this tenant's slice of the shared pool BEFORE touching
    # httpx. The byte cap added for Bug-8349 R2 bounds memory per connection
    # but not wall-clock hold time: a receiver replying 500 and then sending
    # one byte every 3 seconds stays under the 10s per-read timeout and under
    # the 8 KB cap indefinitely, and holds its connection the whole time. With
    # one shared 20-connection pool and no per-tenant budget, four such
    # receivers in ONE tenant starve every other tenant in the process.
    #
    # Refusing to start is strictly better than queueing forever: the retry
    # loop already backs off and eventually DLQs, so a throttled attempt costs
    # a delayed delivery for the noisy tenant, not a lost one — while a
    # blocked attempt costs every other tenant their delivery.
    budget = _tenant_budget(tenant_id)
    try:
        await asyncio.wait_for(
            budget.acquire(), timeout=_POOL_ACQUIRE_TIMEOUT_SEC
        )
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning(
            "Agent webhook delivery for tenant %s throttled: the tenant's "
            "concurrent-delivery budget (%d) is fully in use",
            tenant_id, _MAX_CONNECTIONS_PER_TENANT,
        )
        return False, None, TENANT_BUDGET_EXHAUSTED_REASON

    try:
        client = _shared_client
        if client is None:
            # Fallback: if lifecycle hasn't initialised the shared client
            # yet (e.g. during tests), create a one-shot client.
            logger.debug("Shared httpx client not initialised — using one-shot client")
            # Bug-6045 — the fallback client must also pin the resolved IP.
            async with httpx.AsyncClient(
                timeout=_client_timeout(), transport=ssrf_safe_transport(),
            ) as client:
                return await _bounded_send(client, target_url, body_bytes, headers)
        return await _bounded_send(client, target_url, body_bytes, headers)
    except httpx.HTTPError as exc:
        return False, None, str(exc)
    except Exception as exc:
        # Bug-8349 R2 HIGH — this must not be narrowed back to
        # ``httpx.HTTPError``. Two confirmed escapes:
        #   1. ``shared/webhooks/ssrf.py``'s ``_SSRFSafeTransport`` calls the
        #      raw httpcore connection pool directly, with none of httpx's
        #      usual exception-mapping wrapper — a bare
        #      ``httpcore.ConnectError`` (DNS failure, connection refused,
        #      or an SSRF block raised by ``_SSRFSafeBackend``) is NOT an
        #      ``httpx.HTTPError`` subclass and would otherwise propagate
        #      straight out of this function.
        #   2. ``httpx.InvalidURL`` (e.g. a malformed port that
        #      ``validate_webhook_url`` failed to reject pre-flight) is
        #      raised by httpx's own URL parsing before the transport is
        #      even reached, and is also NOT an ``httpx.HTTPError``
        #      subclass.
        # Either escape used to defeat ``dispatch_event``'s entire "never
        # raises — logs and DLQs on failure" contract: the retry loop would
        # raise, the fire-and-forget background task would swallow it
        # unobserved, and NO DLQ row would ever be written. This function's
        # only job is "attempt one HTTP call and report the outcome as a
        # tuple", so every realistic failure of the underlying transport
        # belongs here, not just the ones httpx chose to wrap.
        logger.warning(
            # R2 reviewer finding 5 — the URL argument was already redacted,
            # but `exc` is an arbitrary transport exception whose own message
            # routinely quotes the request URL verbatim, and application logs
            # are shipped, retained and searched far beyond this process. This
            # is the same channel the lane hardened in the shared sibling's
            # `backfill_empty_event_filters`; the fix applies to every
            # affected component, not just the one the finding named.
            "Webhook POST to %s failed with an unexpected transport error: %s",
            redact_url_for_display(target_url),
            scrub_url_from_text(str(exc), target_url),
        )
        # R3 reviewer FIND-3 — scrub the RETURN value as well, exactly as the
        # shared sibling does. Every present consumer of this tuple scrubs
        # before persisting, so nothing leaks today; but the lane's own thesis
        # (and CLAUDE.md's shared-primitive discipline, quoted three lines
        # above) is that a fix lands on every affected component. A future
        # consumer must not inherit a raw credential from this branch.
        return False, None, scrub_url_from_text(str(exc)[:500], target_url)
    finally:
        # Bug-8355 — the budget MUST be released on every exit path, including
        # the two `except` returns above and a cancellation. Leaking a permit
        # would permanently shrink the tenant's budget until the process
        # restarts, which is the same starvation this fix exists to prevent,
        # just slower.
        budget.release()


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
    *,
    dlq_id: Optional[UUID] = None,
) -> None:
    """Write (or update) one DLQ row.

    Bug-8350: the raw ``target_url`` is never persisted — only a sanitised
    ``scheme://host[:port]`` hint (``target_host``, path dropped too: the
    reported repro embedded the secret in the path, not just the query
    string). ``last_error`` is scrubbed of the URL too, since an HTTP client's
    exception message — or the receiver's own echoed response body — can carry
    the request URL, in escaped renderings as well as raw (Bug-8357).

    When ``dlq_id`` is given (a manual retry of an existing DLQ entry that
    failed again) the existing row is updated in place instead of inserting a
    duplicate — the row keeps living under its original id until a retry
    actually succeeds and resolves it.
    """
    # Bug-8349 R2 HIGH, defense in depth — every branch of ``dispatch_event``
    # ends up here to record a failure, so this function is the single
    # choke point that must never raise regardless of caller. The
    # redaction calls below moved INSIDE this try (they used to run before
    # it) specifically so a future bug in ``redact_url_for_display`` or
    # ``scrub_url_from_text`` (already hardened against a malformed-port URL,
    # but "never say never") cannot itself become a second, harder-to-notice
    # way for an event to vanish with zero DLQ row.
    try:
        target_host = redact_url_for_display(target_url)
        scrubbed_error = scrub_url_from_text(last_error, target_url)
        async for db in get_tenant_db(tenant_id):
            row: Optional[AgentWebhookDlq] = None
            if dlq_id is not None:
                row = await db.get(AgentWebhookDlq, dlq_id)
            if row is not None:
                row.attempt_count = attempts
                row.last_status_code = last_status
                row.last_error = scrubbed_error
                row.target_host = target_host
                row.last_attempted_at = datetime.now(timezone.utc)
            else:
                row = AgentWebhookDlq(
                    project_id=project_id,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    event_type=event_type,
                    target_host=target_host,
                    payload=payload,
                    attempt_count=attempts,
                    last_status_code=last_status,
                    last_error=scrubbed_error,
                )
                db.add(row)
            await db.commit()
            return
    except Exception:
        logger.exception("Failed to persist webhook DLQ row")


async def _resolve_dlq_row(tenant_id: str, dlq_id: UUID) -> None:
    """Mark a DLQ row resolved after a confirmed signed 2xx delivery.

    Bug-8349 — a manual DLQ retry must only clear the row once delivery has
    actually succeeded. Marking it resolved before the retry attempt (the
    prior behaviour in ``src/api/webhooks.py``) meant a retry that failed
    again still looked resolved to the operator.
    """
    try:
        async for db in get_tenant_db(tenant_id):
            row = await db.get(AgentWebhookDlq, dlq_id)
            if row is not None and row.resolved_at is None:
                row.resolved_at = datetime.now(timezone.utc)
                await db.commit()
            return
    except Exception:
        logger.exception("Failed to resolve webhook DLQ row %s", dlq_id)


async def dispatch_event(
    tenant_id: str,
    project_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    *,
    conversation_id: Optional[UUID] = None,
    turn_id: Optional[UUID] = None,
    source_dlq_id: Optional[UUID] = None,
) -> None:
    """Fire-and-forget webhook dispatch. Safe to call from a background
    task. Never raises — logs and DLQs on failure.

    ``source_dlq_id``: set when this call is a manual retry of an existing
    DLQ row (``POST .../dlq/{id}/retry``). On success the row is resolved
    (Bug-8349 — only after a confirmed signed 2xx, never before the retry
    attempt); on a further failure the same row is updated in place instead
    of a duplicate DLQ row being inserted.
    """
    target_url: Optional[str] = None
    secret: Optional[str] = None
    event_filters: Any = None
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
            event_filters = getattr(cfg, "webhook_event_filters", None)
            break
    except Exception:
        logger.exception("Webhook dispatch could not load config")
        return

    if not target_url:
        return  # No webhook configured.

    # Bug-8411 — the project may subscribe to a subset of agent events. An
    # unsubscribed event is not a failure: it is the operator's explicit
    # choice, so it is dropped silently WITHOUT a DLQ row (a DLQ row means
    # "this should have been delivered and was not", and an operator staring
    # at a queue full of events they deselected would be actively
    # misleading). A NULL/absent filter list still means "everything", so no
    # existing receiver loses events on upgrade.
    #
    # The subscription governs AUTOMATIC emission only. A manual DLQ retry
    # (``source_dlq_id`` set) is an explicit per-row instruction from an
    # operator looking at that exact row, so it is always attempted: filtering
    # it would make the retry button a silent no-op on any row whose event
    # type was deselected after the row was queued, leaving the operator with
    # a row that neither delivers nor clears. Rows they no longer want are
    # removed with Discard.
    if source_dlq_id is None and not agent_event_subscribed(event_type, event_filters):
        logger.debug(
            "Agent webhook for project %s skipped event %s "
            "(not in the project's event subscription)",
            project_id, event_type,
        )
        return

    # Bug-6045 — fail CLOSED on a non-routable target: a URL persisted before
    # the config-write guard existed (or one whose host is a blocked internal
    # name / non-global literal IP) is refused here, before any HTTP attempt,
    # and parked in the DLQ for operator review rather than dispatched. The
    # connect-time transport guard above is the DNS-rebinding backstop; this
    # is the cheap early rejection with a clear DLQ reason.
    try:
        validate_webhook_url(target_url)
    except ValueError as exc:
        await _persist_dlq(
            tenant_id, project_id, conversation_id, turn_id,
            event_type, target_url, {"event_type": event_type, "payload": payload},
            attempts=0, last_status=None,
            last_error=f"SSRF: webhook URL rejected ({exc})",
            dlq_id=source_dlq_id,
        )
        logger.warning(
            "Agent webhook for project %s rejected by SSRF guard: %s",
            project_id, exc,
        )
        return

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
            dlq_id=source_dlq_id,
        )
        return

    # Bug-8349 — never sign with a missing/placeholder/undecryptable secret,
    # and never send unsigned either. Unlike a transient network failure this
    # will not resolve itself on retry (an operator must rotate the secret),
    # so it is refused terminally here, before any HTTP attempt, exactly
    # like shared/webhooks/dispatcher.py's attempt_delivery (Bug-8056). A 2xx
    # from an unsigned POST must never be recorded as delivered — the
    # receiver could never have authenticated it.
    can_sign = is_valid_signing_secret(secret)
    if not can_sign:
        logger.error(
            "Agent webhook for project %s refused: no valid signing secret "
            "configured (empty, placeholder, or undecryptable); payload NOT "
            "sent and recorded to the DLQ. Rotate the secret to restore "
            "signed delivery.",
            project_id,
        )
        await _persist_dlq(
            tenant_id, project_id, conversation_id, turn_id,
            event_type, target_url, body,
            attempts=0, last_status=None,
            last_error=UNSIGNED_WEBHOOK_REFUSAL_REASON,
            dlq_id=source_dlq_id,
        )
        return

    attempts = 0
    last_status: Optional[int] = None
    last_error: Optional[str] = None
    for delay in (0, *_BACKOFF_SCHEDULE_SEC):
        if delay:
            await asyncio.sleep(delay)
        attempts += 1
        # Bug-5952 pattern — recompute the timestamp, body ``emitted_at``
        # and signature for every attempt so the HMAC replay window is
        # one backoff interval, not the entire (up to ~6 minute)
        # delivery lifetime. The body is rebuilt so ``emitted_at``
        # always equals the signature ``t`` (mirrors shared
        # rebuild_signed_body, F-022-11); the 32 KB size guard above
        # stays pre-loop because the encoded length is timestamp-stable.
        unix_ts = int(time.time())
        body["emitted_at"] = unix_ts
        body_bytes = json.dumps(body, separators=(",", ":"), default=str).encode()
        sig_header = ""
        if can_sign and secret is not None:
            sig_header = _sign(secret, unix_ts, body_bytes)
        try:
            ok, status_code, error = await _post_once(
                tenant_id, target_url, body_bytes, sig_header, event_type
            )
        except Exception as exc:
            # Bug-8349 R2 HIGH, defense in depth — ``_post_once`` above now
            # catches the full realistic transport failure surface itself
            # (httpcore.ConnectError, httpx.InvalidURL, etc.), so this
            # should be unreachable in practice. It stays as a narrow
            # per-attempt fallback — scoped to ONLY the POST call, not the
            # whole loop — specifically so it can never reclassify an
            # ALREADY-SUCCEEDED delivery as failed: if this were a broad
            # try/except wrapping the success branch below too (a prior
            # draft of this fix did exactly that), a hypothetical future
            # failure in ``_resolve_dlq_row`` after a confirmed 2xx would
            # incorrectly write a "failed" DLQ row for an event that was
            # actually delivered. Treating this attempt as failed and
            # continuing the retry loop is always the safe interpretation
            # of an unexpected exception from the one call whose entire
            # job is "attempt delivery and report the outcome".
            logger.exception(
                "Unexpected error posting agent webhook for project %s "
                "(event %s, attempt %d) — treating as a failed attempt",
                project_id, event_type, attempts,
            )
            ok, status_code, error = False, None, f"unexpected dispatch error: {exc}"
        last_status = status_code
        last_error = error
        if ok:
            if source_dlq_id is not None:
                # Bug-8349 — resolve the originating DLQ row only now,
                # after a confirmed signed 2xx. Never before the
                # attempt.
                await _resolve_dlq_row(tenant_id, source_dlq_id)
            return
        # 4xx (except 408/429) is a permanent client error — don't retry.
        if status_code is not None and 400 <= status_code < 500 and status_code not in (408, 429):
            break

    await _persist_dlq(
        tenant_id, project_id, conversation_id, turn_id,
        event_type, target_url, body,
        attempts=attempts, last_status=last_status, last_error=last_error,
        dlq_id=source_dlq_id,
    )
