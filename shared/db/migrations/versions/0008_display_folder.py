"""Display folder support for dimensions and measures.

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-13

Phase 1 of the semantic-layer plan (docs/architecture/architecture_semantic-layer.md). Adds an
optional folder hint that the XMLA gateway exposes as
MEASURE_DISPLAY_FOLDER / DIMENSION_DISPLAY_FOLDER, so Excel pivot field
lists render measures and dimensions inside expandable groups instead of
one flat list.

New columns:
  - dimensions.display_folder (VARCHAR(255), NULL)
  - measures.display_folder   (VARCHAR(255), NULL)
"""
from alembic import op
import sqlalchemy as sa


revision = "0008"
down_revision = "0007"
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
    if not _column_exists("dimensions", "display_folder"):
        op.add_column(
            "dimensions",
            sa.Column("display_folder", sa.String(length=255), nullable=True),
        )
    if not _column_exists("measures", "display_folder"):
        op.add_column(
            "measures",
            sa.Column("display_folder", sa.String(length=255), nullable=True),
        )


def downgrade() -> None:
    if _column_exists("measures", "display_folder"):
        op.drop_column("measures", "display_folder")
    if _column_exists("dimensions", "display_folder"):
        op.drop_column("dimensions", "display_folder")
