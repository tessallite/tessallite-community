"""Row security — dual-shape rules per model.

Revision ID: 0035
Revises: 0034
Create Date: 2026-04-23

Phase 5.1 — row security as a first-class object.

Adds ``row_security_rules`` with two shapes discriminated by ``rule_type``:

* ``role_predicate`` — sparse role-based filter, DSL expression +
  applies_to_roles[].
* ``user_mapping`` — dense per-user mapping to a ModelTable
  (mapping_table_id + user_column + value_column).

CHECK constraint enforces that the correct columns are populated for each
shape.

Plan: ``work/phase-5-row-security-and-live-polish-action-plan.md``
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


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


def _constraint_exists(table: str, constraint: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE table_schema = current_schema() "
            "AND table_name = :table AND constraint_name = :constraint"
        ),
        {"table": table, "constraint": constraint},
    )
    return result.scalar() is not None


def _index_exists(index: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM pg_indexes "
            "WHERE schemaname = current_schema() AND indexname = :index"
        ),
        {"index": index},
    )
    return result.scalar() is not None


def upgrade() -> None:
    if not _table_exists("row_security_rules"):
        op.create_table(
            "row_security_rules",
            sa.Column(
                "id",
                UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "model_id",
                UUID(as_uuid=True),
                sa.ForeignKey("models.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("dimension_path", sa.String(length=255), nullable=False),
            sa.Column("rule_type", sa.String(length=16), nullable=False),
            sa.Column("predicate_expression", sa.Text(), nullable=True),
            sa.Column("applies_to_roles", JSONB(), nullable=True),
            sa.Column(
                "mapping_table_id",
                UUID(as_uuid=True),
                sa.ForeignKey("model_tables.id", ondelete="RESTRICT"),
                nullable=True,
            ),
            sa.Column("mapping_user_column", sa.String(length=255), nullable=True),
            sa.Column("mapping_value_column", sa.String(length=255), nullable=True),
            sa.Column(
                "is_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.UniqueConstraint("model_id", "name", name="uq_row_security_rules_model_name"),
        )

    if not _constraint_exists("row_security_rules", "row_security_rules_shape_check"):
        op.create_check_constraint(
            "row_security_rules_shape_check",
            "row_security_rules",
            "("
            "rule_type = 'role_predicate' "
            "AND predicate_expression IS NOT NULL "
            "AND applies_to_roles IS NOT NULL "
            "AND mapping_table_id IS NULL "
            "AND mapping_user_column IS NULL "
            "AND mapping_value_column IS NULL"
            ") OR ("
            "rule_type = 'user_mapping' "
            "AND predicate_expression IS NULL "
            "AND applies_to_roles IS NULL "
            "AND mapping_table_id IS NOT NULL "
            "AND mapping_user_column IS NOT NULL "
            "AND mapping_value_column IS NOT NULL"
            ")",
        )

    if not _index_exists("ix_row_security_rules_model_id"):
        op.create_index(
            "ix_row_security_rules_model_id",
            "row_security_rules",
            ["model_id"],
        )


def downgrade() -> None:
    if _index_exists("ix_row_security_rules_model_id"):
        op.drop_index(
            "ix_row_security_rules_model_id", table_name="row_security_rules"
        )
    if _constraint_exists("row_security_rules", "row_security_rules_shape_check"):
        op.drop_constraint(
            "row_security_rules_shape_check",
            "row_security_rules",
            type_="check",
        )
    if _table_exists("row_security_rules"):
        op.drop_table("row_security_rules")
