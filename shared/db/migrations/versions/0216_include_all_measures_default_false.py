"""Bug-9409 / F-102-26 (Choice A): default ``include_all_measures`` to FALSE.

Two individually correct features composed into a machine that never hits. With
``include_all_measures`` on, the optimizer materialised EVERY model measure at a
grain; the query-router's row-population proof (Bug-8664) then refused to serve
that artifact for any query whose own plan would not join the relations those
extra measures dragged in. ``modely`` carried ~50 aggregates and a 24-hour hit
rate of zero. The user's decision (2026-08-17) is the simple product path:
materialise what the workload asks for, and make all-measure aggregates an
explicit opt-in.

WHAT THIS MIGRATION DOES — and deliberately does not do
-------------------------------------------------------
It alters the COLUMN DEFAULT only. No row is rewritten, so every existing model
keeps the value it has today, which is the "preserve persisted values" half of
the decision. A model created after this migration gets ``false`` unless the
caller sends a value.

The old default was written by migration 0106 as ``server_default=true``. A
column default is not carried in the deployed snapshot and is not a definition,
so no artifact is invalidated and no aggregate is retired here: changing the
flag on a model goes through the ordinary aggregate lifecycle (the optimizer
sweep's ``backfill_include_all_measures`` widens on OFF->ON; new builds narrow
on ON->OFF), never through a migration.

This is a TENANT-chain migration (``models`` lives in ``<slug>_meta``); the table
name is unqualified and resolves against the per-tenant ``search_path`` the
runner sets, exactly like 0214/0215. It revises tenant head 0215. The system
branch remains at 0212.

Revision ID: 0216
Revises: 0215
Create Date: 2026-08-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0216"
down_revision = "0215"
branch_labels = None
depends_on = None

_TABLE = "models"
_COLUMN = "include_all_measures"


def _has_column(bind) -> bool:
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return False
    return any(col["name"] == _COLUMN for col in inspector.get_columns(_TABLE))


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind):
        # A tenant schema created before 0106, or a partial schema; nothing to
        # alter. Idempotent by construction.
        return
    op.alter_column(
        _TABLE,
        _COLUMN,
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default=sa.text("false"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind):
        return
    op.alter_column(
        _TABLE,
        _COLUMN,
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default=sa.text("true"),
    )
