"""Within-epoch ordering marker + durable post-deploy re-eval outbox (Bug-7982 R6).

Finding 1 (within-epoch write race): add ``kpi_latest.eval_started_at`` so the
upsert guard can order two same-epoch writers by (evaluated_for_epoch,
eval_started_at) instead of epoch alone — a later-STARTING evaluation can no
longer be clobbered by an earlier-starting one that merely commits after it.

Finding 6 (non-durable re-eval trigger): add the ``pending_kpi_reeval`` outbox
table. A deploy/revert writes a row here inside the same transaction as the
epoch bump; the scheduler sweep drains any row the in-process trigger did not
clear (process death / trigger failure), logging that the re-eval is overdue.

Tenant-schema guarded (skip when the schema has no ``kpi_latest`` table),
idempotent, reversible.

Revision ID: 0182
Revises: 0181
Create Date: 2026-07-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0182"
down_revision = "0181"
branch_labels = None
depends_on = None

_KPI_LATEST = "kpi_latest"
_OUTBOX = "pending_kpi_reeval"


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return any(c["name"] == column for c in sa.inspect(bind).get_columns(table))


def upgrade() -> None:
    # This migration only touches tenant schemas that carry the model tables.
    # Gate on kpi_latest so it is a no-op in the system schema.
    if not _table_exists(_KPI_LATEST):
        return
    if not _has_column(_KPI_LATEST, "eval_started_at"):
        op.add_column(
            _KPI_LATEST,
            sa.Column("eval_started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        )
    if not _table_exists(_OUTBOX):
        op.create_table(
            _OUTBOX,
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "model_id", UUID(as_uuid=True),
                sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column("project_id", UUID(as_uuid=True), nullable=False),
            sa.Column("requested_for_epoch", sa.Integer(), nullable=False),
            sa.Column(
                "requested_at", sa.TIMESTAMP(timezone=True),
                nullable=False, server_default=sa.func.now(),
            ),
            sa.UniqueConstraint("model_id", name="uq_pending_kpi_reeval_model"),
        )
        op.create_index(
            "ix_pending_kpi_reeval_model_id", _OUTBOX, ["model_id"],
        )


def downgrade() -> None:
    if _table_exists(_OUTBOX):
        op.drop_table(_OUTBOX)
    if _table_exists(_KPI_LATEST) and _has_column(_KPI_LATEST, "eval_started_at"):
        op.drop_column(_KPI_LATEST, "eval_started_at")
