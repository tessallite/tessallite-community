"""Enforce unique (model_id, alias) on model_tables (Bug-3577).

The alias uniqueness within a model was enforced only by a check-then-act
API guard (_assert_alias_unique_in_model in tables.py) — read existing
aliases, then insert.  Two concurrent requests can both pass the count
check and produce duplicate aliases, breaking semantic binding and SQL
generation.  A UNIQUE constraint closes the race at the storage layer.

The app-level IntegrityError retry (Bug-5246 in calendar.py) survives
intact and will now be triggered by this real DB constraint rather than
relying on check-then-act.

Idempotent: guarded on constraint existence; search_path is set to the
target schema by env.py so this applies per tenant schema.

Revision ID: 0150
Revises: 0149
Create Date: 2026-06-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0150"
down_revision = "0149"
branch_labels = None
depends_on = None

_TABLE = "model_tables"
_CONSTRAINT = "uq_model_tables_model_id_alias"


def _table_exists() -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    try:
        insp.get_columns(_TABLE)
        return True
    except sa.exc.NoSuchTableError:
        return False


def _constraint_exists() -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    try:
        return any(
            uc["name"] == _CONSTRAINT
            for uc in insp.get_unique_constraints(_TABLE)
        )
    except sa.exc.NoSuchTableError:
        return False


def upgrade() -> None:
    if not _table_exists() or _constraint_exists():
        return
    op.create_unique_constraint(
        _CONSTRAINT,
        _TABLE,
        ["model_id", "alias"],
    )


def downgrade() -> None:
    if _constraint_exists():
        op.drop_constraint(_CONSTRAINT, _TABLE, type_="unique")
