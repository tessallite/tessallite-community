"""REPEATABLE READ snapshot helper for live-model serialisation (Bug-8380).

``snapshot_model`` issues many sequential SELECTs. Under PostgreSQL's default
READ COMMITTED isolation each SELECT observes the latest committed state, so a
definition writer that commits BETWEEN two of those SELECTs produces an
internally inconsistent snapshot — a measure referencing a column set from a
different instant. Originally fixed for Save in ``versions.py`` as a local
``_consistent_snapshot`` function (Bug-7980); this module generalises the pattern
so every live-state snapshot caller (single-model export, project export,
governance export, YAML export) gets the same guarantee.

The snapshot session is opened from the NullPool snapshot factory
(``get_tenant_snapshot_session_factory``), so the second connection never
competes with the bounded request pool even when the request's main connection
and advisory lock are held (avoids pool starvation/deadlock).

Callers that snapshot from an OFFLINE / own-transaction context (e.g.
``rehydrator.py::append_authentic_import_version`` — snapshots the importer's
own uncommitted transaction, self-consistent already) do NOT need this helper.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.session import get_tenant_snapshot_session_factory
from shared.model_snapshot.serialiser import snapshot_model


@asynccontextmanager
async def consistent_read_session(tenant_id: str) -> AsyncIterator[AsyncSession]:
    """Yield a dedicated tenant session whose reads share one DB snapshot."""
    factory = await get_tenant_snapshot_session_factory(tenant_id)
    async with factory() as snap_db:
        snap_db.info["tenant_id"] = tenant_id
        await snap_db.connection(
            execution_options={"isolation_level": "REPEATABLE READ"}
        )
        yield snap_db


async def consistent_snapshot(
    tenant_id: str,
    model_id: UUID,
    *,
    include_versions: bool = False,
) -> dict[str, Any]:
    """Snapshot a live model under REPEATABLE READ isolation.

    Returns the same ``dict`` as ``snapshot_model``, but every SELECT inside
    the serialiser observes ONE consistent committed point-in-time: a concurrent
    writer's commit is observed atomically (all or none), never partially.

    Parameters
    ----------
    tenant_id:
        Tenant slug — used to obtain the NullPool snapshot session factory.
    model_id:
        The model to snapshot.
    include_versions:
        Forward to ``snapshot_model``; when True the snapshot carries each
        model version's own portable ``snapshot_json`` (project export v2).
    """
    async with consistent_read_session(tenant_id) as snap_db:
        return await snapshot_model(
            model_id, snap_db, include_versions=include_versions
        )
