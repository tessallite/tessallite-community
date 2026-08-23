"""Add signing_secret_snapshot to webhook_deliveries (F-022-06).

A webhook delivery is signed with the endpoint's signing secret. Previously a
retry rebuilt the signed body using the endpoint's CURRENT encrypted secret, so
rotating the endpoint secret while a delivery was still queued re-signed the
in-flight request with the new secret — receivers that still expected the old
secret for in-flight deliveries then rejected it, producing avoidable failures
and DLQ entries.

This column pins the signing secret to the delivery at enqueue time: the
encrypted secret is snapshotted when the row is created, and every retry signs
with that snapshot rather than the endpoint's current secret. Rotation no longer
invalidates queued signatures.

Tenant-schema guarded (skip when the schema has no ``webhook_deliveries``
table), idempotent, reversible.

Revision ID: 0173
Revises: 0172
Create Date: 2026-07-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0173"
down_revision = "0172"
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
    if not _column_exists(conn, "webhook_deliveries", "signing_secret_snapshot"):
        op.add_column(
            "webhook_deliveries",
            sa.Column("signing_secret_snapshot", sa.LargeBinary(), nullable=True),
        )


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, "webhook_deliveries"):
        return
    if _column_exists(conn, "webhook_deliveries", "signing_secret_snapshot"):
        op.drop_column("webhook_deliveries", "signing_secret_snapshot")
