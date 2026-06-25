"""Per-aggregate refresh/build lock — one destructive CTAS per aggregate.

Originally introduced as ``scheduler/src/jobs/refresh_lock.py`` (B17 round-2
Finding 3): the hourly refresh sweep, the SLA monitor's retry, and the manual
API trigger all reach ``full_refresh_aggregate`` (a DROP TABLE + CTAS) through
different code paths with no shared guard, so a slow scheduled refresh could
collide with an SLA-triggered duplicate of the same DROP+CTAS on the same
physical table.

H12 (F-009-04 + B17 round-2 handoff) hoists the helper to ``shared/`` so the
optimizer's create path can participate in the SAME per-aggregate advisory
lock the scheduler refresh executors take. Any executor that builds or
rebuilds an aggregate's physical table acquires a PostgreSQL
transaction-scoped advisory lock keyed on the aggregate id before doing any
work. The lock lives on the tenant metadata session, whose transaction stays
open for the entire build (the run row is flushed at the start and committed
at the end), so the lock is held for exactly the build duration and is
released automatically on commit or rollback — no session/pool leakage.

Re-entrancy: advisory locks are re-entrant within one transaction, so
``incremental_refresh_aggregate`` falling back to ``full_refresh_aggregate``
on the same session re-acquires safely.

Callers that cannot proceed receive ``RefreshInFlightError`` and must report
the skip truthfully (the SLA monitor records ``skipped_in_flight``; the sweep
logs and moves on; the API returns 409).
"""
from __future__ import annotations

import hashlib

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class RefreshInFlightError(RuntimeError):
    """Another refresh/build of the same aggregate is already executing."""

    def __init__(self, aggregate_id: object):
        self.aggregate_id = aggregate_id
        super().__init__(
            f"A refresh for aggregate {aggregate_id} is already in flight"
        )


def _lock_id(name: str) -> int:
    """Stable 60-bit advisory-lock key (same scheme as shared.distributed_lock)."""
    return int(hashlib.sha256(name.encode()).hexdigest()[:15], 16)


async def acquire_refresh_lock(db: AsyncSession, aggregate_id: object) -> bool:
    """Try to take the per-aggregate refresh/build lock for this transaction.

    Returns True when acquired (held until the session's transaction commits
    or rolls back), False when another session holds it.
    """
    stmt = text("SELECT pg_try_advisory_xact_lock(:id)").bindparams(
        id=_lock_id(f"refresh:aggregate:{aggregate_id}")
    )
    result = await db.execute(stmt)
    return bool(result.scalar())
