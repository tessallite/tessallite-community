"""Model flags and metadata performance indexes.

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-06
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = :table AND column_name = :column"
        ),
        {"table": table, "column": column},
    )
    return result.scalar() is not None


def _index_exists(index_name: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM pg_indexes WHERE indexname = :name"
        ),
        {"name": index_name},
    )
    return result.scalar() is not None


def upgrade() -> None:
    if not _column_exists("models", "aggregations_enabled"):
        op.add_column(
            "models",
            sa.Column("aggregations_enabled", sa.Boolean(), nullable=False, server_default="true"),
        )
    op.alter_column("models", "status", server_default="active")
    op.execute("UPDATE models SET status = 'active' WHERE status = 'draft'")

    for idx_name, table, columns in [
        ("idx_models_project_id", "models", ["project_id"]),
        ("idx_data_sources_model_id", "data_sources", ["model_id"]),
        ("idx_data_targets_model_id", "data_targets", ["model_id"]),
        ("idx_model_tables_model_id", "model_tables", ["model_id"]),
        ("idx_model_tables_source_id", "model_tables", ["source_id"]),
        ("idx_query_miss_logs_model_id", "query_miss_logs", ["model_id"]),
        ("idx_query_miss_logs_last_seen_at", "query_miss_logs", ["last_seen_at"]),
        ("idx_query_miss_logs_occurrence", "query_miss_logs", ["occurrence_count"]),
        ("idx_route_logs_query_log_id", "route_logs", ["query_log_id"]),
        ("idx_aggregate_refresh_runs_aggregate_definition_id", "aggregate_refresh_runs", ["aggregate_definition_id"]),
        ("idx_aggregate_refresh_runs_started_at", "aggregate_refresh_runs", ["started_at"]),
    ]:
        if not _index_exists(idx_name):
            op.create_index(idx_name, table, columns, unique=False)


def downgrade() -> None:
    op.drop_index("idx_aggregate_refresh_runs_started_at", table_name="aggregate_refresh_runs")
    op.drop_index("idx_aggregate_refresh_runs_aggregate_definition_id", table_name="aggregate_refresh_runs")
    op.drop_index("idx_route_logs_query_log_id", table_name="route_logs")
    op.drop_index("idx_query_miss_logs_occurrence", table_name="query_miss_logs")
    op.drop_index("idx_query_miss_logs_last_seen_at", table_name="query_miss_logs")
    op.drop_index("idx_query_miss_logs_model_id", table_name="query_miss_logs")
    op.drop_index("idx_model_tables_source_id", table_name="model_tables")
    op.drop_index("idx_model_tables_model_id", table_name="model_tables")
    op.drop_index("idx_data_targets_model_id", table_name="data_targets")
    op.drop_index("idx_data_sources_model_id", table_name="data_sources")
    op.drop_index("idx_models_project_id", table_name="models")

    op.alter_column("models", "status", server_default="draft")
    op.drop_column("models", "aggregations_enabled")
