"""Create audit_events table in per-tenant meta schema.

Revision ID: 0060
Revises: 0059
Create Date: 2026-05-01
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def _table_exists(inspector, name: str) -> bool:
    return name in set(inspector.get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector, "audit_events"):
        op.create_table(
            "audit_events",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("timestamp", sa.TIMESTAMP(timezone=True), nullable=False, index=True),
            sa.Column("actor_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("actor_email", sa.Text(), nullable=True),
            sa.Column("action", sa.Text(), nullable=False),
            sa.Column("target_type", sa.Text(), nullable=True),
            sa.Column("target_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("target_name", sa.Text(), nullable=True),
            sa.Column("severity", sa.Text(), nullable=False),
            sa.Column("detail", sa.dialects.postgresql.JSONB(), nullable=True),
            sa.Column("ip_address", sa.Text(), nullable=True),
        )
        op.create_index(
            "ix_audit_events_action",
            "audit_events",
            ["action"],
        )
        op.create_index(
            "ix_audit_events_actor_id",
            "audit_events",
            ["actor_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "audit_events"):
        op.drop_table("audit_events")
