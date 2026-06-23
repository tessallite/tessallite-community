"""Add format column to measures.

Revision ID: 0029
Revises: 0028
Create Date: 2026-04-22

Phase 1 of work/competitive-analysis-lesson-learnt-action-plan.md.
Adds a nullable Text ``format`` column to ``measures`` so the modeller
can pin a presentation format token (currency, percent, decimal_2dp,
etc.) on each measure. The frontend formats client-side; the backend
only stores and returns the token.

Tokens are validated by the API layer, not the DB, so the column is
free-form Text.
"""
from alembic import op
import sqlalchemy as sa


revision = "0029"
down_revision = "0028"
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
    if not _column_exists("measures", "format"):
        op.add_column("measures", sa.Column("format", sa.Text(), nullable=True))


def downgrade() -> None:
    if _column_exists("measures", "format"):
        op.drop_column("measures", "format")
