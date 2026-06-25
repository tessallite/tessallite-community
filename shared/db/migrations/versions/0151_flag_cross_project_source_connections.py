"""Detect cross-project DataSource/DataTarget connections (Bug-5325).

A legacy or imported ``data_sources.project_connection_id`` (or
``data_targets.project_connection_id``) row can point at a
``project_connections`` row that belongs to a DIFFERENT project than the
source/target's owning model. The model-service create/update guards block
NEW cross-project writes; the shared fail-closed resolver
(``shared.connection_scope``) now refuses to USE a malformed row at the
foreground execution sites: the model-service SOURCE read endpoints, the
query-router introspect route, the query-router gateway-query SOURCE execution,
the query-router aggregate/pocket TARGET execution, and the shared aggregate
SOURCE resolver. As of Bug-5500 the remaining BACKGROUND/job paths are also
routed through the same shared resolver and fail closed: the optimizer stats
collector and scheduler schema-drift (SOURCE), and the pocket refresh/drop,
optimizer aggregate creator, the shared aggregate physical-table drop
(retire/cap/purge), and scheduler full/incremental refresh (TARGET).
This migration is the detection/validation companion: it WARNs for every
malformed source/target row so operators can re-point them, without silently
deleting or mutating data (the runtime guard rejects use of such a row, but the
row itself still needs an operator to re-point it at the correct connection).

Non-destructive by design. We log rather than NULL the column because
``project_connection_id`` is NOT NULL and the correct connection is
operator-knowledge, not derivable here.

Idempotent: a pure SELECT-and-log pass; safe to re-run. search_path is set to
the target tenant schema by env.py, so it runs per tenant schema.

Revision ID: 0151
Revises: 0150
Create Date: 2026-06-24
"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

revision = "0151"
down_revision = "0150"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.migration.0151_cross_project_connections")


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    try:
        insp.get_columns(name)
        return True
    except sa.exc.NoSuchTableError:
        return False


_DETECT_SQL = """
SELECT ds.id AS row_id,
       ds.project_connection_id AS connection_id,
       m.project_id AS owning_project_id,
       pc.project_id AS connection_project_id
FROM {table} ds
JOIN models m ON m.id = ds.model_id
JOIN project_connections pc ON pc.id = ds.project_connection_id
WHERE pc.project_id <> m.project_id
"""


def _scan(table: str) -> int:
    if not (
        _table_exists(table)
        and _table_exists("models")
        and _table_exists("project_connections")
    ):
        return 0
    bind = op.get_bind()
    rows = bind.execute(sa.text(_DETECT_SQL.format(table=table))).fetchall()
    for r in rows:
        logger.warning(
            "Bug-5325 cross-project %s row %s: connection %s belongs to project "
            "%s but the row's model belongs to project %s. Re-point it at a "
            "connection in the correct project.",
            table, r.row_id, r.connection_id,
            r.connection_project_id, r.owning_project_id,
        )
    return len(rows)


def upgrade() -> None:
    total = _scan("data_sources") + _scan("data_targets")
    if total:
        logger.warning(
            "Bug-5325/Bug-5500: %d cross-project source/target connection "
            "row(s) flagged in this schema. Both foreground (model-service "
            "endpoints, query-router introspect + gateway query execution, "
            "shared aggregate resolver, aggregate/pocket TARGET execution) and "
            "background job paths (optimizer stats + scheduler schema-drift "
            "SOURCE; pocket refresh/drop, aggregate creator, aggregate "
            "physical-table drop, scheduler full/incremental refresh TARGET) "
            "now reject these fail-closed via "
            "shared.connection_scope. Re-point each flagged row at a connection "
            "in the correct project so the source/target becomes usable again.",
            total,
        )


def downgrade() -> None:
    # Detection-only migration — nothing to reverse.
    pass
