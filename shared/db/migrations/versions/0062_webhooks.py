"""Create webhook_endpoints and webhook_deliveries tables.

Revision ID: 0062
Revises: 0061
Create Date: 2026-05-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def _table_exists(inspector, name: str) -> bool:
    return name in set(inspector.get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector, "webhook_endpoints"):
        op.create_table(
            "webhook_endpoints",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("signing_secret", sa.LargeBinary(), nullable=True),
            sa.Column("event_filters", sa.dialects.postgresql.JSONB(), nullable=False,
                      server_default=sa.text("'[\"*\"]'::jsonb")),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        )

    if not _table_exists(inspector, "webhook_deliveries"):
        op.create_table(
            "webhook_deliveries",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("endpoint_id", sa.dialects.postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("webhook_endpoints.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("event_type", sa.String(64), nullable=False),
            sa.Column("payload", sa.dialects.postgresql.JSONB(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("last_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
            sa.Column("response_code", sa.Integer(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_webhook_deliveries_status", "webhook_deliveries", ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "webhook_deliveries"):
        op.drop_table("webhook_deliveries")
    if _table_exists(inspector, "webhook_endpoints"):
        op.drop_table("webhook_endpoints")
