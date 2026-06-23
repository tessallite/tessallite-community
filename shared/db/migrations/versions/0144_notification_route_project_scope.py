"""Scope notification routes to projects.

Revision ID: 0144
Revises: 0143
Create Date: 2026-06-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0144"
down_revision = "0143"
branch_labels = None
depends_on = None

_TABLE = "notification_routes"
_COLUMN = "project_id"
_INDEX = "ix_notification_routes_project_id"


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    return column_name in {
        col["name"] for col in sa.inspect(bind).get_columns(table_name)
    }


def upgrade() -> None:
    if not _column_exists(_TABLE, _COLUMN):
        op.add_column(
            _TABLE,
            sa.Column(
                _COLUMN,
                UUID(as_uuid=True),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )
    op.create_index(_INDEX, _TABLE, [_COLUMN], unique=False, if_not_exists=True)


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE, if_exists=True)
    if _column_exists(_TABLE, _COLUMN):
        op.drop_column(_TABLE, _COLUMN)
