"""Add Solidatus and Collibra integration tables (F-030-05 / Enhancement-030-05).

Creates per-tenant metadata tables for:
  - solidatus_connections  — Solidatus target config (one per model)
  - solidatus_sync_runs    — push history
  - solidatus_object_mappings — Tessallite→Solidatus ID map for incremental sync

  - collibra_connections   — Collibra target config (one per model)
  - collibra_sync_runs     — push history
  - collibra_object_mappings — Tessallite→Collibra ID map for incremental sync

Both use the same encrypted_credentials BYTEA pattern as project_connections.
Connections are scoped to a model (model_id NOT NULL), with project_id
as a denormalised FK for simpler RBAC queries.
Tokens/credentials are never returned by the API.

Revision ID: 0143
Revises: 0142
Create Date: 2026-06-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0143"
down_revision = "0142"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Solidatus --------------------------------------------------------
    op.create_table(
        "solidatus_connections",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("base_url", sa.Text, nullable=False),
        sa.Column("auth_type", sa.String(32), nullable=False, server_default=sa.text("'bearer_token'")),
        sa.Column("encrypted_credentials", sa.LargeBinary, nullable=False),
        sa.Column("workspace_id", sa.Text, nullable=True),
        sa.Column("model_ref", sa.Text, nullable=True),
        sa.Column("sync_scope", sa.String(32), nullable=False, server_default=sa.text("'model'")),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "solidatus_sync_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("connection_id", UUID(as_uuid=True), sa.ForeignKey("solidatus_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=True),
        sa.Column("mode", sa.String(32), nullable=False),  # dry_run | push | validate
        sa.Column("status", sa.String(32), nullable=False),  # pending | running | succeeded | failed | partial
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("tessallite_snapshot_hash", sa.Text, nullable=True),
        sa.Column("solidatus_target_ref", sa.Text, nullable=True),
        sa.Column("nodes_total", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("edges_total", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("nodes_created", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("nodes_updated", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("edges_created", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("edges_updated", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("result_json", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )

    op.create_table(
        "solidatus_object_mappings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("connection_id", UUID(as_uuid=True), sa.ForeignKey("solidatus_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tessallite_object_type", sa.String(64), nullable=False),
        sa.Column("tessallite_object_id", sa.Text, nullable=False),
        sa.Column("tessallite_stable_key", sa.Text, nullable=False),
        sa.Column("solidatus_object_id", sa.Text, nullable=True),
        sa.Column("solidatus_object_ref", sa.Text, nullable=True),
        sa.Column("last_payload_hash", sa.Text, nullable=False),
        sa.Column("last_synced_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_sync_run_id", UUID(as_uuid=True), sa.ForeignKey("solidatus_sync_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("is_deprecated", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.UniqueConstraint("connection_id", "tessallite_object_type", "tessallite_object_id"),
    )

    # --- Collibra ---------------------------------------------------------
    op.create_table(
        "collibra_connections",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("base_url", sa.Text, nullable=False),
        sa.Column("auth_type", sa.String(32), nullable=False, server_default=sa.text("'bearer_token'")),
        sa.Column("encrypted_credentials", sa.LargeBinary, nullable=False),
        sa.Column("community_id", sa.Text, nullable=True),
        sa.Column("domain_id", sa.Text, nullable=True),
        sa.Column("asset_type_mapping", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("relation_type_mapping", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("responsibility_mapping", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("sync_scope", sa.String(32), nullable=False, server_default=sa.text("'model'")),
        sa.Column("sync_mode", sa.String(32), nullable=False, server_default=sa.text("'rest_api'")),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "collibra_sync_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("connection_id", UUID(as_uuid=True), sa.ForeignKey("collibra_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=True),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("tessallite_snapshot_hash", sa.Text, nullable=True),
        sa.Column("collibra_import_job_id", sa.Text, nullable=True),
        sa.Column("assets_total", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("relations_total", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("attributes_total", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("responsibilities_total", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("assets_created", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("assets_updated", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("relations_created", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("relations_updated", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("warnings_json", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("result_json", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error_message", sa.Text, nullable=True),
    )

    op.create_table(
        "collibra_object_mappings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("connection_id", UUID(as_uuid=True), sa.ForeignKey("collibra_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tessallite_object_type", sa.String(64), nullable=False),
        sa.Column("tessallite_object_id", sa.Text, nullable=False),
        sa.Column("tessallite_stable_key", sa.Text, nullable=False),
        sa.Column("collibra_resource_type", sa.String(32), nullable=False, server_default=sa.text("'asset'")),
        sa.Column("collibra_resource_id", sa.Text, nullable=True),
        sa.Column("collibra_full_name", sa.Text, nullable=True),
        sa.Column("last_payload_hash", sa.Text, nullable=False),
        sa.Column("last_synced_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_sync_run_id", UUID(as_uuid=True), sa.ForeignKey("collibra_sync_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("is_deprecated", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.UniqueConstraint("connection_id", "tessallite_object_type", "tessallite_object_id", "collibra_resource_type"),
    )

    # --- Indexes on FK columns -------------------------------------------
    # These mirror the index=True declarations on the ORM tables. Without
    # them, Alembic-migrated environments (staging/prod) would seq-scan the
    # filter/join columns used by load_mappings (every sync) and the
    # list/runs/mappings endpoints. Index names match SQLAlchemy's default
    # auto-naming (ix_<table>_<column>) so create_all and Alembic agree.
    op.create_index("ix_solidatus_connections_project_id", "solidatus_connections", ["project_id"])
    op.create_index("ix_solidatus_connections_model_id", "solidatus_connections", ["model_id"])
    op.create_index("ix_solidatus_sync_runs_connection_id", "solidatus_sync_runs", ["connection_id"])
    op.create_index("ix_solidatus_object_mappings_connection_id", "solidatus_object_mappings", ["connection_id"])
    op.create_index("ix_collibra_connections_project_id", "collibra_connections", ["project_id"])
    op.create_index("ix_collibra_connections_model_id", "collibra_connections", ["model_id"])
    op.create_index("ix_collibra_sync_runs_connection_id", "collibra_sync_runs", ["connection_id"])
    op.create_index("ix_collibra_object_mappings_connection_id", "collibra_object_mappings", ["connection_id"])


def downgrade() -> None:
    op.drop_index("ix_collibra_object_mappings_connection_id", table_name="collibra_object_mappings")
    op.drop_index("ix_collibra_sync_runs_connection_id", table_name="collibra_sync_runs")
    op.drop_index("ix_collibra_connections_model_id", table_name="collibra_connections")
    op.drop_index("ix_collibra_connections_project_id", table_name="collibra_connections")
    op.drop_index("ix_solidatus_object_mappings_connection_id", table_name="solidatus_object_mappings")
    op.drop_index("ix_solidatus_sync_runs_connection_id", table_name="solidatus_sync_runs")
    op.drop_index("ix_solidatus_connections_model_id", table_name="solidatus_connections")
    op.drop_index("ix_solidatus_connections_project_id", table_name="solidatus_connections")

    op.drop_table("collibra_object_mappings")
    op.drop_table("collibra_sync_runs")
    op.drop_table("collibra_connections")
    op.drop_table("solidatus_object_mappings")
    op.drop_table("solidatus_sync_runs")
    op.drop_table("solidatus_connections")
