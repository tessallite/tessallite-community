"""Pocket table metadata and query-log route linkage.

Revision ID: 0018
Revises: 0017
Create Date: 2026-04-18

Adds tenant-schema tables:
- pocket_definitions
- pocket_predicates
- pocket_refresh_runs

And extends query_logs with pocket_id for route attribution.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pocket_definitions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_id", UUID(as_uuid=True), sa.ForeignKey("data_targets.id"), nullable=False),
        sa.Column("source_table", sa.String(512), nullable=False),
        sa.Column("physical_table_name", sa.String(512), nullable=False),
        sa.Column("target_schema", sa.String(255)),
        sa.Column("defining_sql", sa.Text(), nullable=False),
        sa.Column("query_fingerprint", sa.String(64), nullable=False),
        sa.Column("row_count", sa.BigInteger()),
        sa.Column("storage_bytes", sa.BigInteger()),
        sa.Column("refresh_policy", sa.String(32), nullable=False, server_default="schedule"),
        sa.Column("refresh_cron", sa.String(128)),
        sa.Column("refresh_event_name", sa.String(128)),
        sa.Column("incremental_column", sa.String(255)),
        sa.Column("incremental_lookback_hours", sa.Integer()),
        sa.Column("ttl_days", sa.Integer(), nullable=False, server_default="14"),
        sa.Column("status", sa.String(32), nullable=False, server_default="fresh"),
        sa.Column("failure_reason", sa.Text()),
        sa.Column("last_refresh_at", sa.DateTime(timezone=True)),
        sa.Column("last_access_at", sa.DateTime(timezone=True)),
        sa.Column("last_match_at", sa.DateTime(timezone=True)),
        sa.Column("hit_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("time_saved_ms_total", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_pocket_definitions_model_fingerprint_status",
        "pocket_definitions",
        ["model_id", "query_fingerprint", "status"],
    )
    op.create_index(
        "ix_pocket_definitions_last_access_at",
        "pocket_definitions",
        ["last_access_at"],
    )

    op.create_table(
        "pocket_predicates",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "pocket_definition_id",
            UUID(as_uuid=True),
            sa.ForeignKey("pocket_definitions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("column_name", sa.String(255), nullable=False),
        sa.Column("operator", sa.String(16), nullable=False),
        sa.Column("value_json", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("pocket_definition_id", "column_name", "operator", "value_json"),
    )
    op.create_index(
        "ix_pocket_predicates_pocket_id",
        "pocket_predicates",
        ["pocket_definition_id"],
    )

    op.create_table(
        "pocket_refresh_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "pocket_definition_id",
            UUID(as_uuid=True),
            sa.ForeignKey("pocket_definitions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("refresh_mode", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("rows_written", sa.BigInteger()),
        sa.Column("bytes_processed", sa.BigInteger()),
        sa.Column("error_message", sa.Text()),
        sa.Column("triggered_by", sa.String(32), nullable=False, server_default="scheduler"),
    )

    op.add_column("query_logs", sa.Column("pocket_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_query_logs_pocket_id",
        "query_logs",
        "pocket_definitions",
        ["pocket_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_query_logs_pocket_id", "query_logs", type_="foreignkey")
    op.drop_column("query_logs", "pocket_id")

    op.drop_table("pocket_refresh_runs")
    op.drop_index("ix_pocket_predicates_pocket_id", table_name="pocket_predicates")
    op.drop_table("pocket_predicates")
    op.drop_index("ix_pocket_definitions_last_access_at", table_name="pocket_definitions")
    op.drop_index("ix_pocket_definitions_model_fingerprint_status", table_name="pocket_definitions")
    op.drop_table("pocket_definitions")
