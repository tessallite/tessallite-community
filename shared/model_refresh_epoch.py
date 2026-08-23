"""Model data-freshness epoch (F-017-03 / Bug-7989).

``Model.data_epoch`` is a monotonically increasing counter bumped on every
successful DATA refresh (aggregate full/incremental refresh, pocket refresh,
manual source refresh). The KPI evaluation cache folds it into its key, so the
next evaluation after a refresh reads the new epoch, forms a new key, and misses
the pre-refresh entry — on every model-service replica, without a cross-process
event bus (the same cross-replica bound the deployed-version keying already
relies on).

The bump is an ATOMIC in-place SQL increment (``SET data_epoch = data_epoch +
1``) rather than read-modify-write, so two concurrent refresh transactions on the
same model can never lose an increment. Callers invoke it inside the SAME
transaction that commits the refresh's ``last_refreshed_at`` / ``is_stale=False``
so the epoch and the fresh data commit together (the refresh operation and epoch
must be committed in a defined order — F-017-03 recommendation).
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Model


async def bump_data_epoch(db: AsyncSession, model_id: UUID) -> None:
    """Atomically increment ``Model.data_epoch`` for one model.

    Must be called inside the refresh transaction (before its commit) so the
    epoch advance and the freshly refreshed data are made visible together. A
    missing model row is a no-op (the refresh target was deleted concurrently).
    """
    await db.execute(
        update(Model)
        .where(Model.id == model_id)
        .values(data_epoch=Model.data_epoch + 1)
    )
