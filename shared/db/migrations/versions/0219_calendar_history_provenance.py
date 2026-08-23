"""Persist server-owned provenance for reversible calendar auto-create history."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0219"
down_revision = "0218"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "calendar_history_provenance",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("token", sa.UUID(), nullable=False),
        sa.Column("model_id", sa.UUID(), nullable=False),
        sa.Column("data_source_id", sa.UUID(), nullable=False),
        sa.Column("calendar_id", sa.UUID(), nullable=True),
        sa.Column("physical_table", sa.String(length=512), nullable=False),
        sa.Column("generated_metadata", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["model_id"], ["models.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["data_source_id"], ["data_sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token"),
    )
    op.create_index("ix_calendar_history_provenance_token", "calendar_history_provenance", ["token"], unique=True)
    op.create_index("ix_calendar_history_provenance_model_id", "calendar_history_provenance", ["model_id"])
    op.create_index("ix_calendar_history_provenance_data_source_id", "calendar_history_provenance", ["data_source_id"])
    op.create_index("ix_calendar_history_provenance_calendar_id", "calendar_history_provenance", ["calendar_id"])


def downgrade() -> None:
    op.drop_index("ix_calendar_history_provenance_calendar_id", table_name="calendar_history_provenance")
    op.drop_index("ix_calendar_history_provenance_data_source_id", table_name="calendar_history_provenance")
    op.drop_index("ix_calendar_history_provenance_model_id", table_name="calendar_history_provenance")
    op.drop_index("ix_calendar_history_provenance_token", table_name="calendar_history_provenance")
    op.drop_table("calendar_history_provenance")
