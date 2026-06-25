"""Cross-database aggregate connection resolution.

Resolves the single source ProjectConnection for a model's tables and
detects whether source and target databases are the same connection.
When tables span multiple source connections, the model is rejected
for aggregate materialization (multi-source aggregates are unsupported).
"""
from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.connection_scope import assert_connection_in_project
from shared.db.models import DataSource, Model, ModelTable, ProjectConnection

logger = logging.getLogger(__name__)


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
    result = await db.execute(
        select(DataSource.project_connection_id)
        .join(ModelTable, ModelTable.source_id == DataSource.id)
        .where(ModelTable.model_id == model_id)
        .distinct()
    )
    conn_ids = [row[0] for row in result.all()]

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
