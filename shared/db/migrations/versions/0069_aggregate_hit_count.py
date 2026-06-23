"""Add hit_count column to aggregate_definitions.

Revision ID: 0069
Revises: 0068
Create Date: 2026-05-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "aggregate_definitions",
        sa.Column(
            "hit_count",
            sa.BigInteger,
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("aggregate_definitions", "hit_count")
