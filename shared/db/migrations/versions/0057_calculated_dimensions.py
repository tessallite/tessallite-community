"""Add calculated dimension fields to dimensions table.

Revision ID: 0057
Revises: 0056
Create Date: 2026-05-01
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def _table_exists(inspector, name: str) -> bool:
    return name in set(inspector.get_table_names())


def _column_exists(inspector, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "dimensions"):
        if not _column_exists(inspector, "dimensions", "calc_expression"):
            op.add_column(
                "dimensions",
                sa.Column("calc_expression", sa.Text, nullable=True),
            )
        if not _column_exists(inspector, "dimensions", "calc_expression_tables"):
            op.add_column(
                "dimensions",
                sa.Column(
                    "calc_expression_tables",
                    sa.dialects.postgresql.JSONB,
                    nullable=True,
                ),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "dimensions"):
        if _column_exists(inspector, "dimensions", "calc_expression_tables"):
            op.drop_column("dimensions", "calc_expression_tables")
        if _column_exists(inspector, "dimensions", "calc_expression"):
            op.drop_column("dimensions", "calc_expression")
