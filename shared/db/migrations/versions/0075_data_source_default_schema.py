"""Add default_schema to data_sources."""
from alembic import op
import sqlalchemy as sa

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "data_sources",
        sa.Column("default_schema", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("data_sources", "default_schema")
