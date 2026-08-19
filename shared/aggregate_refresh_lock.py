"""Per-aggregate refresh/build lock — one destructive CTAS per aggregate.

Originally introduced as ``scheduler/src/jobs/refresh_lock.py`` (B17 round-2
Finding 3): the hourly refresh sweep, the SLA monitor's retry, and the manual
API trigger all reach ``full_refresh_aggregate`` (a DROP TABLE + CTAS) through
different code paths with no shared guard, so a slow scheduled refresh could
collide with an SLA-triggered duplicate of the same DROP+CTAS on the same
physical table.

H12 (F-009-04 + B17 round-2 handoff) hoisted the helper to ``shared/`` so the
optimizer's create path can participate in the SAME per-aggregate advisory
lock the scheduler refresh executors take.

Bug-6570 / Bug-6548 (Fable F-005 sibling of the confirmed pocket bug
Bug-6104 / F-005-01): the refresh executors first took a *transaction*-scoped
``pg_try_advisory_xact_lock`` (released on any commit), then a *session*-scoped
``pg_try_advisory_lock`` on the ORM session's connection. Fable F-1 proved the
latter is ALSO broken: the pooled ``AsyncSession`` returns its connection on
every ``commit()`` and a PostgreSQL session advisory lock is bound to the
CONNECTION, so a mid-materialisation commit moves the ORM session onto a
different connection while the lock stays on the returned one — the exit unlock
runs on the wrong connection, leaking the lock and re-opening the concurrent
DROP+CTAS corruption.

Fix (Fable F-1 remediation): ``aggregate_refresh_lock`` now holds the advisory
lock on a DEDICATED connection (checked out from the session's engine and held
open for the whole materialisation) via ``shared.refresh_lock_core``. The lock
is independent of the ORM session's commit-driven connection churn, is released
(and its connection returned or invalidated) on exit, and is re-entrant on the
same session so the ``incremental -> full`` fallback does not deadlock itself.

The legacy transaction-scoped ``acquire_refresh_lock`` is retained for the
optimizer create path (``optimizer/src/lifecycle/creator.py``): that caller
takes the lock inside its single finalisation transaction, which has no
mid-flight commit, so a transaction-scoped lock correctly bounds exactly the
active-row -> first-refresh window it needs to guard. Session-scoped (dedicated
connection) and transaction-scoped advisory locks share ONE PostgreSQL lock
space keyed on the same id — independent of which connection queries it — so
the creator's xact lock and the scheduler's dedicated-connection lock still
mutually exclude one another on the same aggregate.

Callers that cannot proceed receive ``RefreshInFlightError`` and must report
the skip truthfully (the SLA monitor records ``skipped_in_flight``; the sweep
logs and moves on; the API returns 409).
"""
from __future__ import annotations

from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from contextlib import asynccontextmanager

from shared.refresh_lock_core import dedicated_advisory_lock, lock_id as _lock_id


class RefreshInFlightError(RuntimeError):
    """Another refresh/build of the same aggregate is already executing."""

    def __init__(self, aggregate_id: object):
        self.aggregate_id = aggregate_id
        super().__init__(
            f"A refresh for aggregate {aggregate_id} is already in flight"
        )


def _key(aggregate_id: object) -> int:
    return _lock_id(f"refresh:aggregate:{aggregate_id}")


async def acquire_refresh_lock(db: AsyncSession, aggregate_id: object) -> bool:
    """Try to take the per-aggregate refresh/build lock for this TRANSACTION.

    Transaction-scoped: released automatically on the next COMMIT or ROLLBACK
    on this connection. Correct ONLY when the caller holds the lock for the
    lifetime of a single transaction with no intermediate commit (the
    optimizer create path). Refresh executors that materialise across multiple
    statements/commits MUST use ``aggregate_refresh_lock`` instead.

    Shares the ONE per-aggregate advisory-lock id with ``aggregate_refresh_lock``
    (same ``_key``), so this xact lock and the scheduler's dedicated-connection
    session lock mutually exclude one another.

    Returns True when acquired, False when another session holds it.
    """
    stmt = text("SELECT pg_try_advisory_xact_lock(:id)").bindparams(
        id=_key(aggregate_id)
    )
    result = await db.execute(stmt)
    return bool(result.scalar())


@asynccontextmanager
async def aggregate_refresh_lock(
    db: AsyncSession, aggregate_id: object
) -> AsyncIterator[None]:
    """Async context manager: hold the per-aggregate refresh lock across the
    whole materialisation, release on exit. Raises ``RefreshInFlightError`` when
    the lock is already held by another session.

    Bug-6570 / Bug-6548 / Fable F-1: the lock is held on a DEDICATED connection
    (see ``shared.refresh_lock_core``), so it survives every commit/rollback the
    build performs on the ORM ``db`` session — the connection the lock lives on
    is never returned to the pool until this context manager exits. It is
    re-entrant on the same ``db`` (``incremental_refresh_aggregate`` re-enters
    ``full_refresh_aggregate``): the nested acquire reuses the outer dedicated
    connection and only balances a depth counter, so it cannot deadlock against
    itself.

    Usage::

        async with aggregate_refresh_lock(db, agg_def_id):
            db.add(run)
            await db.flush()
            await _materialise()       # DROP + CTAS; may commit internally
            await db.commit()          # lock survives this commit
        # lock released here
    """
    async with dedicated_advisory_lock(
        db,
        _key(aggregate_id),
        on_conflict=lambda: RefreshInFlightError(aggregate_id),
    ):
        yield
