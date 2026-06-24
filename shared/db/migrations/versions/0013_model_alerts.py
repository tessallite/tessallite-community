"""Model alerts — dedup-aware event stream for model health signals.

Revision ID: 0013
Revises: 0012
Create Date: 2026-04-13

Introduces ``model_alerts``, a per-tenant event log that the model
health system reads to surface invalid semantic objects, refresh
failures, optimiser errors, and query-router fallbacks to the
modeler. Design notes:

- Deduplication is enforced at the database level via a partial
  unique index on ``(model_id, category, related_object_type,
  related_object_id)`` WHERE the alert is open (not resolved and not
  dismissed). Repeating the same condition is handled by the
  application layer incrementing ``occurrence_count`` on the matching
  row; resolved/dismissed alerts automatically drop out of the
  uniqueness constraint so new occurrences after a fix create a
  fresh row.
- A second partial index accelerates the common "give me all open
  alerts for a model" query used by the Model Health tab.
- Nullable ``related_object_id`` so model-wide alerts (e.g. "no
  tables") can be stored without a specific target.

All fields match the pydantic ``ModelAlertResponse`` shape used by
``shared.semantic.model_alerts``.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = :name"
        ),
        {"name": name},
    )
    return result.scalar() is not None


def upgrade() -> None:
    if _table_exists("model_alerts"):
        return
    op.create_table(
        "model_alerts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "model_id",
            UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("related_object_type", sa.String(32), nullable=True),
        sa.Column("related_object_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "occurrence_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_by", UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "idx_model_alerts_model_open",
        "model_alerts",
        ["model_id"],
        postgresql_where=sa.text("resolved_at IS NULL AND dismissed_at IS NULL"),
    )
    op.create_index(
        "idx_model_alerts_dedup",
        "model_alerts",
        ["model_id", "category", "related_object_type", "related_object_id"],
        unique=True,
        postgresql_where=sa.text("resolved_at IS NULL AND dismissed_at IS NULL"),
    )


def downgrade() -> None:
    if _table_exists("model_alerts"):
        op.drop_index("idx_model_alerts_dedup", table_name="model_alerts")
        op.drop_index("idx_model_alerts_model_open", table_name="model_alerts")
        op.drop_table("model_alerts")
