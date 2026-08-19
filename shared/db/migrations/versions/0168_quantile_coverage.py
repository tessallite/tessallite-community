"""Add the quantile_coverage table (Bug-6969/5891, spec §4.2).

Persists the versioned semantic identity of each materialised pNN aggregate
column (method continuous/discrete, order direction, exact fraction as text,
null policy, value type, build exactness). The query-router's proof engine
consumes this as the AUTHORITATIVE input that gates every stored-quantile serve
(Gap D / I8): a pNN column WITHOUT a coverage row is treated as
``exactness='unknown'`` and never served in exact mode.

Tenant-schema guarded (skip when the schema has no ``aggregate_columns`` table),
idempotent (skip when the table already exists), and reversible. Creates no
coverage rows — legacy pNN columns are backfilled later by a controlled
validator and remain unserved (``unknown``) until then, so this migration only
adds nullable structure and cannot fail on legacy data.

Revision ID: 0168
Revises: 0167
Create Date: 2026-07-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0168"
down_revision = "0167"
branch_labels = None
depends_on = None

_TABLE = "quantile_coverage"
_GUARD_TABLE = "aggregate_columns"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    # Tenant-schema guard: only per-tenant meta schemas carry aggregate_columns.
    if _GUARD_TABLE not in table_names:
        return
    if _TABLE in table_names:
        return
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "aggregate_column_id",
            UUID(as_uuid=True),
            sa.ForeignKey("aggregate_columns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "aggregate_definition_id",
            UUID(as_uuid=True),
            sa.ForeignKey("aggregate_definitions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "measure_id",
            UUID(as_uuid=True),
            sa.ForeignKey("measures.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("semantic_measure_name", sa.String(255), nullable=False),
        sa.Column("input_expression_fingerprint", sa.String(512), nullable=False),
        sa.Column("fraction", sa.String(64), nullable=False),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("order_direction", sa.String(4), nullable=False, server_default="asc"),
        sa.Column("null_policy", sa.String(16), nullable=False, server_default="ignore_nulls"),
        sa.Column("value_type", sa.String(64), nullable=True),
        # DB column is "value_collation": bare "collation" is a reserved keyword
        # in PostgreSQL and fails CREATE TABLE with a syntax error (Bug-7858).
        sa.Column("value_collation", sa.String(64), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("exactness", sa.String(24), nullable=False, server_default="unknown"),
        sa.Column("algorithm", sa.String(32), nullable=True),
        sa.Column("algorithm_version", sa.String(32), nullable=True),
        sa.Column("build_source_dialect", sa.String(32), nullable=True),
        sa.Column("build_model_version", sa.String(64), nullable=True),
        sa.Column(
            "refresh_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("aggregate_refresh_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("coverage_schema_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("aggregate_column_id", name="uq_quantile_coverage_column"),
        sa.CheckConstraint(
            "method IN ('continuous', 'discrete')",
            name="ck_quantile_coverage_method",
        ),
        sa.CheckConstraint(
            "order_direction IN ('asc', 'desc')",
            name="ck_quantile_coverage_direction",
        ),
        sa.CheckConstraint(
            "exactness IN ('exact', 'bounded_approximate', 'unknown')",
            name="ck_quantile_coverage_exactness",
        ),
        sa.CheckConstraint(
            "null_policy IN ('ignore_nulls', 'respect_nulls')",
            name="ck_quantile_coverage_null_policy",
        ),
        sa.CheckConstraint(
            r"fraction ~ '^[0-9]+(\.[0-9]+)?$'",
            name="ck_quantile_coverage_fraction_format",
        ),
    )
    op.create_index(
        "ix_quantile_coverage_aggregate_column_id", _TABLE, ["aggregate_column_id"]
    )
    op.create_index(
        "ix_quantile_coverage_aggregate_definition_id",
        _TABLE,
        ["aggregate_definition_id"],
    )
    op.create_index("ix_quantile_coverage_measure_id", _TABLE, ["measure_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    op.drop_index("ix_quantile_coverage_measure_id", table_name=_TABLE)
    op.drop_index(
        "ix_quantile_coverage_aggregate_definition_id", table_name=_TABLE
    )
    op.drop_index(
        "ix_quantile_coverage_aggregate_column_id", table_name=_TABLE
    )
    op.drop_table(_TABLE)
