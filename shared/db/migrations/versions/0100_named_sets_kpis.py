"""Add named_sets and kpis tables."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0100"
down_revision = "0099"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "named_sets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("display_folder", sa.String(255), nullable=True),
        sa.Column("scope", sa.Integer, nullable=False, server_default="1"),
        sa.Column("expression", sa.Text, nullable=False),
        sa.Column("dimensions", sa.Text, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("model_id", "name"),
    )

    op.create_table(
        "kpis",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("display_folder", sa.String(255), nullable=True),
        sa.Column("value_measure_id", UUID(as_uuid=True), sa.ForeignKey("measures.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("goal_measure_id", UUID(as_uuid=True), sa.ForeignKey("measures.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("status_expression", sa.Text, nullable=True),
        sa.Column("trend_expression", sa.Text, nullable=True),
        sa.Column("status_graphic", sa.String(64), nullable=False, server_default="Traffic Light"),
        sa.Column("trend_graphic", sa.String(64), nullable=False, server_default="Standard Arrow"),
        sa.Column("weight", sa.Float, nullable=True),
        sa.Column("parent_kpi_id", UUID(as_uuid=True), sa.ForeignKey("kpis.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("model_id", "name"),
    )


def downgrade() -> None:
    op.drop_table("kpis")
    op.drop_table("named_sets")
