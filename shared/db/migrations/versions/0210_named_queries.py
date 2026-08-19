"""Named Queries — definition + artifact + refresh policy/runs tables.

Named Queries (governed, modeler-authored semantic queries served
materialised-first / source-fallback, referenced as ``SELECT * FROM @Name``):

* ``named_queries`` — the DEFINITION (model-bound logical SQL, output-column
  schema, shape, caps, certification). Uniqueness on
  ``(model_id, lower(name))`` — the ``@`` namespace is case-insensitive per
  model (Bug-7663 precedent); the cross-table uniqueness against
  ``model_parameters`` and ``named_sets`` is enforced at create time in the
  model-service.
* ``named_query_artifacts`` — the MATERIALISATION on the shared artifact
  substrate (physical table, row manifest, lifecycle status with the same
  CHECK as pockets, refresh liveness pointer, version binding).
* ``named_query_refresh_policies`` — 1:1 schedule policy (mirror
  ``pocket_refresh_policies``).
* ``named_query_refresh_runs`` — run history (mirror ``pocket_refresh_runs``).

This is a TENANT-chain migration: it revises 0209 (the physical-cleanup
tenant migration, which is the tenant head), NOT the system head (0207).
``alembic heads`` stays at exactly two heads (system 0207 + tenant 0210).

Revision ID: 0210
Revises: 0209
Create Date: 2026-08-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0210"
down_revision = "0209"
branch_labels = None
depends_on = None

_NAMED_QUERIES_STATUS_CHECK = "ck_named_query_artifacts_status"
_NAMED_QUERIES_STATUSES = ("fresh", "stale", "invalidating", "failed")


def _table_exists(table: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table in set(inspector.get_table_names())


def upgrade() -> None:
    op.create_table(
        "named_queries",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255)),
        sa.Column("description", sa.Text()),
        sa.Column("display_folder", sa.String(255)),
        sa.Column("definition_sql", sa.Text(), nullable=False),
        sa.Column("output_columns", JSONB()),
        sa.Column("shape", sa.String(16), nullable=False, server_default="projection"),
        sa.Column("row_cap", sa.Integer()),
        sa.Column("column_cap", sa.Integer()),
        sa.Column("certification_status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("created_by", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index(
        "uq_named_queries_model_lower_name",
        "named_queries",
        ["model_id", sa.text("lower(name)")],
        unique=True,
    )

    op.create_table(
        "named_query_refresh_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("named_query_id", UUID(as_uuid=True), sa.ForeignKey("named_queries.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("refresh_mode", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("rows_written", sa.BigInteger()),
        sa.Column("bytes_processed", sa.BigInteger()),
        sa.Column("error_message", sa.Text()),
        sa.Column("triggered_by", sa.String(32), nullable=False, server_default="scheduler"),
    )

    # named_query_artifacts references named_query_refresh_runs
    # (active_refresh_run_id, SET NULL), so the runs table must exist first.
    op.create_table(
        "named_query_artifacts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("named_query_id", UUID(as_uuid=True), sa.ForeignKey("named_queries.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("target_id", UUID(as_uuid=True), sa.ForeignKey("data_targets.id"), nullable=False, index=True),
        sa.Column("physical_table_name", sa.String(512), nullable=False),
        sa.Column("target_schema", sa.String(255)),
        sa.Column("row_manifest", JSONB()),
        sa.Column("row_count", sa.BigInteger()),
        sa.Column("status", sa.String(32), nullable=False, server_default="stale"),
        sa.Column("failure_reason", sa.Text()),
        sa.Column("active_refresh_run_id", UUID(as_uuid=True), sa.ForeignKey("named_query_refresh_runs.id", ondelete="SET NULL")),
        sa.Column("last_refresh_at", sa.DateTime(timezone=True)),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column("built_for_version_id", UUID(as_uuid=True)),
        sa.Column("built_for_epoch", sa.Integer()),
    )
    op.create_check_constraint(
        _NAMED_QUERIES_STATUS_CHECK,
        "named_query_artifacts",
        "status IN ('fresh', 'stale', 'invalidating', 'failed')",
    )

    op.create_table(
        "named_query_refresh_policies",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("named_query_id", UUID(as_uuid=True), sa.ForeignKey("named_queries.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("cron_expression", sa.String(128)),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    # named_query_artifacts.active_refresh_run_id references
    # named_query_refresh_runs, so drop the artifact table first.
    if _table_exists("named_query_artifacts"):
        op.drop_table("named_query_artifacts")
    if _table_exists("named_query_refresh_policies"):
        op.drop_table("named_query_refresh_policies")
    if _table_exists("named_query_refresh_runs"):
        op.drop_table("named_query_refresh_runs")
    if _table_exists("named_queries"):
        op.drop_table("named_queries")
