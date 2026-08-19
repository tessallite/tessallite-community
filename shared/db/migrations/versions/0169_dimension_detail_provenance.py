"""Add detail-of provenance columns to dimensions table.

Supports the dimension detail attributes (bijection) authoring feature:
when a modeller declares a bijection detail attribute, the system auto-creates
a Dimension list entry for the detail column and marks it with provenance
fields pointing back to the relationship and owning dimension. These two
nullable FK columns enable the referential lock (cannot delete a detail-of
dimension independently) and the provenance chip in the UI.

Tenant-schema guarded (skip when the schema has no ``dimensions`` table),
idempotent (skip when the columns already exist), and reversible.

Revision ID: 0169
Revises: 0168
Create Date: 2026-07-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0169"
down_revision = "0168"
branch_labels = None
depends_on = None


def _table_exists(conn, table_name: str) -> bool:
    result = conn.execute(
        sa.text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM information_schema.tables"
            "  WHERE table_schema = current_schema()"
            "    AND table_name = :t"
            ")"
        ),
        {"t": table_name},
    )
    return result.scalar()


def _column_exists(conn, table_name: str, column_name: str) -> bool:
    result = conn.execute(
        sa.text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM information_schema.columns"
            "  WHERE table_schema = current_schema()"
            "    AND table_name = :t"
            "    AND column_name = :c"
            ")"
        ),
        {"t": table_name, "c": column_name},
    )
    return result.scalar()


def upgrade() -> None:
    conn = op.get_bind()

    # Tenant-schema guard: skip when the schema has no dimensions table.
    if not _table_exists(conn, "dimensions"):
        return

    # Idempotent: skip when the columns already exist.
    if not _column_exists(conn, "dimensions", "detail_of_relationship_id"):
        op.add_column(
            "dimensions",
            sa.Column(
                "detail_of_relationship_id",
                UUID(as_uuid=True),
                sa.ForeignKey(
                    "dimension_attribute_relationships.id",
                    ondelete="SET NULL",
                ),
                nullable=True,
            ),
        )
        op.create_index(
            "ix_dimensions_detail_of_relationship_id",
            "dimensions",
            ["detail_of_relationship_id"],
        )

    if not _column_exists(conn, "dimensions", "detail_of_dimension_id"):
        op.add_column(
            "dimensions",
            sa.Column(
                "detail_of_dimension_id",
                UUID(as_uuid=True),
                sa.ForeignKey("dimensions.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.create_index(
            "ix_dimensions_detail_of_dimension_id",
            "dimensions",
            ["detail_of_dimension_id"],
        )


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, "dimensions"):
        return

    if _column_exists(conn, "dimensions", "detail_of_dimension_id"):
        op.drop_index("ix_dimensions_detail_of_dimension_id", table_name="dimensions")
        op.drop_column("dimensions", "detail_of_dimension_id")

    if _column_exists(conn, "dimensions", "detail_of_relationship_id"):
        op.drop_index(
            "ix_dimensions_detail_of_relationship_id", table_name="dimensions"
        )
        op.drop_column("dimensions", "detail_of_relationship_id")
