"""Bug-8395 — add models.predictive_built_for_epoch.

The predictive built-stamp is now epoch-aware: a revert-to-previously-built-
version bumps ``deploy_epoch`` and stales the built artifacts, so a
version-id-only comparison would silently report the model "already built".
The optimizer compares BOTH ``predictive_built_for_version_id`` and
``predictive_built_for_epoch`` against the live pointer; older rows carry
NULL epoch and are treated as not-built (a one-time re-build, which is
cheap and idempotent).

Revision ID: 0204
Revises: 0203
Create Date: 2026-08-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0204"
down_revision = "0203"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "models" not in inspector.get_table_names():
        return
    existing_cols = {c["name"] for c in inspector.get_columns("models")}
    if "predictive_built_for_epoch" not in existing_cols:
        op.add_column(
            "models",
            sa.Column("predictive_built_for_epoch", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "models" not in inspector.get_table_names():
        return
    existing_cols = {c["name"] for c in inspector.get_columns("models")}
    if "predictive_built_for_epoch" in existing_cols:
        op.drop_column("models", "predictive_built_for_epoch")
