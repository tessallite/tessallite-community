"""Create named_set_versions and kpi_versions tables for version history."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0102"
down_revision = "0101"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "named_set_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("named_set_id", UUID(as_uuid=True), sa.ForeignKey("named_sets.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("version_number", sa.Integer, nullable=False),
        sa.Column("changed_by", sa.String(255), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("change_summary", sa.Text, nullable=True),
        sa.Column("snapshot", JSONB, nullable=False),
        sa.UniqueConstraint("named_set_id", "version_number", name="uq_named_set_version"),
    )

    op.create_table(
        "kpi_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("kpi_id", UUID(as_uuid=True), sa.ForeignKey("kpis.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("version_number", sa.Integer, nullable=False),
        sa.Column("changed_by", sa.String(255), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("change_summary", sa.Text, nullable=True),
        sa.Column("snapshot", JSONB, nullable=False),
        sa.UniqueConstraint("kpi_id", "version_number", name="uq_kpi_version"),
    )


def downgrade() -> None:
    op.drop_table("kpi_versions")
    op.drop_table("named_set_versions")
