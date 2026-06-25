"""Add max_compound_steps to project_agent_configs.

Revision ID: 0085
Revises: 0084
"""
from alembic import op
import sqlalchemy as sa

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project_agent_configs",
        sa.Column(
            "max_compound_steps",
            sa.Integer,
            server_default="3",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("project_agent_configs", "max_compound_steps")
