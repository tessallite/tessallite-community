"""Add session_history_depth to project_agent_configs.

Revision ID: 0064
Revises: 0063
"""
from alembic import op
import sqlalchemy as sa

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project_agent_configs",
        sa.Column(
            "session_history_depth",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("20"),
        ),
    )


def downgrade() -> None:
    op.drop_column("project_agent_configs", "session_history_depth")
