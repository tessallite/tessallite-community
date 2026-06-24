"""Add data_quality_rules and data_quality_violations tables.

Revision ID: 0067
Revises: 0066
Create Date: 2026-05-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "data_quality_rules",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("target_type", sa.Text, nullable=False),
        sa.Column("target_id", UUID(as_uuid=True), nullable=False),
        sa.Column("rule_type", sa.Text, nullable=False),
        sa.Column("rule_config", JSONB, nullable=True),
        sa.Column("severity", sa.Text, nullable=False, server_default="warn"),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("last_checked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_violation_count", sa.Integer, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("model_id", "name", name="uq_data_quality_rule_name"),
    )

    op.create_table(
        "data_quality_violations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("rule_id", UUID(as_uuid=True), sa.ForeignKey("data_quality_rules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("detected_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("violation_count", sa.Integer, nullable=False),
        sa.Column("sample_values", JSONB, nullable=True),
        sa.Column("aggregate_id", UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("data_quality_violations")
    op.drop_table("data_quality_rules")
