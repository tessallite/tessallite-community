"""Add enable_agent_log_screen toggle to ProjectAgentConfig.

Revision ID: 0054
Revises: 0053
Create Date: 2026-04-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def _table_exists(inspector, name: str) -> bool:
    return name in set(inspector.get_table_names())


def _column_exists(inspector, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector, "project_agent_configs"):
        return

    if not _column_exists(inspector, "project_agent_configs", "enable_agent_log_screen"):
        op.add_column(
            "project_agent_configs",
            sa.Column(
                "enable_agent_log_screen",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _table_exists(inspector, "project_agent_configs") and _column_exists(
        inspector, "project_agent_configs", "enable_agent_log_screen"
    ):
        op.drop_column("project_agent_configs", "enable_agent_log_screen")
