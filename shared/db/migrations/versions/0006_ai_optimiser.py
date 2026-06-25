"""AI Optimiser — new tables and column extensions.

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-08

New tables:
  - llm_provider_configs
  - model_telemetry_snapshots
  - ai_optimizer_runs
  - ai_aggregate_recommendations
  - model_ai_scheduler_config

Altered tables:
  - aggregate_columns: add aggregation_function
  - aggregate_definitions: add is_stale
  - models: add llm_config_id, max_ai_recommendations
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "0006"
down_revision = "0005"
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


def _table_exists(table: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = :table"
        ),
        {"table": table},
    )
    return result.scalar() is not None


def _index_exists(index_name: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text("SELECT 1 FROM pg_indexes WHERE indexname = :name"),
        {"name": index_name},
    )
    return result.scalar() is not None


def upgrade() -> None:
    # ---- New tables ----

    if not _table_exists("llm_provider_configs"):
        op.create_table(
            "llm_provider_configs",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("provider", sa.String(32), nullable=False),
            sa.Column("display_name", sa.String(255), nullable=False),
            sa.Column("base_url", sa.Text),
            sa.Column("encrypted_api_key", sa.LargeBinary),
            sa.Column("model_name", sa.String(128), nullable=False),
            sa.Column("max_tokens", sa.Integer, nullable=False, server_default="4096"),
            sa.Column("temperature", sa.Float, nullable=False, server_default="0.2"),
            sa.Column("timeout_seconds", sa.Integer, nullable=False, server_default="60"),
            sa.Column("is_active", sa.Boolean, nullable=False, server_default="false"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        )

    if not _table_exists("model_telemetry_snapshots"):
        op.create_table(
            "model_telemetry_snapshots",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=False),
            sa.Column("snapshot_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("lookback_hours", sa.Integer, nullable=False),
            sa.Column("snapshot_json", JSONB, nullable=False),
            sa.Column("total_miss_patterns", sa.Integer, nullable=False, server_default="0"),
            sa.Column("total_miss_occurrences", sa.Integer, nullable=False, server_default="0"),
            sa.Column("top_cost_score", sa.Float),
            sa.Column("triggered_by", sa.String(32), nullable=False, server_default="scheduler"),
        )
        if not _index_exists("idx_telemetry_model_snapshot"):
            op.create_index(
                "idx_telemetry_model_snapshot",
                "model_telemetry_snapshots",
                ["model_id", sa.text("snapshot_at DESC")],
            )

    if not _table_exists("ai_optimizer_runs"):
        op.create_table(
            "ai_optimizer_runs",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=False),
            sa.Column("triggered_by", sa.String(32), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="running"),
            sa.Column("is_dry_run", sa.Boolean, nullable=False, server_default="false"),
            sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.Column("llm_provider", sa.String(64)),
            sa.Column("llm_model", sa.String(128)),
            sa.Column("telemetry_snapshot_id", UUID(as_uuid=True), sa.ForeignKey("model_telemetry_snapshots.id")),
            sa.Column("recommendations_count", sa.Integer, nullable=False, server_default="0"),
            sa.Column("aggregates_created", sa.Integer, nullable=False, server_default="0"),
            sa.Column("aggregates_skipped", sa.Integer, nullable=False, server_default="0"),
            sa.Column("error_message", sa.Text),
            sa.Column("raw_llm_response", sa.Text),
        )
        if not _index_exists("idx_ai_runs_model_started"):
            op.create_index(
                "idx_ai_runs_model_started",
                "ai_optimizer_runs",
                ["model_id", sa.text("started_at DESC")],
            )

    if not _table_exists("ai_aggregate_recommendations"):
        op.create_table(
            "ai_aggregate_recommendations",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=False),
            sa.Column("optimizer_run_id", UUID(as_uuid=True), sa.ForeignKey("ai_optimizer_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("grain", JSONB, nullable=False),
            sa.Column("measures", JSONB, nullable=False),
            sa.Column("rationale", sa.Text),
            sa.Column("addresses_fingerprints", JSONB, nullable=False, server_default="[]"),
            sa.Column("estimated_hit_rate", sa.Float),
            sa.Column("priority", sa.Integer, nullable=False, server_default="1"),
            sa.Column("status", sa.String(32), nullable=False, server_default="applied"),
            sa.Column("aggregate_definition_id", UUID(as_uuid=True), sa.ForeignKey("aggregate_definitions.id", ondelete="SET NULL")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        )
        if not _index_exists("idx_ai_recs_model_status"):
            op.create_index(
                "idx_ai_recs_model_status",
                "ai_aggregate_recommendations",
                ["model_id", "status"],
            )

    if not _table_exists("model_ai_scheduler_config"):
        op.create_table(
            "model_ai_scheduler_config",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=False, unique=True),
            sa.Column("ai_enabled", sa.Boolean, nullable=False, server_default="false"),
            sa.Column("cron_expression", sa.String(128), nullable=False, server_default="0 5 * * *"),
            sa.Column("lookback_hours", sa.Integer, nullable=False, server_default="168"),
            sa.Column("max_creates_per_run", sa.Integer, nullable=False, server_default="3"),
            sa.Column("min_confidence", sa.Float, nullable=False, server_default="0.5"),
            sa.Column("dry_run", sa.Boolean, nullable=False, server_default="false"),
            sa.Column("enable_ai_aggregation", sa.Boolean, nullable=False, server_default="true"),
            sa.Column("llm_config_id", UUID(as_uuid=True), sa.ForeignKey("llm_provider_configs.id", ondelete="SET NULL")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        )

    # ---- Existing table extensions ----

    if not _column_exists("aggregate_columns", "aggregation_function"):
        op.add_column(
            "aggregate_columns",
            sa.Column("aggregation_function", sa.String(32)),
            # Values: sum|avg|count|count_distinct|min|max
            # If NULL, falls back to the measure's default_agg (existing behaviour)
        )

    if not _column_exists("aggregate_definitions", "is_stale"):
        op.add_column(
            "aggregate_definitions",
            sa.Column("is_stale", sa.Boolean, nullable=False, server_default="false"),
        )

    if not _column_exists("models", "llm_config_id"):
        op.add_column(
            "models",
            sa.Column("llm_config_id", UUID(as_uuid=True), sa.ForeignKey("llm_provider_configs.id", ondelete="SET NULL")),
        )

    if not _column_exists("models", "max_ai_recommendations"):
        op.add_column(
            "models",
            sa.Column("max_ai_recommendations", sa.Integer, nullable=False, server_default="5"),
        )


def downgrade() -> None:
    # Remove extensions first
    if _column_exists("models", "max_ai_recommendations"):
        op.drop_column("models", "max_ai_recommendations")
    if _column_exists("models", "llm_config_id"):
        op.drop_column("models", "llm_config_id")
    if _column_exists("aggregate_definitions", "is_stale"):
        op.drop_column("aggregate_definitions", "is_stale")
    if _column_exists("aggregate_columns", "aggregation_function"):
        op.drop_column("aggregate_columns", "aggregation_function")

    # Drop new tables in reverse dependency order
    for table in [
        "ai_aggregate_recommendations",
        "ai_optimizer_runs",
        "model_telemetry_snapshots",
        "model_ai_scheduler_config",
        "llm_provider_configs",
    ]:
        if _table_exists(table):
            op.drop_table(table)
