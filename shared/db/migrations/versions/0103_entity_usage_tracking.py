"""Create named_set_usage and kpi_usage tables for workbook tracking."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0103"
down_revision = "0102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "named_set_usage",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("named_set_id", UUID(as_uuid=True), sa.ForeignKey("named_sets.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("workbook_id", sa.String(255), nullable=True),
        sa.Column("worksheet", sa.String(255), nullable=True),
        sa.Column("cell_reference", sa.String(64), nullable=True),
        sa.Column("usage_type", sa.String(32), nullable=False),
        sa.Column("reported_by", sa.String(255), nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "kpi_usage",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("kpi_id", UUID(as_uuid=True), sa.ForeignKey("kpis.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("workbook_id", sa.String(255), nullable=True),
        sa.Column("worksheet", sa.String(255), nullable=True),
        sa.Column("cell_reference", sa.String(64), nullable=True),
        sa.Column("usage_type", sa.String(32), nullable=False),
        sa.Column("reported_by", sa.String(255), nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("kpi_usage")
    op.drop_table("named_set_usage")
