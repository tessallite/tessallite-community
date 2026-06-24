"""Persist aggregate storage-byte telemetry.

Revision ID: 0146
Revises: 0145
Create Date: 2026-06-14
"""
from alembic import op
import sqlalchemy as sa


revision = "0146"
down_revision = "0145"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "aggregate_definitions",
        sa.Column("storage_bytes", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("aggregate_definitions", "storage_bytes")
