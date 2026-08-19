"""Add a DB-level CHECK on hierarchy_definitions.type (Bug-7195).

The hierarchy ``type`` domain (``explicit | date_embedded | segment``) is
enforced application-side by ``hierarchies.py::_normalize_hierarchy_type``
(ALLOWED_HIERARCHY_TYPES). This migration adds the matching fail-closed CHECK
constraint so no out-of-band writer (import/rehydrate, a script, a future
endpoint) can persist an unroutable hierarchy type.

Tenant-schema guarded (skip when the schema has no ``hierarchy_definitions``
table), idempotent (skip when the constraint already exists), and reversible.
Any pre-existing out-of-domain value is coerced to ``explicit`` before the
constraint is added so an ADD CONSTRAINT can never fail on legacy data — the
API layer never wrote such a value, but the coercion keeps the migration safe.

Revision ID: 0167
Revises: 0166
Create Date: 2026-07-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0167"
down_revision = "0166"
branch_labels = None
depends_on = None

_TABLE = "hierarchy_definitions"
_CONSTRAINT = "ck_hierarchy_definitions_type"
_ALLOWED = ("explicit", "date_embedded", "segment")


def _existing_check_constraints(
    inspector: sa.engine.reflection.Inspector, table: str
) -> set[str]:
    return {c["name"] for c in inspector.get_check_constraints(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    if _CONSTRAINT in _existing_check_constraints(inspector, _TABLE):
        return
    # Coerce any legacy out-of-domain value so ADD CONSTRAINT cannot fail.
    op.execute(
        sa.text(
            f"UPDATE {_TABLE} SET type = 'explicit' "
            "WHERE type NOT IN ('explicit', 'date_embedded', 'segment')"
        )
    )
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        "type IN ('explicit', 'date_embedded', 'segment')",
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    if _CONSTRAINT not in _existing_check_constraints(inspector, _TABLE):
        return
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
