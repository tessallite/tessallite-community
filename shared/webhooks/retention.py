"""Webhook delivery retention — bounded purge of terminal delivery rows.

Bug-6316: ``webhook_deliveries`` had no retention purge, so every delivered
(success) and dead-lettered (``dlq``) row accumulated forever, growing the
per-tenant table without bound. This module holds the single source of truth
for purging those terminal rows so the scheduler sweep (and any manual
maintenance endpoint) share one implementation.

Only TERMINAL deliveries are eligible:

  - ``delivered`` — successfully sent.
  - ``dlq``       — dead-lettered (retries exhausted or non-retryable).

``pending`` rows are in-flight (the scheduler drain retries them on their
backoff schedule) and are NEVER purged regardless of age.

The purge is bounded: it deletes in fixed-size batches, committing each batch,
up to ``max_batches`` per call, so a single sweep never opens an unbounded
DELETE that locks the table or builds a huge transaction. The caller can invoke
it again on the next sweep to continue draining a large backlog.

All operations are scoped to one tenant DB session.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import WebhookDelivery

logger = logging.getLogger(__name__)

# Fallback window when a caller passes no explicit retention. Mirrors the
# ``scheduler.webhook_delivery_retention_days`` registry default.
DEFAULT_WEBHOOK_DELIVERY_RETENTION_DAYS = 30

# Delivery statuses safe to purge. ``pending`` is deliberately excluded — those
# rows are still being retried by the scheduler drain.
_TERMINAL_STATUSES: tuple[str, ...] = ("delivered", "dlq")


async def purge_expired_deliveries(
    db: AsyncSession,
    retention_days: int = DEFAULT_WEBHOOK_DELIVERY_RETENTION_DAYS,
    *,
    batch_size: int = 5000,
    max_batches: int = 20,
    tenant_slug: str = "",
) -> int:
    """Purge terminal webhook deliveries older than the retention window.

    Deletes ``delivered`` / ``dlq`` rows whose ``created_at`` predates the
    cutoff, in batches of ``batch_size``, committing each batch, up to
    ``max_batches`` batches per call. Returns the total number of rows deleted.

    ``retention_days <= 0`` disables the purge (rows kept indefinitely). Does
    not touch ``pending`` (in-flight) deliveries.
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
                select(WebhookDelivery.id)
                .where(
                    WebhookDelivery.status.in_(_TERMINAL_STATUSES),
                    WebhookDelivery.created_at < cutoff,
                )
                .limit(batch_size)
            )
        ).scalars().all()
        if not ids:
            break
        result = await db.execute(
            sa_delete(WebhookDelivery).where(WebhookDelivery.id.in_(ids))
        )
        await db.commit()
        # Count rows actually deleted (rowcount), not rows selected: a
        # concurrent purge could have already removed some of the selected ids,
        # so ``len(ids)`` would over-report. rowcount can be -1/None on drivers
        # that do not report it — fall back to the selected count then.
        deleted = getattr(result, "rowcount", None)
        total += deleted if deleted is not None and deleted >= 0 else len(ids)
        # Short of a full batch of eligible ids means the set is drained.
        if len(ids) < batch_size:
            break

    if total:
        logger.info(
            "Webhook delivery retention tenant=%s purged=%s retention_days=%s",
            tenant_slug, total, retention_days,
        )
    return total
