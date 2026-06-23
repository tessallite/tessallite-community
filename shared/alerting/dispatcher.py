"""Alert dispatcher — routes lifecycle events to configured notification channels.

Given an event type and tenant DB session, looks up matching NotificationRoute
entries and dispatches to the appropriate sender (SMTP email or Slack webhook).
"""
from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import NotificationDispatchDedup, NotificationRoute

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
})

_DEDUP_WINDOW_SECONDS = 300


def _dedup_key(
    event_type: str,
    channel_type: str,
    target: str,
    *,
    route_id: UUID | str,
) -> str:
    return f"{event_type}:{route_id}:{channel_type}:{target}"


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
) -> int:
    if event_type not in EVENT_TYPES:
        logger.warning("Unknown alert event type: %s", event_type)
        return 0

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
    if not routes:
        return 0

    sent = 0

    for route in routes:
        try:
            if route.channel_type == "email":
                recipients = route.channel_config.get("recipients", [])
                if not recipients:
                    continue
                key = _dedup_key(
                    event_type,
                    "email",
                    ",".join(sorted(recipients)),
                    route_id=route.id,
                )
                if not await _claim_dispatch(db, key):
                    logger.debug("Dedup: skipping email for %s", key)
                    continue

                from shared.alerting.smtp_sender import send_email
                await send_email(
                    to=recipients,
                    subject=f"[Tessallite] {subject}",
                    body_html=body_html,
                    body_text=body_text,
                )
                sent += 1

            elif route.channel_type == "slack":
                webhook_url = route.channel_config.get("webhook_url", "")
                if not webhook_url:
                    continue
                key = _dedup_key(
                    event_type,
                    "slack",
                    webhook_url,
                    route_id=route.id,
                )
                if not await _claim_dispatch(db, key):
                    logger.debug("Dedup: skipping slack for %s", key)
                    continue

                from shared.alerting.slack_sender import send_slack
                await send_slack(
                    webhook_url=webhook_url,
                    text=slack_text or subject,
                    blocks=slack_blocks,
                )
                sent += 1

            else:
                logger.warning("Unknown channel type: %s", route.channel_type)

        except Exception as exc:
            logger.error(
                "Alert dispatch failed for route %s (%s/%s): %s",
                route.id, route.channel_type, route.event_type, exc,
            )

    return sent
