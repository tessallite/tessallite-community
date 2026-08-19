"""Bug-8034 — durable worker claim fields for AI optimiser runs.

Adds ``claimed_by`` and ``claimed_at`` columns to ``ai_optimizer_runs``
so the worker loop can atomically claim queued rows and the startup janitor
can distinguish worker-owned rows from legacy in-flight rows.

- ``claimed_by``: which worker process owns the in-flight run (NULL when
  queued/completed/failed).
- ``claimed_at``: UTC timestamp of when the row was claimed.

Both are nullable with no default — existing rows pre-date the worker.

Revision ID: 0200
Revises: 0199
Create Date: 2026-08-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0200"
down_revision: Union[str, None] = "0199"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "ai_optimizer_runs"


def _has_column(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_TABLE):
        return False
    return any(column["name"] == name for column in inspector.get_columns(_TABLE))


def upgrade() -> None:
    # Tenant-schema guarded and idempotent, matching the rest of this batch.
    if not sa.inspect(op.get_bind()).has_table(_TABLE):
        return
    if not _has_column("claimed_by"):
        op.add_column(
            _TABLE,
            sa.Column("claimed_by", sa.String(256), nullable=True),
        )
    if not _has_column("claimed_at"):
        op.add_column(
            _TABLE,
            sa.Column("claimed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        )


def downgrade() -> None:
    if _has_column("claimed_at"):
        op.drop_column(_TABLE, "claimed_at")
    if _has_column("claimed_by"):
        op.drop_column(_TABLE, "claimed_by")
