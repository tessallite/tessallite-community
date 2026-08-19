"""PostgreSQL advisory lock for distributed job deduplication.

Wraps ``pg_try_advisory_lock`` / ``pg_advisory_unlock`` so that only one
instance of a scheduled job runs at a time, even if ``maxScale > 1`` or
the service is started locally alongside Docker.

Usage::

    async for db in get_system_db():
        async with advisory_lock(db, "optimizer:daily_sweep") as acquired:
            if not acquired:
                logger.info("Skipping — another instance holds the lock")
                return
            await _do_daily_sweep(db)

Bug-6604 (Fable F-1, same root cause as the refresh locks Bug-6570/6548/6104):
this guard previously took the session-scoped ``pg_try_advisory_lock`` on the
pooled system-DB ORM session and then committed that session (F-012-17) to avoid
``idle_in_transaction`` — but a pooled ``AsyncSession.commit()`` RETURNS the
lock-bearing connection to the pool, and a PostgreSQL session advisory lock is
held by the CONNECTION. The sweeps share the ``SystemSessionLocal`` pool with
the every-minute webhook drain and co-firing hourly jobs, so another task could
check out the lock-bearing connection between the post-acquire commit and the
sweep's next statement; the exit unlock then ran on a different connection,
returned False, and was swallowed — the sweep lock leaked for up to
``pool_recycle`` seconds, making every subsequent refresh/pocket/SLA sweep skip
("another instance holds the lock") platform-wide.

Fix: the lock now lives on a DEDICATED connection checked out from the session's
engine and held open for the whole ``async with`` body, released only on exit
(``shared.refresh_lock_core.dedicated_advisory_lock_optional``). It is decoupled
from the ORM session's commit-driven connection churn; the dedicated connection
commits once after acquiring so it never sits idle-in-transaction (preserving the
F-012-17 intent) without touching the caller's session.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from shared.refresh_lock_core import dedicated_advisory_lock_optional, lock_id as _lock_id

logger = logging.getLogger(__name__)


@asynccontextmanager
async def advisory_lock(
    session: AsyncSession, name: str,
) -> AsyncIterator[bool]:
    key = _lock_id(name)
    async with dedicated_advisory_lock_optional(session, key) as acquired:
        if acquired:
            logger.debug("advisory_lock(%s) acquired (id=%d)", name, key)
        else:
            logger.debug("advisory_lock(%s) not acquired — another holder", name)
        yield acquired
