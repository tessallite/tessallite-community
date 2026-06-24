"""Tenant schema — all tables in {slug}_meta schema.

Run with MIGRATE_MODE=tenant TENANT_SLUG=<slug> DATABASE_URL=<tenant_db_url> alembic upgrade head

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-19
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "0002"
down_revision = None
branch_labels = ("tenant",)
depends_on = None

# Schema is set dynamically via search_path in env.py; use unqualified table names.


def upgrade() -> None:
    # local_users
    op.create_table(
        "local_users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("username", sa.String(255), nullable=False, unique=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    # projects
    op.create_table(
        "projects",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
    )

    # project_connections
    op.create_table(
        "project_connections",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("connection_type", sa.String(32), nullable=False),
        sa.Column("encrypted_credentials", sa.LargeBinary, nullable=False),
        sa.Column("config", JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    # data_targets (created before models so models can FK to it)
    op.create_table(
        "data_targets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("model_id", UUID(as_uuid=True), nullable=False),  # FK added after models
        sa.Column("project_connection_id", UUID(as_uuid=True), sa.ForeignKey("project_connections.id"), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("config", JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    # models
    op.create_table(
        "models",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("target_id", UUID(as_uuid=True), sa.ForeignKey("data_targets.id", ondelete="SET NULL")),
        sa.Column("refresh_strategy", sa.String(32), nullable=False, server_default="scheduled"),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("aggregations_enabled", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("seed", sa.String(64), nullable=False),
        sa.Column("max_aggregates", sa.Integer, nullable=False, server_default="50"),
        sa.Column("miss_threshold_daily", sa.Integer, nullable=False, server_default="3"),
        sa.Column("miss_threshold_weekly", sa.Integer, nullable=False, server_default="5"),
        sa.Column("schema_drift_interval_hours", sa.Integer, nullable=False, server_default="24"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("project_id", "slug"),
    )

    # now add the FK from data_targets.model_id → models.id
    op.create_foreign_key("fk_data_targets_model", "data_targets", "models", ["model_id"], ["id"], ondelete="CASCADE")

    # data_sources
    op.create_table(
        "data_sources",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_connection_id", UUID(as_uuid=True), sa.ForeignKey("project_connections.id"), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("config", JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    # model_tables
    op.create_table(
        "model_tables",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("table_type", sa.String(16), nullable=False),
        sa.Column("physical_name", sa.String(512), nullable=False),
        sa.Column("alias", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("row_count_estimate", sa.BigInteger),
        sa.Column("last_stats_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    # model_columns
    op.create_table(
        "model_columns",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("model_table_id", UUID(as_uuid=True), sa.ForeignKey("model_tables.id", ondelete="CASCADE"), nullable=False),
        sa.Column("column_name", sa.String(255), nullable=False),
        sa.Column("data_type", sa.String(64), nullable=False),
        sa.Column("is_nullable", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("cardinality_estimate", sa.BigInteger),
        sa.Column("last_stats_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("model_table_id", "column_name"),
    )

    # dimensions
    op.create_table(
        "dimensions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255)),
        sa.Column("source_column_id", UUID(as_uuid=True), sa.ForeignKey("model_columns.id", ondelete="SET NULL")),
        sa.Column("hierarchy", JSONB),
        sa.Column("is_time_dim", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("time_grain", sa.String(32)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("model_id", "name"),
    )

    # measures
    op.create_table(
        "measures",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255)),
        sa.Column("source_column_id", UUID(as_uuid=True), sa.ForeignKey("model_columns.id", ondelete="SET NULL")),
        sa.Column("measure_type", sa.String(32), nullable=False, server_default="standard"),
        sa.Column("expression", sa.Text),
        sa.Column("data_type", sa.String(32), nullable=False, server_default="numeric"),
        sa.Column("default_agg", sa.String(32), nullable=False, server_default="sum"),
        sa.Column("is_additive", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("model_id", "name"),
    )

    # joins
    op.create_table(
        "joins",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=False),
        sa.Column("left_table_id", UUID(as_uuid=True), sa.ForeignKey("model_tables.id", ondelete="CASCADE"), nullable=False),
        sa.Column("right_table_id", UUID(as_uuid=True), sa.ForeignKey("model_tables.id", ondelete="CASCADE"), nullable=False),
        sa.Column("join_type", sa.String(32), nullable=False, server_default="many_to_one"),
        sa.Column("left_column_id", UUID(as_uuid=True), sa.ForeignKey("model_columns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("right_column_id", UUID(as_uuid=True), sa.ForeignKey("model_columns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    # aggregate_definitions
    op.create_table(
        "aggregate_definitions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_id", UUID(as_uuid=True), sa.ForeignKey("data_targets.id"), nullable=False),
        sa.Column("physical_table_name", sa.String(512), nullable=False),
        sa.Column("target_schema", sa.String(255)),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("grain", JSONB, nullable=False, server_default="[]"),
        sa.Column("source_row_count", sa.BigInteger),
        sa.Column("agg_row_count", sa.BigInteger),
        sa.Column("estimated_hit_rate", sa.Float),
        sa.Column("creation_reason", sa.String(32), nullable=False, server_default="auto"),
        sa.Column("include_quantiles", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True)),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
    )

    # aggregate_columns
    op.create_table(
        "aggregate_columns",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("aggregate_definition_id", UUID(as_uuid=True), sa.ForeignKey("aggregate_definitions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("measure_id", UUID(as_uuid=True), sa.ForeignKey("measures.id", ondelete="SET NULL")),
        sa.Column("physical_col_name", sa.String(255), nullable=False),
        sa.Column("stat_type", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    # aggregate_refresh_policies
    op.create_table(
        "aggregate_refresh_policies",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("aggregate_definition_id", UUID(as_uuid=True), sa.ForeignKey("aggregate_definitions.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("refresh_mode", sa.String(32), nullable=False, server_default="scheduled"),
        sa.Column("cron_expression", sa.String(128)),
        sa.Column("incremental_column", sa.String(255)),
        sa.Column("incremental_lookback", sa.Integer),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    # aggregate_refresh_runs
    op.create_table(
        "aggregate_refresh_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("aggregate_definition_id", UUID(as_uuid=True), sa.ForeignKey("aggregate_definitions.id"), nullable=False),
        sa.Column("refresh_mode", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("rows_written", sa.BigInteger),
        sa.Column("bytes_processed", sa.BigInteger),
        sa.Column("error_message", sa.Text),
        sa.Column("triggered_by", sa.String(32), nullable=False, server_default="scheduler"),
    )

    # query_logs
    op.create_table(
        "query_logs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="SET NULL")),
        sa.Column("user_identity", sa.String(255)),
        sa.Column("protocol", sa.String(16), nullable=False),
        sa.Column("raw_query", sa.Text, nullable=False),
        sa.Column("query_fingerprint", sa.String(64), nullable=False),
        sa.Column("route_type", sa.String(32), nullable=False),
        sa.Column("aggregate_id", UUID(as_uuid=True), sa.ForeignKey("aggregate_definitions.id", ondelete="SET NULL")),
        sa.Column("rewritten_query", sa.Text),
        sa.Column("execution_ms", sa.Integer),
        sa.Column("rows_returned", sa.BigInteger),
        sa.Column("bytes_processed", sa.BigInteger),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("idx_query_logs_fingerprint", "query_logs", ["query_fingerprint"])
    op.create_index("idx_query_logs_model_id", "query_logs", ["model_id"])
    op.create_index("idx_query_logs_created_at", "query_logs", ["created_at"])

    # query_miss_logs
    op.create_table(
        "query_miss_logs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="SET NULL")),
        sa.Column("query_fingerprint", sa.String(64), nullable=False),
        sa.Column("miss_reason", sa.String(64), nullable=False),
        sa.Column("normalized_query", sa.Text),
        sa.Column("requested_dimensions", JSONB),
        sa.Column("requested_measures", JSONB),
        sa.Column("requested_grain", JSONB),
        sa.Column("occurrence_count", sa.Integer, nullable=False, server_default="1"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("candidate_aggregate_id", UUID(as_uuid=True), sa.ForeignKey("aggregate_definitions.id", ondelete="SET NULL")),
        sa.UniqueConstraint("model_id", "query_fingerprint"),
    )

    # route_logs
    op.create_table(
        "route_logs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("query_log_id", UUID(as_uuid=True), sa.ForeignKey("query_logs.id", ondelete="CASCADE")),
        sa.Column("route_stage", sa.String(64), nullable=False),
        sa.Column("detail", JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    # lineage_mappings
    op.create_table(
        "lineage_mappings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=False),
        sa.Column("semantic_field_name", sa.String(255), nullable=False),
        sa.Column("semantic_field_type", sa.String(32), nullable=False),
        sa.Column("aggregate_col_id", UUID(as_uuid=True), sa.ForeignKey("aggregate_columns.id", ondelete="SET NULL")),
        sa.Column("source_column_id", UUID(as_uuid=True), sa.ForeignKey("model_columns.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    # schema_change_events
    op.create_table(
        "schema_change_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("data_sources.id", ondelete="SET NULL")),
        sa.Column("table_name", sa.String(512)),
        sa.Column("change_type", sa.String(32), nullable=False),
        sa.Column("is_breaking", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("detail", JSONB, nullable=False, server_default="{}"),
        sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
    )

    # user_access_bindings
    op.create_table(
        "user_access_bindings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_identity", sa.String(255), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE")),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("user_identity", "role", "project_id", "model_id"),
    )


def downgrade() -> None:
    for table in [
        "user_access_bindings", "schema_change_events", "lineage_mappings",
        "route_logs", "query_miss_logs", "query_logs", "aggregate_refresh_runs",
        "aggregate_refresh_policies", "aggregate_columns", "aggregate_definitions",
        "joins", "measures", "dimensions", "model_columns", "model_tables",
        "data_sources", "models", "data_targets", "project_connections",
        "projects", "local_users",
    ]:
        op.drop_table(table)
