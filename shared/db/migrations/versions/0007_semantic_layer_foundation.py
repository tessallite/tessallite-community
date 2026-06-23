"""Semantic layer foundation — descriptions, display names, hidden columns.

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-13

Phase 0 of the semantic-layer plan (docs/architecture/architecture_semantic-layer.md). Adds the
metadata fields the gateway, glossary, and Excel-facing catalog work in
later phases will surface to non-technical business users.

New columns:
  - dimensions.description     (TEXT, NULL)
  - measures.description       (TEXT, NULL)
  - model_tables.description   (TEXT, NULL)
  - model_columns.description  (TEXT, NULL)
  - model_columns.display_name (VARCHAR(255), NULL)
  - model_columns.is_hidden    (BOOLEAN, NOT NULL, DEFAULT FALSE)

The visibility cascade lives at the ModelColumn level only — Dimension and
Measure inherit visibility by walking back to their source column. See
Section 4.1 of the semantic-layer plan.
"""
from alembic import op
import sqlalchemy as sa


revision = "0007"
down_revision = "0006"
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
    if not _column_exists("dimensions", "description"):
        op.add_column("dimensions", sa.Column("description", sa.Text(), nullable=True))
    if not _column_exists("measures", "description"):
        op.add_column("measures", sa.Column("description", sa.Text(), nullable=True))
    if not _column_exists("model_tables", "description"):
        op.add_column("model_tables", sa.Column("description", sa.Text(), nullable=True))
    if not _column_exists("model_columns", "description"):
        op.add_column("model_columns", sa.Column("description", sa.Text(), nullable=True))
    if not _column_exists("model_columns", "display_name"):
        op.add_column(
            "model_columns",
            sa.Column("display_name", sa.String(length=255), nullable=True),
        )
    if not _column_exists("model_columns", "is_hidden"):
        op.add_column(
            "model_columns",
            sa.Column(
                "is_hidden",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )


def downgrade() -> None:
    if _column_exists("model_columns", "is_hidden"):
        op.drop_column("model_columns", "is_hidden")
    if _column_exists("model_columns", "display_name"):
        op.drop_column("model_columns", "display_name")
    if _column_exists("model_columns", "description"):
        op.drop_column("model_columns", "description")
    if _column_exists("model_tables", "description"):
        op.drop_column("model_tables", "description")
    if _column_exists("measures", "description"):
        op.drop_column("measures", "description")
    if _column_exists("dimensions", "description"):
        op.drop_column("dimensions", "description")
