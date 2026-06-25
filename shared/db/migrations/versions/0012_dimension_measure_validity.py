"""Dimension + measure structural-validity flags.

Revision ID: 0012
Revises: 0011
Create Date: 2026-04-13

Adds ``is_invalid`` + ``invalid_reason`` to ``dimensions`` and
``measures`` so the revalidation pass (Phase 1 of the model-health
plan) can flag semantic objects whose source column or source table
has become structurally unreachable after a join/table/column
deletion.

Mirrors the shape already in place for ``aggregate_definitions``
(see migration 0011). Flags are nullable/falsy on legacy rows so
existing data keeps working; revalidation kicks in on the next
structural edit.
"""
from alembic import op
import sqlalchemy as sa


revision = "0012"
down_revision = "0011"
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
    if not _column_exists("dimensions", "is_invalid"):
        op.add_column(
            "dimensions",
            sa.Column(
                "is_invalid",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
    if not _column_exists("dimensions", "invalid_reason"):
        op.add_column(
            "dimensions",
            sa.Column("invalid_reason", sa.Text(), nullable=True),
        )
    if not _column_exists("measures", "is_invalid"):
        op.add_column(
            "measures",
            sa.Column(
                "is_invalid",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
    if not _column_exists("measures", "invalid_reason"):
        op.add_column(
            "measures",
            sa.Column("invalid_reason", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    if _column_exists("measures", "invalid_reason"):
        op.drop_column("measures", "invalid_reason")
    if _column_exists("measures", "is_invalid"):
        op.drop_column("measures", "is_invalid")
    if _column_exists("dimensions", "invalid_reason"):
        op.drop_column("dimensions", "invalid_reason")
    if _column_exists("dimensions", "is_invalid"):
        op.drop_column("dimensions", "is_invalid")
