"""Phase Agent-A1 — conversational agent foundation tables.

Per-tenant tables that back the project-scoped conversational agent
described in ``docs/architecture/architecture_conversational-agent.md``
and planned in ``docs/execution/execution_conversational-agent-plan.md``.

All eight new tables live in the per-tenant ``{slug}_meta`` schema —
agent state is per-tenant, per-project. The ``tess_system`` DB runs this
migration as a no-op (no ``projects`` table there).

Tables created:

- ``project_agent_configs``        — one row per project; master agent config.
- ``project_agent_models``         — allow-list of models per project agent.
- ``project_agent_model_contexts`` — per (project, model) prompt context block.
- ``agent_judge_rubrics``          — structured judge rubric (named sections).
- ``project_cross_model_recipes``  — ordered query steps + combine expression.
- ``model_alias_maps``             — per-model phrase -> canonical attribute jsonb.
- ``agent_conversations``          — conversation header per principal.
- ``agent_turns``                  — single-row-per-turn log (F2 shape).

The agent reuses the existing tenant-level ``llm_provider_configs`` for
both the answer LLM and the judge LLM (see G1 / B3 of the answered
questions doc). No new LLM config table is added.

Revision ID: 0049
Revises: 0048
Create Date: 2026-04-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    # Tenant-only migration; system schema has no `projects` table.
    if "projects" not in table_names:
        return

    # ------------------------------------------------------------------
    # 1. project_agent_configs
    # ------------------------------------------------------------------
    if "project_agent_configs" not in table_names:
        op.create_table(
            "project_agent_configs",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "project_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
            ),
            sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("false")),
            sa.Column("display_name", sa.String(255), nullable=True),
            sa.Column("project_brief", sa.Text, nullable=True),
            sa.Column(
                "agent_role",
                sa.Text,
                nullable=False,
                server_default=sa.text("'data analyst'"),
            ),
            sa.Column(
                "tone_preset",
                sa.String(32),
                nullable=False,
                server_default=sa.text("'professional'"),
            ),
            sa.Column("tone_overrides", sa.Text, nullable=True),
            sa.Column("brand_guidelines", sa.Text, nullable=True),
            sa.Column("safety_policy", sa.Text, nullable=True),
            sa.Column("content_rules", sa.Text, nullable=True),
            sa.Column("default_locale", sa.String(16), nullable=True),
            sa.Column("disclosure_text", sa.Text, nullable=True),
            sa.Column("webhook_url", sa.Text, nullable=True),
            sa.Column("webhook_signing_secret", sa.LargeBinary, nullable=True),
            sa.Column(
                "primary_model_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("models.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "answer_llm_config_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("llm_provider_configs.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "judge_llm_config_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("llm_provider_configs.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "judge_mode",
                sa.String(16),
                nullable=False,
                server_default=sa.text("'async'"),
            ),
            sa.Column(
                "judge_rubric_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
            sa.Column(
                "judge_block_visibility",
                sa.String(16),
                nullable=False,
                server_default=sa.text("'transparent'"),
            ),
            sa.Column(
                "show_thought_process",
                sa.Boolean,
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column(
                "show_semantic_query",
                sa.Boolean,
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column(
                "show_physical_query",
                sa.Boolean,
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column(
                "feedback_enabled",
                sa.Boolean,
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column(
                "created_at",
                postgresql.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                postgresql.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )
        op.create_index(
            "ix_project_agent_configs_project_id",
            "project_agent_configs",
            ["project_id"],
        )

    # ------------------------------------------------------------------
    # 2. project_agent_models  (allow-list)
    # ------------------------------------------------------------------
    if "project_agent_models" not in table_names:
        op.create_table(
            "project_agent_models",
            sa.Column(
                "project_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "model_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("models.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "added_at",
                postgresql.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )

    # ------------------------------------------------------------------
    # 3. project_agent_model_contexts
    # ------------------------------------------------------------------
    if "project_agent_model_contexts" not in table_names:
        op.create_table(
            "project_agent_model_contexts",
            sa.Column(
                "project_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "model_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("models.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("model_overview", sa.Text, nullable=True),
            sa.Column("analytical_capabilities", sa.Text, nullable=True),
            sa.Column("abbreviation_conflict_rules", sa.Text, nullable=True),
            sa.Column(
                "example_questions",
                postgresql.JSONB,
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column(
                "aggregates_summary",
                postgresql.JSONB,
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column(
                "calendar_aliases",
                postgresql.JSONB,
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column(
                "dimension_aliases",
                postgresql.JSONB,
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column(
                "derived_at",
                postgresql.TIMESTAMP(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "published_at",
                postgresql.TIMESTAMP(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "updated_at",
                postgresql.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )

    # ------------------------------------------------------------------
    # 4. agent_judge_rubrics
    # ------------------------------------------------------------------
    if "agent_judge_rubrics" not in table_names:
        op.create_table(
            "agent_judge_rubrics",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "project_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column(
                "sections",
                postgresql.JSONB,
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column(
                "created_at",
                postgresql.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                postgresql.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )
        op.create_index(
            "ix_agent_judge_rubrics_project_id",
            "agent_judge_rubrics",
            ["project_id"],
        )

        # Now that the rubric table exists, add the deferred FK from
        # project_agent_configs.judge_rubric_id -> agent_judge_rubrics.id.
        op.create_foreign_key(
            "fk_project_agent_configs_judge_rubric",
            "project_agent_configs",
            "agent_judge_rubrics",
            ["judge_rubric_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # ------------------------------------------------------------------
    # 5. project_cross_model_recipes
    # ROLLBACK CANDIDATE: this table can fold back into
    # project_agent_configs.cross_model_calculations: jsonb if maintenance
    # proves heavy — see D2 decision.
    # ------------------------------------------------------------------
    if "project_cross_model_recipes" not in table_names:
        op.create_table(
            "project_cross_model_recipes",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "project_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("description", sa.Text, nullable=True),
            sa.Column(
                "parameters",
                postgresql.JSONB,
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column(
                "steps",
                postgresql.JSONB,
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column("combine", sa.Text, nullable=False, server_default=sa.text("''")),
            sa.Column("notes", sa.Text, nullable=True),
            sa.Column(
                "created_at",
                postgresql.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                postgresql.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.UniqueConstraint(
                "project_id", "name", name="uq_cross_model_recipe_project_name"
            ),
        )
        op.create_index(
            "ix_project_cross_model_recipes_project_id",
            "project_cross_model_recipes",
            ["project_id"],
        )

    # ------------------------------------------------------------------
    # 6. model_alias_maps  (per-model phrase -> canonical attribute jsonb)
    # ------------------------------------------------------------------
    if "model_alias_maps" not in table_names:
        op.create_table(
            "model_alias_maps",
            sa.Column(
                "model_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("models.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "alias_map",
                postgresql.JSONB,
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column(
                "updated_at",
                postgresql.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )

    # ------------------------------------------------------------------
    # 7. agent_conversations
    # ------------------------------------------------------------------
    if "agent_conversations" not in table_names:
        op.create_table(
            "agent_conversations",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "project_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("caller_kind", sa.String(32), nullable=False),
            sa.Column("caller_ref", sa.String(255), nullable=False),
            sa.Column(
                "started_at",
                postgresql.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "last_active_at",
                postgresql.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "deleted_at",
                postgresql.TIMESTAMP(timezone=True),
                nullable=True,
            ),
        )
        op.create_index(
            "ix_agent_conversations_project_id",
            "agent_conversations",
            ["project_id"],
        )
        op.create_index(
            "ix_agent_conversations_caller",
            "agent_conversations",
            ["caller_kind", "caller_ref"],
        )

    # ------------------------------------------------------------------
    # 8. agent_turns  (single-row-per-turn log per F2)
    # ------------------------------------------------------------------
    if "agent_turns" not in table_names:
        op.create_table(
            "agent_turns",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "conversation_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("agent_conversations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("turn_index", sa.Integer, nullable=False),
            sa.Column("user_message", sa.Text, nullable=False),
            sa.Column("llm_plan", postgresql.JSONB, nullable=True),
            sa.Column("thought_summary", sa.Text, nullable=True),
            sa.Column("semantic_query", postgresql.JSONB, nullable=True),
            sa.Column(
                "recipe_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey(
                    "project_cross_model_recipes.id", ondelete="SET NULL"
                ),
                nullable=True,
            ),
            sa.Column(
                "recipe_steps_executed",
                postgresql.JSONB,
                nullable=True,
            ),
            sa.Column("routed_sql", sa.Text, nullable=True),
            sa.Column("route", sa.String(16), nullable=True),
            sa.Column("query_result_rows", sa.Integer, nullable=True),
            sa.Column("answer_text", sa.Text, nullable=True),
            sa.Column("citations", postgresql.JSONB, nullable=True),
            sa.Column(
                "guardrail_actions",
                postgresql.JSONB,
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column("judge_verdict", sa.String(32), nullable=True),
            sa.Column("judge_reasoning", sa.Text, nullable=True),
            sa.Column("judge_metrics", postgresql.JSONB, nullable=True),
            sa.Column("user_feedback", postgresql.JSONB, nullable=True),
            sa.Column(
                "usage_input_tokens",
                sa.Integer,
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "usage_output_tokens",
                sa.Integer,
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "latency_ms",
                sa.Integer,
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "status",
                sa.String(32),
                nullable=False,
                server_default=sa.text("'ok'"),
            ),
            sa.Column(
                "created_at",
                postgresql.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.UniqueConstraint(
                "conversation_id",
                "turn_index",
                name="uq_agent_turns_conversation_turn_index",
            ),
        )
        op.create_index(
            "ix_agent_turns_conversation_id",
            "agent_turns",
            ["conversation_id"],
        )
        op.create_index(
            "ix_agent_turns_created_at",
            "agent_turns",
            ["created_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "projects" not in table_names:
        return

    # Drop in reverse dependency order.
    if "agent_turns" in table_names:
        op.drop_table("agent_turns")
    if "agent_conversations" in table_names:
        op.drop_table("agent_conversations")
    if "model_alias_maps" in table_names:
        op.drop_table("model_alias_maps")
    if "project_cross_model_recipes" in table_names:
        op.drop_table("project_cross_model_recipes")

    # Drop the deferred FK before dropping the rubric table.
    if "project_agent_configs" in table_names:
        for fk in inspector.get_foreign_keys("project_agent_configs"):
            if fk.get("constrained_columns") == ["judge_rubric_id"]:
                op.drop_constraint(
                    fk["name"], "project_agent_configs", type_="foreignkey"
                )
                break

    if "agent_judge_rubrics" in table_names:
        op.drop_table("agent_judge_rubrics")
    if "project_agent_model_contexts" in table_names:
        op.drop_table("project_agent_model_contexts")
    if "project_agent_models" in table_names:
        op.drop_table("project_agent_models")
    if "project_agent_configs" in table_names:
        op.drop_table("project_agent_configs")
