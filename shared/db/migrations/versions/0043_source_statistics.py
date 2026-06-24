"""Phase 9 / F1 — Source statistics tables.

Three tables that hold the raw stats the predictive aggregate scorer
(F4) and the demand-defined planner uplift (F8) read from:

- ``source_statistics`` — one row per (data_source_id, model_table_id)
  with the table-level row count and refresh metadata. F2 reuses the
  same row to record cadence (`refresh_cadence`, `last_refreshed_at`,
  `next_refresh_at`).
- ``source_column_statistics`` — one row per column with distinct count,
  null ratio, top-N values (jsonb), min/max as text.
- ``source_join_statistics`` — one row per declared join with the
  measured selectivity (left-side row hits per right-side row).

Columns from F2 (refresh cadence + last/next refreshed timestamps) ship
with this migration so 0044 stays empty — the cadence default is
``'manual'`` per Q7=C.

Revision ID: 0043
Revises: 0042
Create Date: 2026-04-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def _exists(sql: str, **params) -> bool:
    return op.get_bind().execute(sa.text(sql), params).scalar() is not None


def _table_exists(table: str) -> bool:
    return _exists(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = :t",
        t=table,
    )


def upgrade() -> None:
    if not _table_exists("source_statistics"):
        op.create_table(
            "source_statistics",
            sa.Column(
                "id",
                UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "data_source_id",
                UUID(as_uuid=True),
                sa.ForeignKey("data_sources.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "model_table_id",
                UUID(as_uuid=True),
                sa.ForeignKey("model_tables.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("row_count", sa.BigInteger(), nullable=True),
            sa.Column("table_size_bytes", sa.BigInteger(), nullable=True),
            sa.Column(
                "refresh_cadence",
                sa.String(16),
                nullable=False,
                server_default=sa.text("'manual'"),
            ),
            sa.Column("last_refreshed_at", sa.TIMESTAMP(timezone=True)),
            sa.Column("next_refresh_at", sa.TIMESTAMP(timezone=True)),
            sa.Column("last_error", sa.Text()),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.UniqueConstraint(
                "data_source_id",
                "model_table_id",
                name="uq_source_statistics_source_table",
            ),
        )

    if not _table_exists("source_column_statistics"):
        op.create_table(
            "source_column_statistics",
            sa.Column(
                "id",
                UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "source_statistics_id",
                UUID(as_uuid=True),
                sa.ForeignKey("source_statistics.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "model_column_id",
                UUID(as_uuid=True),
                sa.ForeignKey("model_columns.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("distinct_count", sa.BigInteger(), nullable=True),
            sa.Column("null_ratio", sa.Float(), nullable=True),
            sa.Column("min_value", sa.Text(), nullable=True),
            sa.Column("max_value", sa.Text(), nullable=True),
            sa.Column(
                "top_values",
                JSONB(),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column(
                "computed_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.UniqueConstraint(
                "source_statistics_id",
                "model_column_id",
                name="uq_source_column_statistics_table_column",
            ),
        )

    if not _table_exists("source_join_statistics"):
        op.create_table(
            "source_join_statistics",
            sa.Column(
                "id",
                UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "data_source_id",
                UUID(as_uuid=True),
                sa.ForeignKey("data_sources.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "join_id",
                UUID(as_uuid=True),
                sa.ForeignKey("joins.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
            ),
            sa.Column("selectivity", sa.Float(), nullable=True),
            sa.Column("left_distinct_count", sa.BigInteger(), nullable=True),
            sa.Column("right_distinct_count", sa.BigInteger(), nullable=True),
            sa.Column("match_ratio", sa.Float(), nullable=True),
            sa.Column(
                "computed_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
        )


def downgrade() -> None:
    for tbl in (
        "source_join_statistics",
        "source_column_statistics",
        "source_statistics",
    ):
        if _table_exists(tbl):
            op.drop_table(tbl)
