"""Calculated measures — per-measure aggregation mode.

Revision ID: 0034
Revises: 0033
Create Date: 2026-04-23

Phase 4A of the drill-through + calculated-members plan. Adds a single
column to ``measures``:

  * ``calc_agg_mode`` — ``expression_as_written`` or
    ``per_row_then_aggregate``. Null for non-calculated measures.

Also adds a supporting index on ``(model_id, measure_type)`` so catalog
queries that partition the measure list by type can hit an index rather
than filtering in memory.

Plan: ``work/phase-4-drill-through-and-calculated-members-action-plan.md``
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0034"
down_revision = "0033"
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


def upgrade() -> None:
    if not _column_exists("measures", "calc_agg_mode"):
        op.add_column(
            "measures",
            sa.Column("calc_agg_mode", sa.String(length=32), nullable=True),
        )
    if not _constraint_exists("measures", "measures_calc_agg_mode_values"):
        op.create_check_constraint(
            "measures_calc_agg_mode_values",
            "measures",
            "calc_agg_mode IS NULL "
            "OR calc_agg_mode IN ('expression_as_written', 'per_row_then_aggregate')",
        )
    if not _index_exists("ix_measures_model_id_measure_type"):
        op.create_index(
            "ix_measures_model_id_measure_type",
            "measures",
            ["model_id", "measure_type"],
        )


def downgrade() -> None:
    if _index_exists("ix_measures_model_id_measure_type"):
        op.drop_index("ix_measures_model_id_measure_type", table_name="measures")
    if _constraint_exists("measures", "measures_calc_agg_mode_values"):
        op.drop_constraint(
            "measures_calc_agg_mode_values", "measures", type_="check"
        )
    if _column_exists("measures", "calc_agg_mode"):
        op.drop_column("measures", "calc_agg_mode")
