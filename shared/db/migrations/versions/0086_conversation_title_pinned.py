"""Add title and pinned_at to agent_conversations.

Revision ID: 0086
Revises: 0085
"""
from alembic import op
import sqlalchemy as sa

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversations",
        sa.Column("title", sa.String(200), nullable=True),
    )
    op.add_column(
        "agent_conversations",
        sa.Column(
            "pinned_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_conversations", "pinned_at")
    op.drop_column("agent_conversations", "title")
