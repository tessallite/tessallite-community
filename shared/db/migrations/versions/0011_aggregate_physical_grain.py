"""Aggregate grain physical-column mapping and invalid-reason.

Revision ID: 0011
Revises: 0010
Create Date: 2026-04-13

Adds two nullable columns to `aggregate_definitions`:

- `grain_physical_cols`: JSONB parallel array to `grain`. Each entry is the
  collision-resolved physical column name used in the aggregate's CTAS output.
  NULL on legacy rows; the refresh pipeline treats NULL as "fall back to the
  bare logical name" so existing aggregates keep working until re-created.

- `invalid_reason`: TEXT. Populated when the revalidation pass flips an
  aggregate's status to 'invalid' (e.g. a join or table used by the grain
  was deleted). Surfaced in the Diagnostics panel so modellers know why an
  aggregate stopped refreshing.

Columns are nullable and have no default so the migration is a no-op for
rows that already exist; the new semantics kick in when aggregates are
created or revalidated after deploy.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0011"
down_revision = "0010"
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
    if not _column_exists("aggregate_definitions", "grain_physical_cols"):
        op.add_column(
            "aggregate_definitions",
            sa.Column("grain_physical_cols", JSONB(), nullable=True),
        )
    if not _column_exists("aggregate_definitions", "invalid_reason"):
        op.add_column(
            "aggregate_definitions",
            sa.Column("invalid_reason", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    if _column_exists("aggregate_definitions", "invalid_reason"):
        op.drop_column("aggregate_definitions", "invalid_reason")
    if _column_exists("aggregate_definitions", "grain_physical_cols"):
        op.drop_column("aggregate_definitions", "grain_physical_cols")
