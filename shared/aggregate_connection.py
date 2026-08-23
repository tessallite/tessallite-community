"""Cross-database aggregate connection resolution.

Resolves the single source ProjectConnection for a model's tables and
detects whether source and target databases are the same connection.
When tables span multiple source connections, the model is rejected
for aggregate materialization (multi-source aggregates are unsupported).

This module owns the ``model -> source ProjectConnection`` relation in BOTH
directions. ``shared/artifact_target_binding.py`` needs the forward direction
under row locks (to re-prove a build's source identity) and the reverse
direction (to find every model a connection edit re-points), so both live here
next to :func:`resolve_source_connection` rather than being re-derived there —
a second implementation of "which connection is this model's source" is exactly
the drift that let the source side go uncovered (Bug-8602).
"""
from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.connection_scope import assert_connection_in_project
from shared.db.models import DataSource, Model, ModelTable, ProjectConnection

logger = logging.getLogger(__name__)


async def source_connection_ids_for_model(
    model_id: UUID,
    db: AsyncSession,
    *,
    lock_for_update: bool = False,
) -> list[UUID]:
    """Distinct ``DataSource.project_connection_id`` values a model reads from.

    ``lock_for_update`` takes a row lock on the DataSource rows only (``FOR
    UPDATE OF data_sources``), so a build finalising its source binding blocks a
    concurrent re-point of the pointer it is about to re-prove — the source-side
    counterpart of the target binding's ``with_for_update`` on ``DataTarget``.

    The lock is applied WITHOUT ``DISTINCT`` because PostgreSQL rejects
    ``SELECT DISTINCT ... FOR UPDATE``; de-duplication happens in Python, which
    is equivalent here (the row set is one row per model table).
    """
    stmt = (
        select(DataSource.project_connection_id)
        .join(ModelTable, ModelTable.source_id == DataSource.id)
        .where(ModelTable.model_id == model_id)
    )
    if lock_for_update:
        stmt = stmt.with_for_update(of=DataSource)
    else:
        stmt = stmt.distinct()
    rows = (await db.execute(stmt)).scalars().all()

    ordered: list[UUID] = []
    seen: set = set()
    for conn_id in rows:
        if conn_id is None or conn_id in seen:
            continue
        seen.add(conn_id)
        ordered.append(conn_id)
    return ordered


async def model_ids_for_source_connection(
    connection_id: UUID,
    db: AsyncSession,
) -> list[UUID]:
    """Every model whose tables read FROM ``connection_id`` (Bug-8602).

    The reverse of :func:`source_connection_ids_for_model`, and the enumeration
    the control-plane invalidator needs: an edit to this connection's endpoint
    changes which database every one of these models' artifacts was built from.

    Deliberately keyed on ``ModelTable.source_id`` rather than
    ``DataSource.model_id``: the FROM clause is assembled from the model's
    TABLES, so a DataSource row that belongs to a model but is referenced by no
    table cannot have contributed to any build. Conversely a table pointing at
    another model's DataSource (legacy/imported rows) IS a real read of this
    connection and must be caught.
    """
    rows = (
        await db.execute(
            select(ModelTable.model_id)
            .join(DataSource, ModelTable.source_id == DataSource.id)
            .where(DataSource.project_connection_id == connection_id)
            .distinct()
        )
    ).scalars().all()
    return [model_id for model_id in rows if model_id is not None]


async def model_ids_reading_source(
    source_id: UUID,
    db: AsyncSession,
) -> list[UUID]:
    """Every model whose tables read through DataSource ``source_id``.

    Normally exactly the DataSource's owning model, but ``ModelTable.source_id``
    carries no composite FK back to ``(model_id, source_id)``, so a legacy or
    imported table CAN reference another model's DataSource — a state
    :func:`model_ids_for_source_connection` already declares it must catch. A
    re-point handler that invalidated only the owning model would leave the
    borrowing model's artifacts serving from the previous database, so the two
    enumerations are kept aligned (round-2 review of Bug-8602).
    """
    rows = (
        await db.execute(
            select(ModelTable.model_id)
            .where(ModelTable.source_id == source_id)
            .distinct()
        )
    ).scalars().all()
    return [m for m in rows if m is not None]


async def resolve_source_connection(
    model_id: UUID,
    db: AsyncSession,
) -> ProjectConnection:
    """Resolve the single source ProjectConnection for a model.

    All ModelTables in a model reference a DataSource via source_id.
    This function loads all distinct DataSource.project_connection_id
    values across the model's tables and returns the single
    ProjectConnection.

    Raises
    ------
    ValueError
        If the model has no source connection, or if its tables span
        multiple source connections (cross-source aggregation is not
        supported).
    """
    conn_ids = await source_connection_ids_for_model(model_id, db)

    if not conn_ids:
        raise ValueError(
            f"No source connection found for model {model_id}. "
            f"Ensure the model has tables linked to a data source."
        )

    if len(conn_ids) > 1:
        raise ValueError(
            f"Model {model_id} has tables spanning {len(conn_ids)} "
            f"source connections. Cross-source aggregation is not "
            f"supported — all tables in the aggregate grain must come "
            f"from the same source."
        )

    conn = await db.get(ProjectConnection, conn_ids[0])
    if conn is None:
        raise ValueError(
            f"ProjectConnection {conn_ids[0]} not found for model {model_id}"
        )

    # Bug-5325 fail-closed: a legacy/imported DataSource can reference a
    # ProjectConnection in a DIFFERENT project than the model that owns it.
    # Reject it rather than materialise/execute against another project's
    # source. The owning model defines the project the connection must match.
    model = await db.get(Model, model_id)
    if model is None:
        raise ValueError(f"Model {model_id} not found for connection resolution")
    assert_connection_in_project(conn, model.project_id)
    return conn


def is_same_database(
    source_conn: ProjectConnection,
    target_conn: ProjectConnection,
) -> bool:
    """True when source and target share the same ProjectConnection."""
    return source_conn.id == target_conn.id
