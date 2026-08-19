"""Truthful cache telemetry + honest import-version snapshot marking.

Bug-6426: add ``query_logs.cache_status`` so an in-TTL result-cache re-serve is
distinguishable from a real route execution. The row keeps its original
``route_type`` (aggregate/pocket/source) for volume analytics, but
``cache_status='cache_hit'`` marks that no route executed and its zero
``execution_ms``/``bytes_processed`` are not real measurements. Acceleration-rate
and cost-savings rollups exclude these rows so a cache re-serve is never counted
as an acceleration hit nor averaged into savings. NULL == 'live' for historical
rows.

Bug-6295: add ``model_versions.snapshot_unavailable``. The export bundle omits
each version's ``snapshot_json`` (Bug-7623), so imported version history has no
faithful historical shape. The importer marks those rows True and stores a
placeholder snapshot; revert refuses them so today's shape can never be served
under an old version label.

Revision ID: 0179
Revises: 0178
Create Date: 2026-07-22
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0179"
down_revision = "0178"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "query_logs" in table_names:
        columns = {col["name"] for col in inspector.get_columns("query_logs")}
        if "cache_status" not in columns:
            op.add_column(
                "query_logs",
                sa.Column("cache_status", sa.String(length=16), nullable=True),
            )
            op.create_index(
                "ix_query_logs_cache_status", "query_logs", ["cache_status"]
            )

    if "model_versions" in table_names:
        columns = {col["name"] for col in inspector.get_columns("model_versions")}
        if "snapshot_unavailable" not in columns:
            op.add_column(
                "model_versions",
                sa.Column(
                    "snapshot_unavailable",
                    sa.Boolean(),
                    nullable=False,
                    server_default="false",
                ),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "model_versions" in table_names:
        columns = {col["name"] for col in inspector.get_columns("model_versions")}
        if "snapshot_unavailable" in columns:
            op.drop_column("model_versions", "snapshot_unavailable")

    if "query_logs" in table_names:
        columns = {col["name"] for col in inspector.get_columns("query_logs")}
        if "cache_status" in columns:
            indexes = {ix["name"] for ix in inspector.get_indexes("query_logs")}
            if "ix_query_logs_cache_status" in indexes:
                op.drop_index("ix_query_logs_cache_status", table_name="query_logs")
            op.drop_column("query_logs", "cache_status")
