"""Add refresh_prior_status to aggregate_definitions (Bug-7903).

The uniform refresh pending-guard flips a servable aggregate to "pending"
(committed, non-servable) before any physical change and restores it after the
new run + manifest + VERIFIED evidence commit. A process crash loses any
in-memory snapshot, so the PRE-refresh status is persisted here in the same
committed transaction as the pending flip. Recovery (the sweep re-running a
stuck-"pending" aggregate) restores it to EXACTLY its prior state — never
re-activating one that was "disabled"/"retired". The rehydrator also sets this
when it forces active/disabled aggregates to pending on import.

Tenant-schema guarded (skip when the schema has no ``aggregate_definitions``
table), idempotent, reversible.

Revision ID: 0171
Revises: 0170
Create Date: 2026-07-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0171"
down_revision = "0170"
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
    if not _table_exists(conn, "aggregate_definitions"):
        return
    if not _column_exists(conn, "aggregate_definitions", "refresh_prior_status"):
        op.add_column(
            "aggregate_definitions",
            sa.Column("refresh_prior_status", sa.String(length=32), nullable=True),
        )


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, "aggregate_definitions"):
        return
    if _column_exists(conn, "aggregate_definitions", "refresh_prior_status"):
        op.drop_column("aggregate_definitions", "refresh_prior_status")
