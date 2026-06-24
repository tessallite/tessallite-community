"""Add is_generated flag to user_defined_attributes.

The hierarchy/date-template generator produces UDAs whose expression uses
EXTRACT/CASE — functions intentionally outside the user editor's allow-list.
Marking generator output lets the validator accept the system's own expression
unchanged on a rename/description edit instead of 422-ing the modeller for
functions they never typed (F-016-06).

Backfill: any UDA still referenced as a hierarchy level key or level attribute
must have been produced by the generator (the user editor never wires a UDA into
a hierarchy level), so it is stamped is_generated=True retroactively. This makes
the lifecycle fix apply to models created before this revision.

Idempotent: guard the add/drop on the live schema (search_path is set to the
target schema by env.py).

Revision ID: 0133
Revises: 0132
Create Date: 2026-06-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0133"
down_revision = "0132"
branch_labels = None
depends_on = None

_TABLE = "user_defined_attributes"
_COLUMN = "is_generated"


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
    # Backfill: UDAs wired into a hierarchy level (as the level key or as a
    # level attribute) are generator output. The user editor never attaches a
    # UDA to a hierarchy level, so this membership is a reliable proxy.
    op.execute(
        sa.text(
            """
            UPDATE user_defined_attributes uda
            SET is_generated = TRUE
            WHERE uda.id IN (
                SELECT hl.key_attribute_id
                FROM hierarchy_levels hl
                WHERE hl.key_attribute_source = 'user_defined_attribute'
                UNION
                SELECT hla.attribute_id
                FROM hierarchy_level_attributes hla
                WHERE hla.attribute_source = 'user_defined_attribute'
            )
            """
        )
    )


def downgrade() -> None:
    if _has_column():
        op.drop_column(_TABLE, _COLUMN)
