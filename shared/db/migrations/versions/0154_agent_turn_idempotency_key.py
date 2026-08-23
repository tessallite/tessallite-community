"""Add idempotency_key to agent_turns (Bug-6521 streaming-retry dedup).

A per-send idempotency key is stamped on the reserved turn placeholder so a
retried stream/sync POST carrying the same key dedupes to the existing turn
instead of reserving and executing a duplicate. The partial unique index makes
the dedup atomic (one turn per conversation+key) while leaving NULL keys
(keyless callers) exempt.

Revision ID: 0154
Revises: 0153
Create Date: 2026-07-06
"""
from alembic import op
import sqlalchemy as sa

revision = "0154"
down_revision = "0153"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    # Tenant-only table (created in 0049, lives in `{slug}_meta`). The
    # `tess_system` DB has no `agent_turns`, so run this as a no-op there —
    # mirrors the guard in 0148/0149.
    if "agent_turns" not in table_names:
        return

    op.add_column(
        "agent_turns",
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "uq_agent_turns_conversation_idempotency_key",
        "agent_turns",
        ["conversation_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "agent_turns" not in table_names:
        return

    op.drop_index(
        "uq_agent_turns_conversation_idempotency_key",
        table_name="agent_turns",
    )
    op.drop_column("agent_turns", "idempotency_key")
