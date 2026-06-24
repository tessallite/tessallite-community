"""Per-perspective workload partition columns — Phase 8.C.2 scaffolding.

Revision ID: 0040
Revises: 0039
Create Date: 2026-04-24

Adds a nullable ``perspective_id`` FK on the four tables that drive the
router's match + optimizer's workload scan:

  * ``aggregate_definitions`` — which perspective an aggregate is
    scoped to. NULL = unscoped (global, usable under any perspective).
  * ``pocket_definitions`` — same semantics for pockets.
  * ``query_logs`` — which perspective (if any) served the query.
  * ``query_miss_logs`` — which perspective the missed query was bound
    to, so the optimizer can partition its workload scan later.

FK uses ``ON DELETE SET NULL`` so a perspective delete does not orphan
historical logs or unintentionally drop aggregates. The matcher
precedence rule (perspective-scoped beats global when a perspective is
bound) lives in the router; this migration only ships the columns.

No composite indexes yet — the optimizer scoping (8.C.2 main) will
add them once the scan pattern is settled.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


_COLUMN = "perspective_id"
_TABLES = (
    "aggregate_definitions",
    "pocket_definitions",
    "query_logs",
    "query_miss_logs",
)


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = :table AND column_name = :column"
        ),
        {"table": table, "column": column},
    )
    return result.scalar() is not None


def upgrade() -> None:
    for table in _TABLES:
        if _column_exists(table, _COLUMN):
            continue
        op.add_column(
            table,
            sa.Column(
                _COLUMN,
                sa.dialects.postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
        )
        op.create_foreign_key(
            f"fk_{table}_perspective_id",
            table,
            "perspectives",
            [_COLUMN],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index(
            f"ix_{table}_perspective_id",
            table,
            [_COLUMN],
        )


def downgrade() -> None:
    for table in _TABLES:
        if not _column_exists(table, _COLUMN):
            continue
        op.drop_index(f"ix_{table}_perspective_id", table_name=table)
        op.drop_constraint(f"fk_{table}_perspective_id", table, type_="foreignkey")
        op.drop_column(table, _COLUMN)
