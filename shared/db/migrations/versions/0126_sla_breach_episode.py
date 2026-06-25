"""Add breach-episode tracking columns to refresh_sla_configs.

B17 round-2 Finding 1: after a breach, every later hourly sweep re-alerted
and re-burned the retry budget even when the data had already healed. The
SLA monitor now tracks one breach EPISODE per model per UTC day:

- ``last_breach_alerted_on`` (DATE) — the UTC day the current episode's
  single breach alert was emitted. The monitor alerts only when this
  differs from today, giving exactly one alert per episode; a new day is
  a new episode and alerts again.
- ``last_breach_resolved_at`` (TIMESTAMPTZ) — set when every aggregate on
  the model has a successful refresh for the breached day (recovery).
  Cleared when a new episode's alert is emitted.

Both columns are nullable and written only by the scheduler's SLA monitor.

Revision ID: 0126
Revises: 0125
Create Date: 2026-06-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0126"
down_revision = "0125"
branch_labels = None
depends_on = None

_TABLE = "refresh_sla_configs"


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    if not _table_exists(_TABLE):
        return
    if not _column_exists(_TABLE, "last_breach_alerted_on"):
        op.add_column(_TABLE, sa.Column("last_breach_alerted_on", sa.Date(), nullable=True))
    if not _column_exists(_TABLE, "last_breach_resolved_at"):
        op.add_column(
            _TABLE,
            sa.Column("last_breach_resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        )


def downgrade() -> None:
    if not _table_exists(_TABLE):
        return
    if _column_exists(_TABLE, "last_breach_resolved_at"):
        op.drop_column(_TABLE, "last_breach_resolved_at")
    if _column_exists(_TABLE, "last_breach_alerted_on"):
        op.drop_column(_TABLE, "last_breach_alerted_on")
