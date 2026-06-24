"""Add prompt_messages and llm_raw_response to AgentTurn.

Revision ID: 0055
Revises: 0054
Create Date: 2026-04-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def _table_exists(inspector, name: str) -> bool:
    return name in set(inspector.get_table_names())


def _column_exists(inspector, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector, "agent_turns"):
        return

    if not _column_exists(inspector, "agent_turns", "prompt_messages"):
        op.add_column(
            "agent_turns",
            sa.Column("prompt_messages", sa.JSON(), nullable=True),
        )

    if not _column_exists(inspector, "agent_turns", "llm_raw_response"):
        op.add_column(
            "agent_turns",
            sa.Column("llm_raw_response", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _table_exists(inspector, "agent_turns"):
        if _column_exists(inspector, "agent_turns", "llm_raw_response"):
            op.drop_column("agent_turns", "llm_raw_response")
        if _column_exists(inspector, "agent_turns", "prompt_messages"):
            op.drop_column("agent_turns", "prompt_messages")
