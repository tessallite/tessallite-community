"""Time-intelligence schema groundwork on hierarchies.

Revision ID: 0030
Revises: 0029
Create Date: 2026-04-22

Phase 1 (Option B) of work/competitive-analysis-lesson-learnt-action-plan.md.
Lays the schema that Phase 2 (time-intelligence DSL) will read from,
without generating any calculations yet.

Adds:
  hierarchy_levels.time_unit         text     nullable
  hierarchy_levels.allowed_time_calcs jsonb   nullable, default '[]'
  hierarchy_definitions.dimension_kind text   nullable

``time_unit`` token vocabulary (validated at the API boundary):
    year | half | quarter | month | week | day | hour | none

``allowed_time_calcs`` is a JSON array of token strings drawn from:
    lag | parallel_period | period_to_date | range | moving_window

``dimension_kind`` marks a hierarchy as ``time`` so Phase 2 generators
know which hierarchies are eligible. Other tokens reserved for future
use (``geo``, ``entity``).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0030"
down_revision = "0029"
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


def upgrade() -> None:
    if not _column_exists("hierarchy_levels", "time_unit"):
        op.add_column(
            "hierarchy_levels",
            sa.Column("time_unit", sa.Text(), nullable=True),
        )
    if not _column_exists("hierarchy_levels", "allowed_time_calcs"):
        op.add_column(
            "hierarchy_levels",
            sa.Column(
                "allowed_time_calcs",
                JSONB,
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
        )
    if not _column_exists("hierarchy_definitions", "dimension_kind"):
        op.add_column(
            "hierarchy_definitions",
            sa.Column("dimension_kind", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    if _column_exists("hierarchy_definitions", "dimension_kind"):
        op.drop_column("hierarchy_definitions", "dimension_kind")
    if _column_exists("hierarchy_levels", "allowed_time_calcs"):
        op.drop_column("hierarchy_levels", "allowed_time_calcs")
    if _column_exists("hierarchy_levels", "time_unit"):
        op.drop_column("hierarchy_levels", "time_unit")
