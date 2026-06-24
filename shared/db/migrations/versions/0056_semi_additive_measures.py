"""Add semi-additive behavior fields to measures.

Revision ID: 0056
Revises: 0055
Create Date: 2026-05-01
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def _table_exists(inspector, name: str) -> bool:
    return name in set(inspector.get_table_names())


def _column_exists(inspector, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "measures"):
        if not _column_exists(inspector, "measures", "semi_additive_behavior"):
            op.add_column(
                "measures",
                sa.Column("semi_additive_behavior", sa.String(32), nullable=True),
            )
        if not _column_exists(inspector, "measures", "semi_additive_account_column_id"):
            op.add_column(
                "measures",
                sa.Column(
                    "semi_additive_account_column_id",
                    sa.dialects.postgresql.UUID(as_uuid=True),
                    sa.ForeignKey("model_columns.id", ondelete="SET NULL"),
                    nullable=True,
                ),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "measures"):
        if _column_exists(inspector, "measures", "semi_additive_account_column_id"):
            op.drop_column("measures", "semi_additive_account_column_id")
        if _column_exists(inspector, "measures", "semi_additive_behavior"):
            op.drop_column("measures", "semi_additive_behavior")
