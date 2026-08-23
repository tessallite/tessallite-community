"""Notification delivery retention — bounded purge of delivery records.

Bug-8385: ``notification_deliveries`` (added by Bug-8053 so an email/Slack
outcome is visible in the product rather than only in application logs) had no
retention lifecycle. ``webhook_deliveries`` has one, audit events and query
logs have one, this table had none — so every tenant's ``_meta`` schema grew a
row per notification attempt forever.

The growth is not theoretical. The dispatcher writes a ``failed`` record for
every misconfiguration skip (SMTP unset, no recipients, no webhook URL) and
does so BEFORE claiming the send-dedup window, deliberately — a broken channel
must always be operator-visible, and consuming the window would suppress the
next real alert (Bug-7340). So the default configuration (``SMTP_HOST`` unset)
plus one enabled route produces a steady stream of rows that nothing throttles
and nothing removed.

This module holds the single implementation of that purge so the scheduler
sweep and any future maintenance path share one rule, mirroring
``shared/webhooks/retention.py``.

Every row is eligible on age alone. Unlike a webhook delivery there is no
in-flight state to protect: ``dispatch_alert`` writes a
``NotificationDelivery`` only once an attempt has reached a TERMINAL outcome
(``sent`` on a genuine successful send, ``failed`` on a send that raised or a
channel that was skipped as misconfigured). Filtering on a status allow-list
would therefore be a trap rather than a safeguard — any status added later
would be silently exempted from retention and grow without bound again.

All operations are scoped to one tenant DB session.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import NotificationDelivery

logger = logging.getLogger(__name__)

# Fallback window when a caller passes no explicit retention. Mirrors the
# ``scheduler.notification_delivery_retention_days`` registry default.
DEFAULT_NOTIFICATION_DELIVERY_RETENTION_DAYS = 30


async def purge_expired_notification_deliveries(
    db: AsyncSession,
    retention_days: int = DEFAULT_NOTIFICATION_DELIVERY_RETENTION_DAYS,
    *,
    batch_size: int = 5000,
    max_batches: int = 20,
    tenant_slug: str = "",
) -> int:
    """Purge notification delivery records older than the retention window.

    Deletes rows whose ``created_at`` predates the cutoff, in batches of
    ``batch_size``, committing each batch, up to ``max_batches`` batches per
    call. Returns the total number of rows deleted. The bounded-batch shape is
    the same one Bug-7335 established for every other purge in this codebase:
    a single sweep never opens an unbounded DELETE that locks the table or
    builds a huge transaction, and the next sweep continues a large backlog.

    ``retention_days <= 0`` disables the purge (records kept indefinitely).
    """
    if retention_days <= 0:
        return 0
    if batch_size <= 0 or max_batches <= 0:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    total = 0
    for _ in range(max_batches):
        ids = (
            await db.execute(
                select(NotificationDelivery.id)
                .where(NotificationDelivery.created_at < cutoff)
                .limit(batch_size)
            )
        ).scalars().all()
        if not ids:
            break
        result = await db.execute(
            sa_delete(NotificationDelivery).where(NotificationDelivery.id.in_(ids))
        )
        await db.commit()
        # Count rows actually deleted, not rows selected: a concurrent purge
        # could have removed some already. rowcount can be -1/None on drivers
        # that do not report it — fall back to the selected count then.
        deleted = getattr(result, "rowcount", None)
        total += deleted if deleted is not None and deleted >= 0 else len(ids)
        # Short of a full batch of eligible ids means the set is drained.
        if len(ids) < batch_size:
            break

    if total:
        logger.info(
            "Notification delivery retention tenant=%s purged=%s "
            "retention_days=%s",
            tenant_slug, total, retention_days,
        )
    return total
