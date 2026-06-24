"""Per-function LLM configuration columns.

Consolidated per-function LLM selection (see
docs/architecture/architecture_llm-function-config.md):

  - project_agent_configs.aggregate_llm_config_id  — project default for the
    aggregate creator (optimizer).
  - project_agent_configs.glossary_llm_config_id   — project default for the
    glossary creator.
  - model_ai_scheduler_config.glossary_llm_config_id — per-model override for the
    glossary creator (aggregate override reuses the existing llm_config_id).

All nullable with ON DELETE SET NULL — NULL means "inherit / fall back".
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0118"
down_revision = "0117"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project_agent_configs",
        sa.Column("aggregate_llm_config_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "project_agent_configs",
        sa.Column("glossary_llm_config_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "model_ai_scheduler_config",
        sa.Column("glossary_llm_config_id", UUID(as_uuid=True), nullable=True),
    )

    op.create_foreign_key(
        "fk_project_agent_configs_aggregate_llm_config",
        "project_agent_configs", "llm_provider_configs",
        ["aggregate_llm_config_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_project_agent_configs_glossary_llm_config",
        "project_agent_configs", "llm_provider_configs",
        ["glossary_llm_config_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_model_ai_scheduler_config_glossary_llm_config",
        "model_ai_scheduler_config", "llm_provider_configs",
        ["glossary_llm_config_id"], ["id"], ondelete="SET NULL",
    )

    op.create_index(
        "ix_project_agent_configs_aggregate_llm_config_id",
        "project_agent_configs", ["aggregate_llm_config_id"],
    )
    op.create_index(
        "ix_project_agent_configs_glossary_llm_config_id",
        "project_agent_configs", ["glossary_llm_config_id"],
    )
    op.create_index(
        "ix_model_ai_scheduler_config_glossary_llm_config_id",
        "model_ai_scheduler_config", ["glossary_llm_config_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_model_ai_scheduler_config_glossary_llm_config_id", "model_ai_scheduler_config")
    op.drop_index("ix_project_agent_configs_glossary_llm_config_id", "project_agent_configs")
    op.drop_index("ix_project_agent_configs_aggregate_llm_config_id", "project_agent_configs")

    op.drop_constraint("fk_model_ai_scheduler_config_glossary_llm_config", "model_ai_scheduler_config", type_="foreignkey")
    op.drop_constraint("fk_project_agent_configs_glossary_llm_config", "project_agent_configs", type_="foreignkey")
    op.drop_constraint("fk_project_agent_configs_aggregate_llm_config", "project_agent_configs", type_="foreignkey")

    op.drop_column("model_ai_scheduler_config", "glossary_llm_config_id")
    op.drop_column("project_agent_configs", "glossary_llm_config_id")
    op.drop_column("project_agent_configs", "aggregate_llm_config_id")
