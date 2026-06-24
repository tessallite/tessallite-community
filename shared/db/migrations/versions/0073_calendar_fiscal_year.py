"""Add fiscal_year_start_month to calendar_tables."""
from alembic import op
import sqlalchemy as sa

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "calendar_tables",
        sa.Column(
            "fiscal_year_start_month",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )


def downgrade() -> None:
    op.drop_column("calendar_tables", "fiscal_year_start_month")
