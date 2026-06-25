"""Add agent_cost_ledger table and budget columns to project_agent_configs.

Revision ID: 0068
Revises: 0067
Create Date: 2026-05-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_cost_ledger",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "turn_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_turns.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "llm_config_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("llm_provider_configs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("estimated_cost_usd", sa.Numeric(10, 6), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_agent_cost_ledger_project_id",
        "agent_cost_ledger",
        ["project_id", "created_at"],
    )

    op.add_column(
        "project_agent_configs",
        sa.Column("daily_token_budget", sa.Integer, nullable=False, server_default="0"),
    )
    op.add_column(
        "project_agent_configs",
        sa.Column(
            "daily_cost_budget_usd",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "project_agent_configs",
        sa.Column(
            "max_query_complexity", sa.Integer, nullable=False, server_default="0"
        ),
    )


def downgrade() -> None:
    op.drop_column("project_agent_configs", "max_query_complexity")
    op.drop_column("project_agent_configs", "daily_cost_budget_usd")
    op.drop_column("project_agent_configs", "daily_token_budget")
    op.drop_index("ix_agent_cost_ledger_project_id", table_name="agent_cost_ledger")
    op.drop_table("agent_cost_ledger")
