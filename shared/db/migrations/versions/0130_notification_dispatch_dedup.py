"""Durable notification dedup table (replica-shared).

F-022-04: notification dedup used a per-process in-memory map, so an event
fanned across N replicas could send up to N duplicate emails/Slack
messages, and a restart cleared the window. This adds a small per-tenant
table keyed by the dedup key (``event_type:channel_type:target``) holding
the last-dispatch timestamp. The dispatcher does an atomic
INSERT ... ON CONFLICT DO UPDATE that only refreshes the timestamp when the
previous send is older than the window, so exactly one replica wins the
window and sends.

Revision ID: 0130
Revises: 0129
Create Date: 2026-06-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0130"
down_revision = "0129"
branch_labels = None
depends_on = None

_TABLE = "notification_dispatch_dedup"


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    if _table_exists(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("dedup_key", sa.String(length=512), primary_key=True),
        sa.Column(
            "last_dispatched_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    if _table_exists(_TABLE):
        op.drop_table(_TABLE)
