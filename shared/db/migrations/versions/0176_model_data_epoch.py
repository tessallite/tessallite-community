"""Add data_epoch counter to models for KPI data-freshness cache invalidation.

F-017-03 (Bug-7989): a successful data/aggregate/pocket refresh leaves the
in-process KPI evaluation cache stale up to the 300s TTL, and different
model-service replicas disagree during the window. data_epoch is a
monotonically increasing counter bumped on every successful data refresh; the
KPI cache folds it into its key so the next evaluation after a refresh misses
the stale entry on every replica without a cross-process event bus. Mirrors the
deploy_epoch pattern (0160) but tracks DATA freshness, not DEFINITION deploys.

Revision ID: 0176
Revises: 0175
Create Date: 2026-07-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0176"
down_revision = "0175"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "models" not in table_names:
        return
    columns = {col["name"] for col in inspector.get_columns("models")}
    if "data_epoch" in columns:
        return
    op.add_column(
        "models",
        sa.Column(
            "data_epoch",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "models" not in table_names:
        return
    columns = {col["name"] for col in inspector.get_columns("models")}
    if "data_epoch" not in columns:
        return
    op.drop_column("models", "data_epoch")
