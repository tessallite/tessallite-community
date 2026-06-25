"""Add refresh_sla_configs table.

Revision ID: 0070
Revises: 0069
Create Date: 2026-05-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "refresh_sla_configs",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "model_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("target_completion_time", sa.String(5), nullable=False),
        sa.Column("grace_period_minutes", sa.Integer, nullable=False, server_default="15"),
        sa.Column("max_retries", sa.Integer, nullable=False, server_default="1"),
        sa.Column("alert_on_breach", sa.Boolean, nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("refresh_sla_configs")
