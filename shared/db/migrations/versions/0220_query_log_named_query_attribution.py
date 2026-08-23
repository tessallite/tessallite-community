"""Add nullable Named Query attribution to existing QueryLog telemetry.

Bug-9172: Named Query execution already writes QueryLog timing and byte-cost
telemetry. The missing contract is attribution. Ordinary rows stay valid with
NULL attribution, historical rows are not backfilled, and no foreign key is
introduced because log retention intentionally outlives governed objects.

Revision ID: 0220
Revises: 0219
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0220"
down_revision = "0219"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "query_logs" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("query_logs")}
    if "named_query_id" not in columns:
        op.add_column(
            "query_logs",
            sa.Column("named_query_id", sa.UUID(), nullable=True),
        )
    if "named_query_fallback_reason" not in columns:
        op.add_column(
            "query_logs",
            sa.Column(
                "named_query_fallback_reason",
                sa.String(length=128),
                nullable=True,
            ),
        )

    indexes = {index["name"] for index in inspector.get_indexes("query_logs")}
    if "ix_query_logs_named_query_id" not in indexes:
        op.create_index(
            "ix_query_logs_named_query_id", "query_logs", ["named_query_id"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "query_logs" not in inspector.get_table_names():
        return

    indexes = {index["name"] for index in inspector.get_indexes("query_logs")}
    if "ix_query_logs_named_query_id" in indexes:
        op.drop_index("ix_query_logs_named_query_id", table_name="query_logs")

    columns = {column["name"] for column in inspector.get_columns("query_logs")}
    if "named_query_fallback_reason" in columns:
        op.drop_column("query_logs", "named_query_fallback_reason")
    if "named_query_id" in columns:
        op.drop_column("query_logs", "named_query_id")
