"""Add user-defined attributes and refs in tenant schema.

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-05
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_defined_attributes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=False),
        sa.Column("table_id", UUID(as_uuid=True), sa.ForeignKey("model_tables.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("expression", sa.Text, nullable=False),
        sa.Column("output_data_type", sa.String(20), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("validated", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("validation_error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("model_id", "table_id", "name"),
    )

    op.create_table(
        "user_defined_attribute_column_refs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "attribute_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user_defined_attributes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("column_id", UUID(as_uuid=True), sa.ForeignKey("model_columns.id"), nullable=False),
        sa.UniqueConstraint("attribute_id", "column_id"),
    )

    op.add_column(
        "dimensions",
        sa.Column(
            "user_defined_attribute_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user_defined_attributes.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "measures",
        sa.Column(
            "user_defined_attribute_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user_defined_attributes.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("measures", "user_defined_attribute_id")
    op.drop_column("dimensions", "user_defined_attribute_id")
    op.drop_table("user_defined_attribute_column_refs")
    op.drop_table("user_defined_attributes")
