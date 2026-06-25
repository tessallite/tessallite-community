"""Add calculation_steps JSONB column to agent_turns.

Stores the step-by-step calculation trace for multi-step (recipe) queries
so that it survives page reload and is available via the REST history
endpoint.

Revision ID: 0122
Revises: 0121
Create Date: 2026-06-09
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0122"
down_revision = "0121"
branch_labels = None
depends_on = None

_TABLE = "agent_turns"
_COLUMN = "calculation_steps"


def _has_column() -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = [c["name"] for c in insp.get_columns(_TABLE)]
    return _COLUMN in cols


def upgrade() -> None:
    if not _has_column():
        op.add_column(_TABLE, sa.Column(_COLUMN, JSONB, nullable=True))


def downgrade() -> None:
    if _has_column():
        op.drop_column(_TABLE, _COLUMN)
