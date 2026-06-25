"""Drill-through sets — add source_join_path for multi-hop overrides.

Revision ID: 0037
Revises: 0036
Create Date: 2026-04-24

Phase 8.A.4. When the modeller overrides ``source_table_id`` to a table
that requires more than one join hop back to the fact, ``source_join_path``
records the explicit ordered list of join ids the modeller picked. Null
means "single-path auto-resolvable" (the builder rejects the save when
that is not true).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


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
    if _column_exists("drill_through_sets", "source_join_path"):
        return
    op.add_column(
        "drill_through_sets",
        sa.Column("source_join_path", JSONB, nullable=True),
    )


def downgrade() -> None:
    if not _column_exists("drill_through_sets", "source_join_path"):
        return
    op.drop_column("drill_through_sets", "source_join_path")
