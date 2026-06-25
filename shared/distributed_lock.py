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
"""
from __future__ import annotations

import hashlib
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _lock_id(name: str) -> int:
    return int(hashlib.sha256(name.encode()).hexdigest()[:15], 16)


@asynccontextmanager
async def advisory_lock(
    session: AsyncSession, name: str,
) -> AsyncIterator[bool]:
    lock_id = _lock_id(name)
    result = await session.execute(
        text("SELECT pg_try_advisory_lock(:id)"), {"id": lock_id},
    )
    acquired = result.scalar()
    if not acquired:
        logger.debug("advisory_lock(%s) not acquired — another holder", name)
        yield False
        return
    # F-012-17: ``pg_try_advisory_lock`` is SESSION-scoped (not transaction-
    # scoped), so the lock outlives a commit. Commit immediately after acquiring
    # it so the session sits IDLE rather than IDLE IN TRANSACTION while the
    # multi-tenant sweep runs its per-tenant work on other sessions. A metadata
    # DB hardened with ``idle_in_transaction_session_timeout`` would otherwise
    # kill this session mid-sweep, silently dropping the lock and allowing a
    # second instance to start the same sweep. An idle (committed) session is
    # not affected by that timeout.
    try:
        await session.commit()
    except Exception:  # pragma: no cover - defensive; lock is still held
        logger.debug("advisory_lock(%s): post-acquire commit failed", name)
    logger.debug("advisory_lock(%s) acquired (id=%d)", name, lock_id)
    try:
        yield True
    finally:
        try:
            await session.execute(
                text("SELECT pg_advisory_unlock(:id)"), {"id": lock_id},
            )
            await session.commit()
            logger.debug("advisory_lock(%s) released", name)
        except Exception:  # pragma: no cover - session may already be closed
            logger.debug("advisory_lock(%s): unlock failed (session closed?)", name)
