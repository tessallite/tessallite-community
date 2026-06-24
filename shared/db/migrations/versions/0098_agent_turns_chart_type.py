"""Add chart_type column to agent_turns."""
from alembic import op
import sqlalchemy as sa

revision = "0098"
down_revision = "0097"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_turns",
        sa.Column("chart_type", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_turns", "chart_type")
