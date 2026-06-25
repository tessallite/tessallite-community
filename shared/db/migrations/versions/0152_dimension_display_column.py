"""Add dimensions.display_column_id for flat-dim display attribute (Bug-5434).

A flat (single-level) dimension can carry a DISPLAY column distinct from its KEY
column (``source_column_id``). When set, member discovery surfaces the display
column's value as the member caption (MEMBER_NAME) while the key remains the
member identity (MEMBER_KEY). NULL keeps the legacy behaviour (caption == key).

The column is a nullable FK to ``model_columns.id`` with ON DELETE SET NULL,
mirroring ``source_column_id`` so a dropped column degrades gracefully rather
than orphaning the dimension.

Idempotent: guarded on column existence; search_path is set to the target schema
by env.py so this applies per tenant schema.

Revision ID: 0152
Revises: 0151
Create Date: 2026-06-24
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from alembic import op

revision = "0152"
down_revision = "0151"
branch_labels = None
depends_on = None

_TABLE = "dimensions"
_COLUMN = "display_column_id"
_FK = "dimensions_display_column_id_fkey"
_IX = "ix_dimensions_display_column_id"


def _table_exists() -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    try:
        insp.get_columns(_TABLE)
        return True
    except sa.exc.NoSuchTableError:
        return False


def _column_exists() -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    try:
        return any(c["name"] == _COLUMN for c in insp.get_columns(_TABLE))
    except sa.exc.NoSuchTableError:
        return False


def upgrade() -> None:
    if not _table_exists() or _column_exists():
        return
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, PGUUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        _FK,
        _TABLE,
        "model_columns",
        [_COLUMN],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(_IX, _TABLE, [_COLUMN])


def downgrade() -> None:
    if not _column_exists():
        return
    op.drop_index(_IX, table_name=_TABLE)
    op.drop_constraint(_FK, _TABLE, type_="foreignkey")
    op.drop_column(_TABLE, _COLUMN)
