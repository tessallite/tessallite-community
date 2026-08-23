"""Add built_for_version_id/built_for_epoch to aggregates and pockets.

F-013-02 / F-013-03 / F-005-01 (Bug-8250): artifact-to-version binding. A
materialised aggregate or pocket carries the exact deployed model version +
epoch it was BUILT FOR, written atomically at successful refresh. The runtime
matcher requires an exact match against the model's current
(deployed_version_id, deploy_epoch); a fresh artifact built under a previous
definition must not serve after a deploy/revert. NULL = never built for any
deployed version (unmaterialised, built while undeployed, or import-cleared).

These are LIVE build metadata: snapshot-EXCLUDED and cleared on
import/clone/rehydrate, mirroring active_refresh_run_id.

Tenant-schema guarded (skip when the schema has no target table), idempotent,
reversible.

Revision ID: 0175
Revises: 0174
Create Date: 2026-07-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0175"
down_revision = "0174"
branch_labels = None
depends_on = None


def _table_exists(conn, table_name: str) -> bool:
    result = conn.execute(
        sa.text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM information_schema.tables"
            "  WHERE table_schema = current_schema()"
            "    AND table_name = :t"
            ")"
        ),
        {"t": table_name},
    )
    return result.scalar()


def _column_exists(conn, table_name: str, column_name: str) -> bool:
    result = conn.execute(
        sa.text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM information_schema.columns"
            "  WHERE table_schema = current_schema()"
            "    AND table_name = :t"
            "    AND column_name = :c"
            ")"
        ),
        {"t": table_name, "c": column_name},
    )
    return result.scalar()


_TARGETS = ("aggregate_definitions", "pocket_definitions")


def upgrade() -> None:
    conn = op.get_bind()
    for table in _TARGETS:
        if not _table_exists(conn, table):
            continue
        if not _column_exists(conn, table, "built_for_version_id"):
            op.add_column(
                table,
                sa.Column("built_for_version_id", postgresql.UUID(as_uuid=True), nullable=True),
            )
        if not _column_exists(conn, table, "built_for_epoch"):
            op.add_column(
                table,
                sa.Column("built_for_epoch", sa.Integer(), nullable=True),
            )


def downgrade() -> None:
    conn = op.get_bind()
    for table in _TARGETS:
        if not _table_exists(conn, table):
            continue
        if _column_exists(conn, table, "built_for_epoch"):
            op.drop_column(table, "built_for_epoch")
        if _column_exists(conn, table, "built_for_version_id"):
            op.drop_column(table, "built_for_version_id")
