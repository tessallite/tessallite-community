"""Add physical_table_purged_at to aggregate_definitions.

Retirement only flipped metadata; the materialised target table was left
behind forever (F-009-08, unbounded storage leak). The retired-table purge
sweep drops the physical table after a configurable grace period and stamps
this column so the sweep never re-attempts the DROP on subsequent runs.

Idempotent: guard the add/drop on the live schema (search_path is set to the
target schema by env.py).

Revision ID: 0134
Revises: 0133
Create Date: 2026-06-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0134"
down_revision = "0133"
branch_labels = None
depends_on = None

_TABLE = "aggregate_definitions"
_COLUMN = "physical_table_purged_at"


def _has_column() -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    try:
        return any(c["name"] == _COLUMN for c in insp.get_columns(_TABLE))
    except sa.exc.NoSuchTableError:
        return False


def upgrade() -> None:
    if not _has_column():
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.TIMESTAMP(timezone=True), nullable=True),
        )


def downgrade() -> None:
    if _has_column():
        op.drop_column(_TABLE, _COLUMN)
