"""Add include_stats opt-in flag to aggregate_definitions.

When set, the aggregate materialises dispersion-statistic columns
(STDDEV_POP/SAMP, VAR_POP/SAMP) for its numeric measures, which the
query-router can serve at exact grain. Mirrors include_quantiles.

Idempotent: the column may already exist on environments where it was added
out-of-band before this revision was recorded, so guard the add/drop on the
live schema (search_path is set to the target schema by env.py).

Revision ID: 0121
Revises: 0120
Create Date: 2026-06-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0121"
down_revision = "0120"
branch_labels = None
depends_on = None

_TABLE = "aggregate_definitions"
_COLUMN = "include_stats"


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
            sa.Column(
                _COLUMN,
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    if _has_column():
        op.drop_column(_TABLE, _COLUMN)
