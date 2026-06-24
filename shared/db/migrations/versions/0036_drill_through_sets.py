"""Drill-through sets — per-measure drill configuration.

Revision ID: 0036
Revises: 0035
Create Date: 2026-04-23

Phase 4C.1 of the drill-through + calculated-members plan. Adds a single
table ``drill_through_sets`` that stores (optional) curation knobs for
the drill-through query a user gets when they click a measure cell.

One row per measure for ``standard`` and ``variant`` measures; calculated
measures get no row (no single source fact). All columns except
``measure_id`` are nullable — null fields mean "apply the implicit
default" (all source-table columns, dimensions from the originating
query, paginated).

Plan: ``work/phase-4-drill-through-and-calculated-members-action-plan.md``
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0036"
down_revision = "0035"
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


def upgrade() -> None:
    if _table_exists("drill_through_sets"):
        return
    op.create_table(
        "drill_through_sets",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "measure_id",
            UUID(as_uuid=True),
            sa.ForeignKey("measures.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "source_table_id",
            UUID(as_uuid=True),
            sa.ForeignKey("model_tables.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("detail_columns", JSONB, nullable=True),
        sa.Column("joined_dimension_ids", JSONB, nullable=True),
        sa.Column("row_limit_override", sa.Integer, nullable=True),
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
    )
    op.create_index(
        "ix_drill_through_sets_measure_id",
        "drill_through_sets",
        ["measure_id"],
        unique=True,
    )


def downgrade() -> None:
    if not _table_exists("drill_through_sets"):
        return
    op.drop_index("ix_drill_through_sets_measure_id", table_name="drill_through_sets")
    op.drop_table("drill_through_sets")
