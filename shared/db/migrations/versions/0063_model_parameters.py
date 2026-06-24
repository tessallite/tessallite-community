"""Create model_parameters table.

Revision ID: 0063
Revises: 0062
Create Date: 2026-05-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def _table_exists(inspector, name: str) -> bool:
    return name in set(inspector.get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector, "model_parameters"):
        op.create_table(
            "model_parameters",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("model_id", sa.dialects.postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("models.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("display_name", sa.String(255), nullable=True),
            sa.Column("param_type", sa.String(32), nullable=False),
            sa.Column("default_value", sa.dialects.postgresql.JSONB(), nullable=True),
            sa.Column("allowed_values", sa.dialects.postgresql.JSONB(), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("model_id", "name", name="uq_model_parameters_model_name"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "model_parameters"):
        op.drop_table("model_parameters")
