"""Per-Named-Query refresh lock — one destructive materialisation per query.

Sibling of ``shared.pocket_refresh_lock`` (Bug-6104 / Fable F-1): the lock is
held on a DEDICATED connection via ``shared.refresh_lock_core``, so it survives
every commit/rollback the refresh performs on the ORM session (including the
``invalidating`` status commit). It shares the ONE PostgreSQL advisory-lock
space but is keyed ``refresh:named_query:{id}`` so a Named Query refresh and a
pocket refresh of the same UUID never collide.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from shared.refresh_lock_core import dedicated_advisory_lock, lock_id as _lock_id


class NamedQueryRefreshInFlightError(RuntimeError):
    """Another refresh/build of the same Named Query is already executing."""

    def __init__(self, named_query_id: object):
        self.named_query_id = named_query_id
        super().__init__(
            f"A refresh for Named Query {named_query_id} is already in flight"
        )


def _key(named_query_id: object) -> int:
    return _lock_id(f"refresh:named_query:{named_query_id}")


@asynccontextmanager
async def named_query_refresh_lock(
    db: AsyncSession, named_query_id: object
) -> AsyncIterator[None]:
    """Hold the per-Named-Query refresh lock across the whole materialisation.

    Raises ``NamedQueryRefreshInFlightError`` when the lock is already held by
    another session. Re-entrant on the same ``db``.
    """
    async with dedicated_advisory_lock(
        db,
        _key(named_query_id),
        on_conflict=lambda: NamedQueryRefreshInFlightError(named_query_id),
    ):
        yield
