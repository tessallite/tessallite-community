"""Alert dispatcher — routes lifecycle events to configured notification channels.

Given an event type and tenant DB session, looks up matching NotificationRoute
entries and dispatches to the appropriate sender (SMTP email or Slack webhook).
"""
from __future__ import annotations

import hashlib
import html
import logging
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config.settings import get_settings as _get_settings
from shared.db.models import (
    NotificationDelivery,
    NotificationDispatchDedup,
    NotificationRoute,
)

logger = logging.getLogger(__name__)

EVENT_TYPES = frozenset({
    "refresh_failure",
    "schema_drift",
    "sla_breach",
    "query_failure_spike",
    "aggregate_retired",
    # F-012-11: refresh dependencies are an ordering primitive, not a data-flow
    # edge — an upstream failure does not make the downstream stale. The event
    # is named for what actually happened (an upstream refresh failed) rather
    # than the misleading "stale dependency".
    "refresh_upstream_failed",
    # Bug-8114: pocket refresh failures are a distinct acceleration-asset
    # failure domain from aggregate refresh failures (separate event type so
    # operators can route/filter the two independently), dispatched from
    # shared/pocket/refresh.py::refresh_pocket_definition.
    "pocket_refresh_failure",
})

_DEDUP_WINDOW_SECONDS = 300


def _resolve_slack_webhook_url(config: dict | None) -> str:
    """Recover the plaintext Slack webhook URL from channel_config.

    Bug-5945: new routes store the URL encrypted under
    ``webhook_url_encrypted``; legacy routes may still have it in plaintext
    under ``webhook_url``. Try encrypted first, fall back to plaintext.
    """
    if not config:
        return ""
    encrypted = config.get("webhook_url_encrypted")
    if encrypted:
        try:
            from shared.security.credential_crypto import decrypt_str
            return decrypt_str(encrypted.encode("utf-8") if isinstance(encrypted, str) else encrypted)
        except Exception:
            logger.warning("Failed to decrypt Slack webhook URL; falling back to plaintext")
    return config.get("webhook_url", "")


def _dedup_key(
    event_type: str,
    channel_type: str,
    target: str,
    *,
    route_id: UUID | str,
    incident_key: str | None,
) -> str:
    """Build the durable dedup key for a notification claim.

    F-022-03: the key MUST include a stable per-incident identity
    (``incident_key``) — otherwise two DISTINCT incidents of the same event
    type, routed to the same destination within the dedup window, collapse into
    one and the second is silently suppressed. The incident key is derived by
    the caller from the failing object identity (model/aggregate/pocket id) plus
    the relevant content, so genuinely-repeated copies of the SAME incident
    still share a key (intended suppression) while distinct incidents do not.

    ``incident_key`` may be ``None`` only for a legacy/coarse caller that has no
    object identity; that preserves the prior event-type-level collapse for that
    caller alone rather than changing its behaviour silently.
    """
    incident = incident_key if incident_key is not None else "-"
    return f"{event_type}:{route_id}:{channel_type}:{target}:{incident}"


async def _claim_dispatch(db: AsyncSession, key: str) -> bool:
    """Atomically claim the dedup window for ``key`` across replicas.

    Returns True if this caller should send (no send within the window),
    False if a recent send already occurred. Implemented as a single
    ``INSERT ... ON CONFLICT DO UPDATE`` that only refreshes
    ``last_dispatched_at`` (and reports the row) when the stored timestamp
    is older than the window. Because the upsert is atomic at the DB level,
    exactly one of N concurrent replicas wins the window.
    """
    window = f"{_DEDUP_WINDOW_SECONDS} seconds"
    stmt = (
        pg_insert(NotificationDispatchDedup)
        .values(dedup_key=key, last_dispatched_at=func.now())
        .on_conflict_do_update(
            index_elements=[NotificationDispatchDedup.dedup_key],
            set_={"last_dispatched_at": func.now()},
            where=(
                NotificationDispatchDedup.last_dispatched_at
                < func.now() - text(f"interval '{window}'")
            ),
        )
        .returning(NotificationDispatchDedup.dedup_key)
    )
    result = await db.execute(stmt)
    claimed = result.scalar_one_or_none() is not None
    # The claim must be durable before we send, so a crash after sending
    # does not re-open the window for another replica.
    await db.commit()
    return claimed


async def _release_dispatch(db: AsyncSession, key: str) -> None:
    """Re-open the dedup window for ``key`` after a failed send (F-022-03).

    The claim is committed before the SMTP/Slack send so concurrent replicas do
    not double-send. If THIS send then fails, the claim must not consume the
    window — otherwise a single transient sender failure suppresses the next
    eligible notification for the whole window. Rewinding
    ``last_dispatched_at`` past the window lets the next attempt re-claim.
    Best-effort: a failure to release is logged, never raised.
    """
    window = f"{_DEDUP_WINDOW_SECONDS} seconds"
    try:
        await db.execute(
            pg_insert(NotificationDispatchDedup)
            .values(
                dedup_key=key,
                last_dispatched_at=func.now() - text(f"interval '{window}'") - text("interval '1 second'"),
            )
            .on_conflict_do_update(
                index_elements=[NotificationDispatchDedup.dedup_key],
                set_={
                    "last_dispatched_at": func.now()
                    - text(f"interval '{window}'")
                    - text("interval '1 second'")
                },
            )
        )
        await db.commit()
    except Exception:
        logger.exception("Failed to release dedup claim for key %s", key)


async def _record_delivery(
    db: AsyncSession,
    *,
    route: NotificationRoute,
    project_id: UUID | str | None,
    event_type: str,
    channel_type: str,
    target: str,
    status: str,
    error: str | None,
) -> None:
    """Persist a durable, operator-visible record of one notification attempt.

    Bug-8053 (F-022-04): an email/Slack delivery outcome must be visible in the
    product, not only in application logs. ``status='sent'`` is written ONLY for
    a genuine successful send; every failed send and every misconfiguration skip
    (SMTP unset, no recipients, no webhook URL) is written as ``status='failed'``
    with a bounded reason, so a broken channel can no longer fail indefinitely
    without appearing anywhere an operator looks.

    ``target`` must be a NON-SECRET destination hint (joined recipients for
    email, a hashed webhook URL for Slack) — never a plaintext Slack secret.

    Best-effort: a failure to persist the record is logged, never raised.
    Recording evidence must not turn a delivered notification into an error, nor
    mask the send outcome it is recording.
    """
    try:
        db.add(
            NotificationDelivery(
                route_id=getattr(route, "id", None),
                project_id=project_id,
                event_type=event_type,
                channel_type=channel_type,
                target=(target or "")[:512],
                status=status,
                error_message=(str(error)[:1000] if error else None),
            )
        )
        await db.commit()
    except Exception:
        logger.exception(
            "Failed to persist notification delivery record "
            "(route=%s, channel=%s, status=%s)",
            getattr(route, "id", None), channel_type, status,
        )


async def dispatch_alert(
    db: AsyncSession,
    *,
    event_type: str,
    project_id: UUID | str | None = None,
    subject: str,
    body_html: str,
    body_text: str | None = None,
    slack_text: str | None = None,
    slack_blocks: list[dict] | None = None,
    incident_key: str | None = None,
) -> int:
    """Dispatch a lifecycle alert to configured notification channels.

    ``incident_key`` (F-022-03) is a stable per-incident identity — typically
    the failing object id plus a short content discriminator — so DISTINCT
    incidents of the same event type are not collapsed by dedup, while repeated
    copies of the SAME incident are suppressed. Callers that raise alerts for a
    specific object MUST pass it.
    """
    if event_type not in EVENT_TYPES:
        logger.warning("Unknown alert event type: %s", event_type)
        return 0

    body_html = html.escape(body_html)

    # Bug-7332: when dispatching for a specific project, first look for
    # project-scoped routes. If none exist, fall back to tenant-global routes
    # (project_id IS NULL) so tenant-wide subscriptions cover projects that
    # have no routes of their own. For a tenant-wide dispatch (project_id=None)
    # only tenant-global routes are considered.
    query = (
        select(NotificationRoute)
        .where(NotificationRoute.event_type == event_type)
        .where(NotificationRoute.enabled.is_(True))
    )
    if project_id is None:
        query = query.where(NotificationRoute.project_id.is_(None))
    else:
        query = query.where(NotificationRoute.project_id == project_id)

    result = await db.execute(query)
    routes = list(result.scalars().all())

    # Bug-7332 fallback: project-scoped dispatch found no routes -- try
    # tenant-global routes so an admin who subscribes at the tenant level
    # covers projects created after the subscription.
    if not routes and project_id is not None:
        fallback_query = (
            select(NotificationRoute)
            .where(NotificationRoute.event_type == event_type)
            .where(NotificationRoute.enabled.is_(True))
            .where(NotificationRoute.project_id.is_(None))
        )
        result = await db.execute(fallback_query)
        routes = list(result.scalars().all())

    if not routes:
        return 0

    sent = 0

    for route in routes:
        # Whether THIS route already produced a delivery record on its own
        # branch. Guards the outer-except safety net below from writing a second
        # (duplicate) record when a send-failure branch already recorded and
        # re-raised. Reset per route.
        recorded = False
        try:
            if route.channel_type == "email":
                recipients = route.channel_config.get("recipients", [])
                # Non-secret destination hint for the durable delivery record.
                target = ",".join(sorted(recipients)) if recipients else "(no recipients)"
                if not recipients:
                    # Bug-8053: a route that reaches dispatch with no recipients
                    # is a misconfiguration that would otherwise be dropped
                    # silently. Record it as a failed delivery so an operator
                    # can see the route never fires.
                    await _record_delivery(
                        db, route=route, project_id=project_id,
                        event_type=event_type, channel_type="email",
                        target=target, status="failed",
                        error="Email route has no recipients configured.",
                    )
                    continue

                # Bug-7340: check SMTP availability BEFORE claiming the dedup
                # window. If SMTP is not configured the claim must not be
                # consumed, otherwise the next real alert within the 5-minute
                # window is suppressed by a no-op send.
                from shared.alerting.smtp_sender import (
                    send_email,
                )
                _smtp_host = getattr(_get_settings(), "SMTP_HOST", "")
                if not _smtp_host:
                    logger.warning(
                        "SMTP not configured; skipping email alert for "
                        "route %s (%s) without consuming dedup window",
                        route.id, event_type,
                    )
                    # Bug-8053: SMTP-not-configured is exactly the "broken
                    # channel fails indefinitely without appearing in the
                    # product" gap. Record a durable failed delivery so it is
                    # operator-visible, while still not consuming the dedup
                    # window (Bug-7340).
                    await _record_delivery(
                        db, route=route, project_id=project_id,
                        event_type=event_type, channel_type="email",
                        target=target, status="failed",
                        error="SMTP is not configured (SMTP_HOST unset); "
                              "email notification could not be sent.",
                    )
                    continue

                key = _dedup_key(
                    event_type,
                    "email",
                    ",".join(sorted(recipients)),
                    route_id=route.id,
                    incident_key=incident_key,
                )
                if not await _claim_dispatch(db, key):
                    logger.debug("Dedup: skipping email for %s", key)
                    continue

                try:
                    await send_email(
                        to=recipients,
                        subject=f"[Tessallite] {subject}",
                        body_html=body_html,
                        body_text=body_text,
                    )
                except Exception as send_exc:
                    # F-022-03: a failed send must not consume the dedup window,
                    # otherwise the next eligible notification is suppressed.
                    await _release_dispatch(db, key)
                    # Bug-8053: persist a durable failed record before
                    # re-raising, so an operator can see and prove the failure.
                    await _record_delivery(
                        db, route=route, project_id=project_id,
                        event_type=event_type, channel_type="email",
                        target=target, status="failed", error=str(send_exc),
                    )
                    recorded = True
                    raise
                # Bug-8053: only a genuine successful send is recorded as sent.
                await _record_delivery(
                    db, route=route, project_id=project_id,
                    event_type=event_type, channel_type="email",
                    target=target, status="sent", error=None,
                )
                sent += 1

            elif route.channel_type == "slack":
                # Bug-5945: Slack webhook URLs may be encrypted at rest.
                # Try the encrypted field first, fall back to legacy plaintext.
                webhook_url = _resolve_slack_webhook_url(route.channel_config)
                if not webhook_url:
                    # Bug-8053: a Slack route with no webhook URL is a
                    # misconfiguration that would otherwise be dropped silently.
                    # Record it as a failed delivery. Target is a fixed
                    # non-secret marker (there is no URL to hash).
                    await _record_delivery(
                        db, route=route, project_id=project_id,
                        event_type=event_type, channel_type="slack",
                        target="slack:(unset)", status="failed",
                        error="Slack route has no webhook URL configured.",
                    )
                    continue
                # Bug-6000: hash the URL so the plaintext secret is never
                # persisted in the notification_dispatch_dedup table — and never
                # in the delivery record either (target below reuses the hash).
                url_hash = hashlib.sha256(webhook_url.encode()).hexdigest()[:16]
                target = f"slack:{url_hash}"
                key = _dedup_key(
                    event_type,
                    "slack",
                    url_hash,
                    route_id=route.id,
                    incident_key=incident_key,
                )
                if not await _claim_dispatch(db, key):
                    logger.debug("Dedup: skipping slack for %s", key)
                    continue

                from shared.alerting.slack_sender import send_slack
                try:
                    await send_slack(
                        webhook_url=webhook_url,
                        text=slack_text or subject,
                        blocks=slack_blocks,
                    )
                except Exception as send_exc:
                    # F-022-03: release the claim so a transient Slack failure
                    # does not suppress the next eligible notification.
                    await _release_dispatch(db, key)
                    # Bug-8053: persist a durable failed record before re-raising.
                    # Scrub the webhook URL out of the error text: a future
                    # sender change could surface the plaintext secret in the
                    # exception message, and this field is read by modeller-level
                    # operators (mirrors the Bug-5945/7341 redaction discipline).
                    safe_error = str(send_exc).replace(
                        webhook_url, "<redacted-webhook-url>"
                    )
                    await _record_delivery(
                        db, route=route, project_id=project_id,
                        event_type=event_type, channel_type="slack",
                        target=target, status="failed", error=safe_error,
                    )
                    recorded = True
                    # Scrub at the boundary: re-raise a SCRUBBED exception (with
                    # the original context suppressed) so no downstream consumer
                    # — the outer-except logger below, which ships to centralised
                    # logging/support bundles — can reintroduce the plaintext
                    # webhook secret the DB field was already scrubbed of.
                    raise RuntimeError(safe_error) from None
                # Bug-8053: only a genuine successful send is recorded as sent.
                await _record_delivery(
                    db, route=route, project_id=project_id,
                    event_type=event_type, channel_type="slack",
                    target=target, status="sent", error=None,
                )
                sent += 1

            else:
                # Bug-8053: an unknown channel type (e.g. a legacy/imported
                # route that predates _VALID_CHANNELS) would otherwise be
                # dropped with only a log line. Record it as failed so the
                # broken route is operator-visible.
                logger.warning("Unknown channel type: %s", route.channel_type)
                await _record_delivery(
                    db, route=route, project_id=project_id,
                    event_type=event_type,
                    channel_type=str(route.channel_type),
                    target="(unknown channel)", status="failed",
                    error=f"Unknown channel type: {route.channel_type!r}",
                )
                recorded = True

        except Exception as exc:
            logger.error(
                "Alert dispatch failed for route %s (%s/%s): %s",
                route.id, route.channel_type, route.event_type, exc,
            )
            # Bug-8053: an UNEXPECTED failure inside a route branch (e.g. a
            # NULL channel_config, a claim error) must still leave durable
            # operator evidence. Skip when the branch already recorded and
            # re-raised (a send failure) to avoid a duplicate row.
            if not recorded:
                await _record_delivery(
                    db, route=route, project_id=project_id,
                    event_type=event_type,
                    channel_type=str(getattr(route, "channel_type", "") or ""),
                    target="(dispatch error)", status="failed", error=str(exc),
                )

    return sent
