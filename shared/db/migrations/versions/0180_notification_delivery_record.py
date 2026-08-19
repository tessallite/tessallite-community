"""Durable email/Slack notification delivery record (Bug-8053 / F-022-04).

Before this table the alerting dispatcher persisted routes and dedup claims but
no record of whether an email/Slack notification actually reached its
destination. A failed send left only an application-log line — invisible in the
product, so an operator could not see, retry, or prove a failed notification,
and a broken SMTP/Slack configuration could fail indefinitely without appearing
anywhere a user looks.

This adds a small per-tenant table: one row per delivery attempt with a terminal
outcome (``sent``/``failed``), a non-secret destination hint, the event/channel
identity, and a bounded error message. Surfaced through the tenant-scoped
notification-deliveries API.

Tenant-schema guarded (skip when the schema has no ``notification_routes``
table), idempotent, reversible.

Revision ID: 0180
Revises: 0179
Create Date: 2026-07-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0180"
down_revision = "0179"
branch_labels = None
depends_on = None

_TABLE = "notification_deliveries"


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    # Only tenant schemas carry the notification stack. Guard on an existing
    # tenant table so the migration is a no-op on the system schema.
    if not _table_exists("notification_routes"):
        return
    if _table_exists(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "route_id",
            UUID(as_uuid=True),
            sa.ForeignKey("notification_routes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("project_id", UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("channel_type", sa.String(length=16), nullable=False),
        sa.Column("target", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_notification_deliveries_route_id", _TABLE, ["route_id"]
    )
    op.create_index(
        "ix_notification_deliveries_project_id", _TABLE, ["project_id"]
    )
    op.create_index(
        "ix_notification_deliveries_created_at", _TABLE, ["created_at"]
    )


def downgrade() -> None:
    if _table_exists(_TABLE):
        op.drop_table(_TABLE)
