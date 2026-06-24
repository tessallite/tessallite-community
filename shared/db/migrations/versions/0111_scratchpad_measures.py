"""Create scratchpad_measures table for per-user ephemeral expressions."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0111"
down_revision = "0110"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scratchpad_measures",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("expression", sa.Text, nullable=False),
        sa.Column("data_type", sa.String(32), nullable=False, server_default="numeric"),
        sa.Column("format", sa.Text, nullable=True),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("model_id", "created_by", "name", name="uq_scratchpad_measure"),
    )


def downgrade() -> None:
    op.drop_table("scratchpad_measures")
