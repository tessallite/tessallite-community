"""Create refresh_dependencies table for scheduler dependency chains."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0108"
down_revision = "0107"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "refresh_dependencies",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("upstream_aggregate_id", UUID(as_uuid=True), sa.ForeignKey("aggregate_definitions.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("downstream_aggregate_id", UUID(as_uuid=True), sa.ForeignKey("aggregate_definitions.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("upstream_aggregate_id", "downstream_aggregate_id", name="uq_refresh_dep"),
    )


def downgrade() -> None:
    op.drop_table("refresh_dependencies")
