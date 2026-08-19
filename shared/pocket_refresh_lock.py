"""Per-pocket refresh lock -- one destructive materialisation per pocket.

Bug-6104 (Fable F-005-01): the original implementation used
``pg_try_advisory_xact_lock`` which is released on any COMMIT or ROLLBACK.
The pocket refresh flow commits mid-refresh (to persist the ``invalidating``
status and the running PocketRefreshRun row), so the lock was released
BEFORE the actual DROP+CTAS materialisation -- the critical section it
was supposed to guard. It was then changed to a session-scoped
``pg_try_advisory_lock`` on the ORM session's connection.

Fable F-1 (empirically proven live) showed the session-scoped variant is ALSO
broken: ``shared/db/session.py`` uses a pooled ``async_sessionmaker`` whose
``AsyncSession.commit()`` returns the physical connection to the pool, and a
PostgreSQL session advisory lock is held by the CONNECTION. The pocket refresh
commits ``invalidating`` mid-body, so the ORM session hops onto a different
connection while the lock stays on the returned one -- the exit unlock runs on
the wrong connection, leaking the lock and re-opening the concurrent DROP+CTAS
race Bug-6104 set out to fix.

Fix (Fable F-1 remediation): ``pocket_refresh_lock`` now holds the advisory
lock on a DEDICATED connection (checked out from the session's engine, held
open for the whole materialisation) via ``shared.refresh_lock_core``. The lock
is decoupled from the ORM session's commit-driven connection churn, released
(connection returned or invalidated) on exit, and re-entrant on the same
session. It shares the ONE PostgreSQL advisory-lock space keyed on the pocket
id, so it still mutually excludes any other holder on the same key.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from shared.refresh_lock_core import dedicated_advisory_lock, lock_id as _lock_id


class PocketRefreshInFlightError(RuntimeError):
    """Another refresh/build of the same pocket is already executing."""

    def __init__(self, pocket_id: object):
        self.pocket_id = pocket_id
        super().__init__(f"A refresh for pocket {pocket_id} is already in flight")


def _key(pocket_id: object) -> int:
    return _lock_id(f"refresh:pocket:{pocket_id}")


@asynccontextmanager
async def pocket_refresh_lock(
    db: AsyncSession, pocket_id: object
) -> AsyncIterator[None]:
    """Async context manager: hold the per-pocket refresh lock across the whole
    materialisation, release on exit. Raises ``PocketRefreshInFlightError`` when
    the lock is already held by another session.

    Bug-6104 / Fable F-1: the lock is held on a DEDICATED connection (see
    ``shared.refresh_lock_core``), so it survives every commit/rollback the
    refresh performs on the ORM ``db`` session -- including the ``invalidating``
    status commit -- because the connection the lock lives on is never returned
    to the pool until this context manager exits. Re-entrant on the same ``db``.

    Usage::

        async with pocket_refresh_lock(db, pocket.id):
            pocket.status = "invalidating"
            await db.commit()          # lock survives this commit
            await _do_materialisation()
            pocket.status = "fresh"
            await db.commit()          # lock survives this commit too
        # lock released here
    """
    async with dedicated_advisory_lock(
        db,
        _key(pocket_id),
        on_conflict=lambda: PocketRefreshInFlightError(pocket_id),
    ):
        yield
