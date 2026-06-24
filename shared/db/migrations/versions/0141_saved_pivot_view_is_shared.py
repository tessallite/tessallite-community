"""Add ``is_shared`` column to ``saved_pivot_views``.

F-029-22: saved pivot views were strictly personal (the list filtered on
``created_by == current_user``), while saved queries are fully shared, with
nothing in the UI explaining the difference. This adds an explicit
``is_shared`` boolean so a user can publish a view to the whole tenant
deliberately, giving both features an explicit personal-vs-shared model.

The column defaults to ``false`` (server default ``false``), so every
existing view stays personal until its owner opts in — no behaviour change
for stored data.

Idempotent: guarded on the live schema (search_path is set to the target
schema by env.py).

Revision ID: 0141
Revises: 0140
Create Date: 2026-06-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0141"
down_revision = "0140"
branch_labels = None
depends_on = None

_TABLE = "saved_pivot_views"
_COLUMN = "is_shared"


def _has_table() -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return insp.has_table(_TABLE)


def _has_column() -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    try:
        return any(c["name"] == _COLUMN for c in insp.get_columns(_TABLE))
    except sa.exc.NoSuchTableError:
        return False


def upgrade() -> None:
    if not _has_table():
        return
    if not _has_column():
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
    if _has_column():
        op.drop_column(_TABLE, _COLUMN)
