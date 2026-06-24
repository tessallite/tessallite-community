"""Admin-config restructure — re-scope ``llm_provider_configs`` to project.

Pre-restructure: tenant-level table, default selection via ``is_active``.
Post-restructure: project-scoped (``project_id NOT NULL`` FK to projects),
default selection moves to ``ProjectSetting('agent.llm_config_id')``,
``is_active`` column dropped, free-form ``config: jsonb`` added for
provider-specific extras (e.g. ``anthropic_api_version``).

Data migration:
- For every existing tenant row, duplicate it into every project in the
  tenant so no project loses access to a configured LLM.
- The previously-active row's id (per project, after duplication) is
  written to ``project_settings(key='agent.llm_config_id')`` as the
  seed default. Non-active duplicates land alongside it.

Tenant-only migration; the system schema has no ``projects`` table and
runs this as a no-op.

Revision ID: 0051
Revises: 0050
Create Date: 2026-04-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    # Tenant-only migration; system schema has no `projects`.
    if "projects" not in table_names or "llm_provider_configs" not in table_names:
        return

    columns = {c["name"] for c in inspector.get_columns("llm_provider_configs")}

    # ------------------------------------------------------------------
    # 1. Add ``project_id`` (nullable initially so we can backfill).
    # ------------------------------------------------------------------
    if "project_id" not in columns:
        op.add_column(
            "llm_provider_configs",
            sa.Column(
                "project_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
        )

    # ------------------------------------------------------------------
    # 2. Add ``config`` jsonb column for provider extras.
    # ------------------------------------------------------------------
    if "config" not in columns:
        op.add_column(
            "llm_provider_configs",
            sa.Column(
                "config",
                postgresql.JSONB,
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
        )

    # ------------------------------------------------------------------
    # 3. Backfill: duplicate each tenant-scoped row into every project.
    #    The first project gets the original row updated in place; the
    #    rest get fresh rows with new ids. Active rows seed
    #    ``project_settings(key='agent.llm_config_id')`` per project.
    # ------------------------------------------------------------------
    has_is_active = "is_active" in columns
    project_ids = [
        row[0]
        for row in bind.execute(sa.text("SELECT id FROM projects")).fetchall()
    ]

    if project_ids:
        # Existing rows that still need a project assignment.
        orphan_rows = bind.execute(
            sa.text(
                "SELECT id, provider, display_name, base_url, encrypted_api_key, "
                "model_name, max_tokens, temperature, timeout_seconds"
                + (", is_active" if has_is_active else "")
                + " FROM llm_provider_configs WHERE project_id IS NULL"
            )
        ).fetchall()

        for row in orphan_rows:
            row_dict = dict(row._mapping)
            original_id = row_dict.pop("id")
            was_active = bool(row_dict.pop("is_active", False))

            for idx, project_id in enumerate(project_ids):
                if idx == 0:
                    # Reuse the original row id for the first project.
                    bind.execute(
                        sa.text(
                            "UPDATE llm_provider_configs SET project_id = :pid "
                            "WHERE id = :rid"
                        ),
                        {"pid": project_id, "rid": original_id},
                    )
                    new_id = original_id
                else:
                    # Insert a duplicate into the next project.
                    insert_stmt = sa.text(
                        "INSERT INTO llm_provider_configs ("
                        "project_id, provider, display_name, base_url, "
                        "encrypted_api_key, model_name, max_tokens, "
                        "temperature, timeout_seconds, config) "
                        "VALUES (:pid, :provider, :display_name, :base_url, "
                        ":encrypted_api_key, :model_name, :max_tokens, "
                        ":temperature, :timeout_seconds, '{}'::jsonb) "
                        "RETURNING id"
                    )
                    new_id = bind.execute(
                        insert_stmt,
                        {
                            "pid": project_id,
                            "provider": row_dict["provider"],
                            "display_name": row_dict["display_name"],
                            "base_url": row_dict["base_url"],
                            "encrypted_api_key": row_dict["encrypted_api_key"],
                            "model_name": row_dict["model_name"],
                            "max_tokens": row_dict["max_tokens"],
                            "temperature": row_dict["temperature"],
                            "timeout_seconds": row_dict["timeout_seconds"],
                        },
                    ).scalar()

                # If this row was the tenant default, seed the project's
                # agent.llm_config_id setting to point at its duplicate.
                if was_active:
                    bind.execute(
                        sa.text(
                            "INSERT INTO project_settings "
                            "(project_id, key, value_json, updated_by) "
                            "VALUES (:pid, 'agent.llm_config_id', "
                            "to_jsonb(CAST(:val AS text)), 'migration-0051') "
                            "ON CONFLICT (project_id, key) DO NOTHING"
                        ),
                        {"pid": project_id, "val": str(new_id)},
                    )

    # ------------------------------------------------------------------
    # 4. Drop any rows that still have NULL project_id (no projects in
    #    the tenant — orphans). Then enforce NOT NULL + FK.
    # ------------------------------------------------------------------
    bind.execute(
        sa.text("DELETE FROM llm_provider_configs WHERE project_id IS NULL")
    )

    op.alter_column("llm_provider_configs", "project_id", nullable=False)

    fk_names = {fk["name"] for fk in inspector.get_foreign_keys("llm_provider_configs")}
    if "fk_llm_provider_configs_project_id" not in fk_names:
        op.create_foreign_key(
            "fk_llm_provider_configs_project_id",
            "llm_provider_configs",
            "projects",
            ["project_id"],
            ["id"],
            ondelete="CASCADE",
        )

    index_names = {idx["name"] for idx in inspector.get_indexes("llm_provider_configs")}
    if "ix_llm_provider_configs_project_id" not in index_names:
        op.create_index(
            "ix_llm_provider_configs_project_id",
            "llm_provider_configs",
            ["project_id"],
        )

    constraints = {
        c["name"] for c in inspector.get_unique_constraints("llm_provider_configs")
    }
    if "uq_llm_config_project_display_name" not in constraints:
        op.create_unique_constraint(
            "uq_llm_config_project_display_name",
            "llm_provider_configs",
            ["project_id", "display_name"],
        )

    # ------------------------------------------------------------------
    # 5. Drop ``is_active`` — replaced by per-project pickers.
    # ------------------------------------------------------------------
    if has_is_active:
        op.drop_column("llm_provider_configs", "is_active")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "llm_provider_configs" not in table_names:
        return

    columns = {c["name"] for c in inspector.get_columns("llm_provider_configs")}

    if "is_active" not in columns:
        op.add_column(
            "llm_provider_configs",
            sa.Column(
                "is_active", sa.Boolean,
                nullable=False, server_default=sa.text("false"),
            ),
        )

    constraints = {
        c["name"] for c in inspector.get_unique_constraints("llm_provider_configs")
    }
    if "uq_llm_config_project_display_name" in constraints:
        op.drop_constraint(
            "uq_llm_config_project_display_name",
            "llm_provider_configs",
            type_="unique",
        )

    index_names = {idx["name"] for idx in inspector.get_indexes("llm_provider_configs")}
    if "ix_llm_provider_configs_project_id" in index_names:
        op.drop_index(
            "ix_llm_provider_configs_project_id",
            table_name="llm_provider_configs",
        )

    fk_names = {fk["name"] for fk in inspector.get_foreign_keys("llm_provider_configs")}
    if "fk_llm_provider_configs_project_id" in fk_names:
        op.drop_constraint(
            "fk_llm_provider_configs_project_id",
            "llm_provider_configs",
            type_="foreignkey",
        )

    if "project_id" in columns:
        op.drop_column("llm_provider_configs", "project_id")

    if "config" in columns:
        op.drop_column("llm_provider_configs", "config")
