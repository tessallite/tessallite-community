"""Move agent settings out of project_settings onto ProjectAgentConfig.

After 0052, the agent.* keys in `project_settings` are orphaned: the
registry no longer carries any agent.* SettingDef. The drawer's seven
agent tabs edit `project_agent_configs` directly via the agent-config
endpoint, the same way the Connections and LLM Configurations tabs edit
their own dedicated tables.

This migration:
  1. Adds `conversation_retention_days` (int, default 30, NOT NULL) to
     `project_agent_configs`. This is the only agent.* key that wasn't
     already a column on the table.
  2. Copies `agent.conversation_retention_days` from `project_settings`
     into the new column for every project that had one.
  3. Deletes every `project_settings` row whose key starts with
     `agent.` — they would be orphans the resolver could never surface.

Revision ID: 0053
Revises: 0052
Create Date: 2026-04-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def _table_exists(inspector, name: str) -> bool:
    return name in set(inspector.get_table_names())


def _column_exists(inspector, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # System DB has neither table — skip cleanly.
    if not _table_exists(inspector, "project_agent_configs"):
        # Tenant DBs without the agent tables (older provisioning paths)
        # also skip; cleanup of orphan settings still applies.
        if _table_exists(inspector, "project_settings"):
            bind.execute(
                sa.text("DELETE FROM project_settings WHERE key LIKE 'agent.%'")
            )
        return

    # 1. Add the retention column if not already present.
    if not _column_exists(inspector, "project_agent_configs", "conversation_retention_days"):
        op.add_column(
            "project_agent_configs",
            sa.Column(
                "conversation_retention_days",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("30"),
            ),
        )

    # 2. Migrate any stored values from project_settings.
    if _table_exists(inspector, "project_settings"):
        bind.execute(
            sa.text(
                """
                UPDATE project_agent_configs AS pac
                SET conversation_retention_days = COALESCE(
                    NULLIF(REGEXP_REPLACE(ps.value_json::text, '[^0-9]', '', 'g'), '')::int,
                    pac.conversation_retention_days
                )
                FROM project_settings AS ps
                WHERE ps.project_id = pac.project_id
                  AND ps.key = 'agent.conversation_retention_days'
                """
            )
        )

        # 3. Drop every orphan agent.* row.
        bind.execute(
            sa.text("DELETE FROM project_settings WHERE key LIKE 'agent.%'")
        )


def downgrade() -> None:
    """Reverse: drop the retention column. Orphan rows are not restored."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _table_exists(inspector, "project_agent_configs") and _column_exists(
        inspector, "project_agent_configs", "conversation_retention_days"
    ):
        op.drop_column("project_agent_configs", "conversation_retention_days")
