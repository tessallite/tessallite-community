"""Add pocket_id FK to data_quality_violations."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "data_quality_violations",
        sa.Column("pocket_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_dq_violation_pocket",
        "data_quality_violations",
        "pocket_definitions",
        ["pocket_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_dq_violation_pocket_id",
        "data_quality_violations",
        ["pocket_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_dq_violation_pocket_id")
    op.drop_constraint("fk_dq_violation_pocket", "data_quality_violations")
    op.drop_column("data_quality_violations", "pocket_id")
