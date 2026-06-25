"""Phase 9 / F5 — track which model version has had its predictive build run.

The deploy path (model-service) does not call the optimizer directly.
Instead, the optimizer polls for models where
``deployed_version_id != predictive_built_for_version_id`` and runs the
predictive build asynchronously. This satisfies Q3=B (deploy returns
immediately; predicted aggregates land in the background).

Revision ID: 0046
Revises: 0045
Create Date: 2026-04-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "models" not in inspector.get_table_names():
        return
    existing_cols = {c["name"] for c in inspector.get_columns("models")}
    if "predictive_built_for_version_id" not in existing_cols:
        op.add_column(
            "models",
            sa.Column(
                "predictive_built_for_version_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "models" not in inspector.get_table_names():
        return
    existing_cols = {c["name"] for c in inspector.get_columns("models")}
    if "predictive_built_for_version_id" in existing_cols:
        op.drop_column("models", "predictive_built_for_version_id")
