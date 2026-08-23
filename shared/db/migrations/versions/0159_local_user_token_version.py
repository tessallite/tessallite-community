"""Add token_version to local_users for regular-session invalidation.

Revision ID: 0159
Revises: 0158
Create Date: 2026-07-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0159"
down_revision = "0158"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "local_users" not in table_names:
        return
    columns = {col["name"] for col in inspector.get_columns("local_users")}
    if "token_version" in columns:
        return
    op.add_column(
        "local_users",
        sa.Column(
            "token_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "local_users" not in table_names:
        return
    columns = {col["name"] for col in inspector.get_columns("local_users")}
    if "token_version" not in columns:
        return
    op.drop_column("local_users", "token_version")
