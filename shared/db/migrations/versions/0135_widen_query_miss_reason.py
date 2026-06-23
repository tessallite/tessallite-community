"""Widen query_miss_logs.miss_reason from VARCHAR(64) to VARCHAR(255).

F-004-11: the aggregate-skip miss reason is built as
``"aggregate_skip:" + ",".join(reasons)`` — three skip reasons already exceed
64 characters, so the stored reason was silently truncated mid-token and the
modeler-facing "why didn't my query hit an aggregate?" signal was lost. Widen
the column so the full reason survives; the ``_MISS_REASON_MAX_LEN`` guard in
the query-router logger is widened in lock-step.

Idempotent: only alters when the current column length is below the target
(search_path is set to the target schema by env.py).

Revision ID: 0135
Revises: 0134
Create Date: 2026-06-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0135"
down_revision = "0134"
branch_labels = None
depends_on = None

_TABLE = "query_miss_logs"
_COLUMN = "miss_reason"
_NEW_LEN = 255
_OLD_LEN = 64


def _current_length() -> int | None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    try:
        for c in insp.get_columns(_TABLE):
            if c["name"] == _COLUMN:
                t = c["type"]
                return getattr(t, "length", None)
    except sa.exc.NoSuchTableError:
        return None
    return None


def upgrade() -> None:
    length = _current_length()
    if length is not None and length < _NEW_LEN:
        op.alter_column(
            _TABLE,
            _COLUMN,
            existing_type=sa.String(length=length),
            type_=sa.String(length=_NEW_LEN),
            existing_nullable=False,
        )


def downgrade() -> None:
    length = _current_length()
    if length is not None and length > _OLD_LEN:
        # Truncate any over-length rows before narrowing so the ALTER succeeds.
        op.execute(
            sa.text(
                f"UPDATE {_TABLE} SET {_COLUMN} = LEFT({_COLUMN}, {_OLD_LEN}) "
                f"WHERE LENGTH({_COLUMN}) > {_OLD_LEN}"
            )
        )
        op.alter_column(
            _TABLE,
            _COLUMN,
            existing_type=sa.String(length=length),
            type_=sa.String(length=_OLD_LEN),
            existing_nullable=False,
        )
