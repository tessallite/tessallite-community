"""Add pinned_model_id to agent_conversations (per-conversation model pin).

Revision ID: 0148
Revises: 0147
Create Date: 2026-06-20
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0148"
down_revision = "0147"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    # Tenant-only table (created in 0049, lives in `{slug}_meta`). The
    # `tess_system` DB has no `agent_conversations`, so run this as a no-op
    # there — mirrors the guard in 0049.
    if "agent_conversations" not in table_names:
        return

    op.add_column(
        "agent_conversations",
        sa.Column(
            "pinned_model_id",
            UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_agent_conversations_pinned_model_id",
        "agent_conversations",
        ["pinned_model_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "agent_conversations" not in table_names:
        return

    op.drop_index(
        "ix_agent_conversations_pinned_model_id",
        table_name="agent_conversations",
    )
    op.drop_column("agent_conversations", "pinned_model_id")
