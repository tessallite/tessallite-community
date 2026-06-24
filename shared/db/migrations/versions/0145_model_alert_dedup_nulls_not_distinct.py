"""Model-alert open dedup treats NULL related-object keys as equal.

Revision ID: 0145
Revises: 0144
Create Date: 2026-06-14
"""
from alembic import op


revision = "0145"
down_revision = "0144"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_model_alerts_dedup")
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_model_alerts_dedup
        ON model_alerts (
            model_id,
            category,
            related_object_type,
            related_object_id
        ) NULLS NOT DISTINCT
        WHERE resolved_at IS NULL AND dismissed_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_model_alerts_dedup")
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_model_alerts_dedup
        ON model_alerts (
            model_id,
            category,
            related_object_type,
            related_object_id
        )
        WHERE resolved_at IS NULL AND dismissed_at IS NULL
        """
    )
