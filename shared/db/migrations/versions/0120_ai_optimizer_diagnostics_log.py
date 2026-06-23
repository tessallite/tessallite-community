"""Add diagnostics_log JSONB column to ai_optimizer_runs table.

Stores structured decision log: parse warnings, dimension validation
failures, dedup skips, and materialisation outcomes.

Revision ID: 0120
Revises: 0119
Create Date: 2026-06-07
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0120"
down_revision = "0119"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_optimizer_runs", sa.Column("diagnostics_log", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("ai_optimizer_runs", "diagnostics_log")
