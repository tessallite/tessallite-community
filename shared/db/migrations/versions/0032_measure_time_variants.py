"""Per-measure time-variant configuration.

Revision ID: 0032
Revises: 0031
Create Date: 2026-04-22

Phase 2 Step 1 of work/phase-2-time-intelligence-action-plan.md.

Decisions (open-questions Q2, Q3, Q5):
  - Q2: a single boolean ``time_variants_enabled`` opts the measure into
    the full standard variant set (PY, PQ, PM, PW, YTD, QTD, MTD, WTD,
    YTD_PY, YoY_growth, YoY_growth_pct, trailing_N, moving_avg_N).
    Variants whose linked hierarchy lacks the matching unit are silently
    dropped from the catalog rather than producing a query-time error.
  - Q3: ``trailing_n`` and ``moving_avg_n`` are configured per measure;
    system defaults (12 and 30) apply when the columns are NULL.
  - Q5: variants are expanded into the published catalog, so once
    ``time_variants_enabled = true`` the catalog endpoint surfaces
    ``<measure>_<variant>`` rows that BI tools see directly.

Adds:
  measures.time_variants_enabled  boolean  not null default false
  measures.trailing_n             integer  nullable
  measures.moving_avg_n           integer  nullable
"""
from alembic import op
import sqlalchemy as sa


revision = "0032"
down_revision = "0031"
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
    if not _column_exists("measures", "time_variants_enabled"):
        op.add_column(
            "measures",
            sa.Column(
                "time_variants_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
    if not _column_exists("measures", "trailing_n"):
        op.add_column(
            "measures",
            sa.Column("trailing_n", sa.Integer(), nullable=True),
        )
    if not _column_exists("measures", "moving_avg_n"):
        op.add_column(
            "measures",
            sa.Column("moving_avg_n", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    if _column_exists("measures", "moving_avg_n"):
        op.drop_column("measures", "moving_avg_n")
    if _column_exists("measures", "trailing_n"):
        op.drop_column("measures", "trailing_n")
    if _column_exists("measures", "time_variants_enabled"):
        op.drop_column("measures", "time_variants_enabled")
