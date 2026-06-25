"""Drop hierarchy_measure_links table.

The link table was redundant — calendar type resolution now uses the time
dimension's hierarchy directly.  All dependent code (model-service API,
query-router rewriter, gateway XMLA, frontend, snapshot pipeline) has
been updated to operate without it.
"""
from alembic import op
import sqlalchemy as sa

revision = "0092"
down_revision = "0091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "idx_hierarchy_measure_links_measure",
        table_name="hierarchy_measure_links",
        if_exists=True,
    )
    op.drop_index(
        "idx_hierarchy_measure_links_hierarchy",
        table_name="hierarchy_measure_links",
        if_exists=True,
    )
    op.drop_table("hierarchy_measure_links")


def downgrade() -> None:
    op.create_table(
        "hierarchy_measure_links",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("hierarchy_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("measure_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("leaf_fact_attribute_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["hierarchy_id"], ["hierarchy_definitions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["measure_id"], ["measures.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["leaf_fact_attribute_id"], ["model_columns.id"]),
        sa.UniqueConstraint("hierarchy_id", "measure_id"),
    )
    op.create_index(
        "idx_hierarchy_measure_links_hierarchy",
        "hierarchy_measure_links",
        ["hierarchy_id"],
    )
    op.create_index(
        "idx_hierarchy_measure_links_measure",
        "hierarchy_measure_links",
        ["measure_id"],
    )
