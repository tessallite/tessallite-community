"""Add calendar_type and fiscal_year_start_month to hierarchy_definitions."""
from alembic import op
import sqlalchemy as sa

revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "hierarchy_definitions",
        sa.Column("calendar_type", sa.String(20), nullable=True),
    )
    op.add_column(
        "hierarchy_definitions",
        sa.Column("fiscal_year_start_month", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("hierarchy_definitions", "fiscal_year_start_month")
    op.drop_column("hierarchy_definitions", "calendar_type")
