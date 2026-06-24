"""Add hierarchy metadata tables in tenant schema.

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-05
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hierarchy_definitions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("segment_config", JSONB),
        sa.Column("date_config", JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("type IN ('explicit', 'date_embedded', 'segment')", name="ck_hierarchy_definitions_type"),
        sa.UniqueConstraint("model_id", "name"),
    )
    op.create_index("idx_hierarchy_definitions_model", "hierarchy_definitions", ["model_id"], unique=False)

    op.create_table(
        "hierarchy_levels",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "hierarchy_id",
            UUID(as_uuid=True),
            sa.ForeignKey("hierarchy_definitions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("key_attribute_id", UUID(as_uuid=True), nullable=False),
        sa.Column("key_attribute_source", sa.String(30), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ck_hierarchy_levels_ordinal_nonnegative"),
        sa.CheckConstraint(
            "key_attribute_source IN ('physical_column', 'user_defined_attribute')",
            name="ck_hierarchy_levels_key_attribute_source",
        ),
        sa.UniqueConstraint("hierarchy_id", "name"),
        sa.UniqueConstraint("hierarchy_id", "ordinal"),
        sa.UniqueConstraint("hierarchy_id", "key_attribute_id"),
    )
    op.create_index("idx_hierarchy_levels_hierarchy", "hierarchy_levels", ["hierarchy_id"], unique=False)

    op.create_table(
        "hierarchy_level_attributes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "level_id",
            UUID(as_uuid=True),
            sa.ForeignKey("hierarchy_levels.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attribute_id", UUID(as_uuid=True), nullable=False),
        sa.Column("attribute_source", sa.String(30), nullable=False),
        sa.Column("role", sa.String(10), nullable=False),
        sa.CheckConstraint(
            "attribute_source IN ('physical_column', 'user_defined_attribute')",
            name="ck_hierarchy_level_attributes_attribute_source",
        ),
        sa.CheckConstraint("role IN ('display', 'filter')", name="ck_hierarchy_level_attributes_role"),
        sa.UniqueConstraint("level_id", "attribute_id"),
    )
    op.create_index("idx_hierarchy_level_attributes_level", "hierarchy_level_attributes", ["level_id"], unique=False)

    op.create_table(
        "hierarchy_measure_links",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "hierarchy_id",
            UUID(as_uuid=True),
            sa.ForeignKey("hierarchy_definitions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "measure_id",
            UUID(as_uuid=True),
            sa.ForeignKey("measures.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "leaf_fact_attribute_id",
            UUID(as_uuid=True),
            sa.ForeignKey("model_columns.id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("hierarchy_id", "measure_id"),
    )
    op.create_index("idx_hierarchy_measure_links_hierarchy", "hierarchy_measure_links", ["hierarchy_id"], unique=False)
    op.create_index("idx_hierarchy_measure_links_measure", "hierarchy_measure_links", ["measure_id"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_hierarchy_measure_links_measure", table_name="hierarchy_measure_links")
    op.drop_index("idx_hierarchy_measure_links_hierarchy", table_name="hierarchy_measure_links")
    op.drop_table("hierarchy_measure_links")

    op.drop_index("idx_hierarchy_level_attributes_level", table_name="hierarchy_level_attributes")
    op.drop_table("hierarchy_level_attributes")

    op.drop_index("idx_hierarchy_levels_hierarchy", table_name="hierarchy_levels")
    op.drop_table("hierarchy_levels")

    op.drop_index("idx_hierarchy_definitions_model", table_name="hierarchy_definitions")
    op.drop_table("hierarchy_definitions")
