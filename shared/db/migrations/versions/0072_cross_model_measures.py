"""Add cross_model_source_model_id / measure_id to measures."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("measures", sa.Column(
        "cross_model_source_model_id",
        PG_UUID(as_uuid=True),
        nullable=True,
    ))
    op.add_column("measures", sa.Column(
        "cross_model_source_measure_id",
        PG_UUID(as_uuid=True),
        nullable=True,
    ))


def downgrade() -> None:
    op.drop_column("measures", "cross_model_source_model_id")
    op.drop_column("measures", "cross_model_source_measure_id")
