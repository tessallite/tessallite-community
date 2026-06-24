"""Add auth_source column to local_users for JIT adoption tracking.

Revision ID: 0058
Revises: 0057
Create Date: 2026-05-01
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def _table_exists(inspector, name: str) -> bool:
    return name in set(inspector.get_table_names())


def _column_exists(inspector, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "local_users"):
        if not _column_exists(inspector, "local_users", "auth_source"):
            op.add_column(
                "local_users",
                sa.Column(
                    "auth_source",
                    sa.String(32),
                    nullable=False,
                    server_default="local",
                ),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "local_users"):
        if _column_exists(inspector, "local_users", "auth_source"):
            op.drop_column("local_users", "auth_source")
