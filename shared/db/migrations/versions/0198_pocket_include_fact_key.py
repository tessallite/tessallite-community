"""Bug-8719 — add include_fact_key column to pocket_definitions.

When True the pocket's materialised output includes the fact table primary key
columns even though they are hidden in the model. Required for incremental
refresh (the row-key DELETE needs the PK to match rows). Auto-set when
incremental_column + incremental_lookback_hours are both configured and the
fact PK is hidden. Not user-facing — the Pocket drawer shows an informational
message instead of a checkbox.

Revision ID: 0198
Revises: 0197
Create Date: 2026-08-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0198"
down_revision = "0197"
branch_labels = None
depends_on = None

_TABLE = "pocket_definitions"
_COLUMN = "include_fact_key"


def _has_column(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_TABLE):
        return False
    return any(column["name"] == name for column in inspector.get_columns(_TABLE))


def upgrade() -> None:
    # Tenant-schema guarded and idempotent, matching the rest of this batch:
    # a schema without pocket_definitions is skipped, and a re-run after a
    # partially-applied upgrade does not fail on a duplicate column.
    if not sa.inspect(op.get_bind()).has_table(_TABLE):
        return
    if _has_column(_COLUMN):
        return
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    if _has_column(_COLUMN):
        op.drop_column(_TABLE, _COLUMN)
