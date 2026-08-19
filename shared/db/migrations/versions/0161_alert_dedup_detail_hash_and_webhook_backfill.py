"""Add detail_hash to model_alerts dedup index; one-time webhook filter backfill.

Bug-7453: the NULLS NOT DISTINCT dedup index on model_alerts collapses distinct
alerts when both related_object_type and related_object_id are NULL. Adding a
detail_hash column (SHA-256 of title+detail) to the index lets model-wide alerts
with different content coexist.

Bug-6849: one-time data migration that backfills webhook endpoints stored with
event_filters=[] (empty list) under the pre-Bug-6313 semantic to ["*"] (wildcard).
Previously this ran as a perpetual boot sweep (removed by Bug-7330); this
migration makes it a genuine one-time operation.

Revision ID: 0161
Revises: 0160
Create Date: 2026-07-13
"""
from __future__ import annotations

import hashlib

import sqlalchemy as sa
from alembic import op

revision = "0161"
down_revision = "0160"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    # ---- Bug-7453: model_alerts.detail_hash column + updated dedup index ----
    if "model_alerts" in table_names:
        columns = {col["name"] for col in inspector.get_columns("model_alerts")}
        if "detail_hash" not in columns:
            op.add_column(
                "model_alerts",
                sa.Column("detail_hash", sa.String(64), nullable=True),
            )

        # Backfill detail_hash for existing rows.
        conn = op.get_bind()
        rows = conn.execute(
            sa.text(
                "SELECT id, title, detail FROM model_alerts "
                "WHERE detail_hash IS NULL"
            )
        ).fetchall()
        for row_id, title, detail in rows:
            payload = title or ""
            if detail:
                payload = f"{payload}\x00{detail}"
            d_hash = hashlib.sha256(
                payload.encode("utf-8", errors="replace")
            ).hexdigest()[:32]
            conn.execute(
                sa.text(
                    "UPDATE model_alerts SET detail_hash = :h WHERE id = :id"
                ),
                {"h": d_hash, "id": row_id},
            )

        # Recreate the dedup index to include detail_hash.
        op.execute("DROP INDEX IF EXISTS idx_model_alerts_dedup")
        op.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_model_alerts_dedup
            ON model_alerts (
                model_id,
                category,
                related_object_type,
                related_object_id,
                detail_hash
            ) NULLS NOT DISTINCT
            WHERE resolved_at IS NULL AND dismissed_at IS NULL
            """
        )

    # ---- Bug-6849: one-time webhook event_filters backfill ----
    if "webhook_endpoints" in table_names:
        conn = op.get_bind()
        # Find active endpoints with empty event_filters (JSON '[]' or NULL).
        # Cast to text for the comparison since JSON column types vary.
        result = conn.execute(
            sa.text(
                "SELECT id FROM webhook_endpoints "
                "WHERE is_active = true "
                "AND (event_filters IS NULL OR event_filters::text = '[]')"
            )
        ).fetchall()
        for (ep_id,) in result:
            conn.execute(
                sa.text(
                    "UPDATE webhook_endpoints "
                    "SET event_filters = :val "
                    "WHERE id = :id"
                ),
                {"val": '["*"]', "id": ep_id},
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "model_alerts" in table_names:
        # Restore the previous dedup index without detail_hash.
        op.execute("DROP INDEX IF EXISTS idx_model_alerts_dedup")
        op.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_model_alerts_dedup
            ON model_alerts (
                model_id,
                category,
                related_object_type,
                related_object_id
            ) NULLS NOT DISTINCT
            WHERE resolved_at IS NULL AND dismissed_at IS NULL
            """
        )
        columns = {col["name"] for col in inspector.get_columns("model_alerts")}
        if "detail_hash" in columns:
            op.drop_column("model_alerts", "detail_hash")
