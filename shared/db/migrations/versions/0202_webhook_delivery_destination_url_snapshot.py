"""Add destination_url_snapshot to webhook_deliveries (Bug-8557).

A webhook delivery's destination URL is snapshotted at enqueue time so the
dispatcher reads the frozen URL, never the live endpoint.url. An admin updating
the endpoint while deliveries are still queued no longer redirects in-flight
deliveries to a different receiver.

Backfills queued (status='pending') rows from their endpoint's current URL.
Rows in other states (delivered, dlq, failed) are left NULL — they are
already final.

Tenant-schema guarded (skip when the schema has no ``webhook_deliveries``
table), idempotent, reversible.

Revision ID: 0202
Revises: 0201
Create Date: 2026-08-01
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0202"
down_revision = "0201"
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
    if not _table_exists(conn, "webhook_deliveries"):
        return
    if _column_exists(conn, "webhook_deliveries", "destination_url_snapshot"):
        return

    op.add_column(
        "webhook_deliveries",
        sa.Column("destination_url_snapshot", sa.Text(), nullable=True),
    )

    # Backfill queued rows so the next dispatch iteration finds a valid
    # snapshot. The URL is read from the endpoint at migration time — a
    # one-shot best-effort backfill. Rows in any other state are left NULL.
    conn.execute(
        sa.text("""
            UPDATE webhook_deliveries
            SET destination_url_snapshot = we.url
            FROM webhook_endpoints we
            WHERE webhook_deliveries.endpoint_id = we.id
              AND webhook_deliveries.status = 'pending'
              AND webhook_deliveries.destination_url_snapshot IS NULL
        """)
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, "webhook_deliveries"):
        return
    if _column_exists(conn, "webhook_deliveries", "destination_url_snapshot"):
        op.drop_column("webhook_deliveries", "destination_url_snapshot")
