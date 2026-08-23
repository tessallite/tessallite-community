"""Add last_breach_episode_opened_on to refresh_sla_configs (Bug-8146).

Separates the episode-opening marker from delivery-confirmation tracking.
``last_breach_episode_opened_on`` is set on the first breach observation of
the UTC day regardless of delivery outcome, so an all-failure dispatch does
not suppress retries on the next monitor tick.

The column is additive, nullable, and backward-compatible — the SLA monitor
already falls back to ``last_breach_alerted_on`` for rows written before this
column existed (see _check_one in sla_monitor.py).

Revision ID: 0203
Revises: 0202
Create Date: 2026-08-09
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0203"
down_revision = "0202"
branch_labels = None
depends_on = None


def _table_exists(conn, table_name: str) -> bool:
    result = conn.execute(
        sa.text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM information_schema.tables"
            "  WHERE table_schema = current_schema()"
            "    AND table_name = :t"
            ")"
        ),
        {"t": table_name},
    )
    return result.scalar()


def _column_exists(conn, table_name: str, column_name: str) -> bool:
    result = conn.execute(
        sa.text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM information_schema.columns"
            "  WHERE table_schema = current_schema()"
            "    AND table_name = :t"
            "    AND column_name = :c"
            ")"
        ),
        {"t": table_name, "c": column_name},
    )
    return result.scalar()


def upgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, "refresh_sla_configs"):
        return
    if _column_exists(conn, "refresh_sla_configs", "last_breach_episode_opened_on"):
        return

    op.add_column(
        "refresh_sla_configs",
        sa.Column("last_breach_episode_opened_on", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, "refresh_sla_configs"):
        return
    if _column_exists(conn, "refresh_sla_configs", "last_breach_episode_opened_on"):
        op.drop_column("refresh_sla_configs", "last_breach_episode_opened_on")
