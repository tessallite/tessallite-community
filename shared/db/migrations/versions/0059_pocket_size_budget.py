"""Add pocket_size_budget_bytes to projects and models tables.

Revision ID: 0059
Revises: 0058
Create Date: 2026-05-01
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def _table_exists(inspector, name: str) -> bool:
    return name in set(inspector.get_table_names())


def _column_exists(inspector, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "projects"):
        if not _column_exists(inspector, "projects", "pocket_size_budget_bytes"):
            op.add_column(
                "projects",
                sa.Column("pocket_size_budget_bytes", sa.BigInteger(), nullable=True),
            )

    if _table_exists(inspector, "models"):
        if not _column_exists(inspector, "models", "pocket_size_budget_bytes"):
            op.add_column(
                "models",
                sa.Column("pocket_size_budget_bytes", sa.BigInteger(), nullable=True),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "models"):
        if _column_exists(inspector, "models", "pocket_size_budget_bytes"):
            op.drop_column("models", "pocket_size_budget_bytes")

    if _table_exists(inspector, "projects"):
        if _column_exists(inspector, "projects", "pocket_size_budget_bytes"):
            op.drop_column("projects", "pocket_size_budget_bytes")
