"""Add query_result_sample JSONB column to agent_turns.

Stores up to 200 result rows so that the frontend can render ECharts
charts on page reload without falling back to the backend-rendered HTML.

Revision ID: 0123
Revises: 0122
Create Date: 2026-06-09
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0123"
down_revision = "0122"
branch_labels = None
depends_on = None

_TABLE = "agent_turns"
_COLUMN = "query_result_sample"


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
