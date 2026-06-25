"""Add indexes to all ForeignKey columns missing them.

Bulk-adds B-tree indexes to 68 FK columns across tenant-schema tables.
These indexes accelerate CASCADE deletes, JOIN lookups, and WHERE
filters on FK columns that were previously unindexed.
"""
from alembic import op

revision = "0095"
down_revision = "0094"

FK_INDEXES = [
    ("project_connections", "project_id"),
    ("models", "target_id"),
    ("models", "llm_config_id"),
    ("data_sources", "model_id"),
    ("data_sources", "project_connection_id"),
    ("data_targets", "model_id"),
    ("data_targets", "project_connection_id"),
    ("model_tables", "model_id"),
    ("model_tables", "source_id"),
    ("user_defined_attributes", "table_id"),
    ("user_defined_attribute_column_refs", "column_id"),
    ("dimensions", "source_column_id"),
    ("dimensions", "user_defined_attribute_id"),
    ("measures", "source_column_id"),
    ("measures", "user_defined_attribute_id"),
    ("measures", "variant_of_measure_id"),
    ("measures", "semi_additive_account_column_id"),
    ("measures", "hierarchy_id"),
    ("measures", "resolved_calendar_id"),
    ("measures", "resolved_date_col_id"),
    ("measures", "date_dimension_column_id"),
    ("drill_through_sets", "source_table_id"),
    ("joins", "model_id"),
    ("joins", "left_table_id"),
    ("joins", "right_table_id"),
    ("joins", "left_column_id"),
    ("joins", "right_column_id"),
    ("aggregate_definitions", "model_id"),
    ("aggregate_definitions", "target_id"),
    ("aggregate_columns", "aggregate_definition_id"),
    ("aggregate_columns", "measure_id"),
    ("aggregate_refresh_runs", "aggregate_definition_id"),
    ("pocket_definitions", "target_id"),
    ("pocket_refresh_runs", "pocket_definition_id"),
    ("row_security_rules", "mapping_table_id"),
    ("lineage_mappings", "model_id"),
    ("lineage_mappings", "aggregate_col_id"),
    ("lineage_mappings", "source_column_id"),
    ("schema_change_events", "model_id"),
    ("schema_change_events", "source_id"),
    ("model_alerts", "model_id"),
    ("user_access_bindings", "project_id"),
    ("user_access_bindings", "model_id"),
    ("model_telemetry_snapshots", "model_id"),
    ("ai_optimizer_runs", "model_id"),
    ("ai_optimizer_runs", "telemetry_snapshot_id"),
    ("ai_aggregate_recommendations", "model_id"),
    ("ai_aggregate_recommendations", "optimizer_run_id"),
    ("ai_aggregate_recommendations", "aggregate_definition_id"),
    ("model_ai_scheduler_config", "llm_config_id"),
    ("glossary_entry", "model_id"),
    ("glossary_entry", "superseded_by"),
    ("glossary_synonym", "entry_id"),
    ("glossary_attachment", "entry_id"),
    ("glossary_share_token", "model_id"),
    ("project_agent_configs", "primary_model_id"),
    ("project_agent_configs", "answer_llm_config_id"),
    ("project_agent_configs", "judge_llm_config_id"),
    ("project_agent_configs", "judge_rubric_id"),
    ("project_persona_model_scopes", "model_id"),
    ("agent_conversations", "persona_id"),
    ("agent_turns", "recipe_id"),
    ("agent_webhook_dlq", "conversation_id"),
    ("agent_webhook_dlq", "turn_id"),
    ("data_quality_violations", "pocket_id"),
    ("agent_cost_ledger", "project_id"),
    ("agent_cost_ledger", "turn_id"),
    ("agent_cost_ledger", "llm_config_id"),
]


def _idx_name(table: str, column: str) -> str:
    return f"ix_{table}_{column}"


def upgrade() -> None:
    for table, column in FK_INDEXES:
        op.create_index(
            _idx_name(table, column),
            table,
            [column],
            if_not_exists=True,
        )


def downgrade() -> None:
    for table, column in reversed(FK_INDEXES):
        op.drop_index(_idx_name(table, column), table_name=table)
