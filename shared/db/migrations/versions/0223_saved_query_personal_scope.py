"""Add explicit personal/shared scope to saved queries.

New saved queries default to personal. Existing rows were historically visible
to every model viewer, so the migration backfills those rows as shared before
making the column non-null; an upgrade must not silently hide teammates'
existing library.

Revision ID: 0223
Revises: 0222
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0223"
down_revision = "0222"
branch_labels = None
depends_on = None

_TABLE = "saved_queries"
_COLUMN = "is_shared"


def _has_table() -> bool:
    return sa.inspect(op.get_bind()).has_table(_TABLE)


def _has_column() -> bool:
    try:
        return any(
            column["name"] == _COLUMN
            for column in sa.inspect(op.get_bind()).get_columns(_TABLE)
        )
    except sa.exc.NoSuchTableError:
        return False


def upgrade() -> None:
    if not _has_table():
        return
    if not _has_column():
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.Boolean(), nullable=True),
        )
    op.execute(
        sa.text(
            "UPDATE saved_queries SET is_shared = true "
            "WHERE is_shared IS NULL"
        )
    )
    op.alter_column(
        _TABLE,
        _COLUMN,
        existing_type=sa.Boolean(),
        nullable=False,
        server_default=sa.text("false"),
    )


def downgrade() -> None:
    if _has_table() and _has_column():
        op.drop_column(_TABLE, _COLUMN)
