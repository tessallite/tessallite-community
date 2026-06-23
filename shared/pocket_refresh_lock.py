"""Per-pocket refresh lock — one destructive materialisation per pocket."""
from __future__ import annotations

import hashlib

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class PocketRefreshInFlightError(RuntimeError):
    """Another refresh/build of the same pocket is already executing."""

    def __init__(self, pocket_id: object):
        self.pocket_id = pocket_id
        super().__init__(f"A refresh for pocket {pocket_id} is already in flight")


def _lock_id(name: str) -> int:
    """Stable 60-bit advisory-lock key matching aggregate refresh locks."""
    return int(hashlib.sha256(name.encode()).hexdigest()[:15], 16)


async def acquire_pocket_refresh_lock(db: AsyncSession, pocket_id: object) -> bool:
    """Try to take the per-pocket refresh/build lock for this transaction."""
    stmt = text("SELECT pg_try_advisory_xact_lock(:id)").bindparams(
        id=_lock_id(f"refresh:pocket:{pocket_id}")
    )
    result = await db.execute(stmt)
    return bool(result.scalar())
