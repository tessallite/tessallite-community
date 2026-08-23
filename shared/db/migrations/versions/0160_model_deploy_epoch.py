"""Add deploy_epoch counter to models for multi-replica cache invalidation.

Bug-7140: on Cloud Run with N replicas, undeploy/revert can leave stale
cache entries because the cache key (model_id, deployed_version_id) does
not change when the pointer goes NULL (undeploy) or when a revert changes
content without changing the key. The deploy_epoch is a monotonically
increasing counter bumped on every deploy/undeploy/revert; the query-router
cache can include it in the key so content changes are always visible.

Revision ID: 0160
Revises: 0159
Create Date: 2026-07-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0160"
down_revision = "0159"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "models" not in table_names:
        return
    columns = {col["name"] for col in inspector.get_columns("models")}
    if "deploy_epoch" in columns:
        return
    op.add_column(
        "models",
        sa.Column(
            "deploy_epoch",
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
    if "deploy_epoch" not in columns:
        return
    op.drop_column("models", "deploy_epoch")
