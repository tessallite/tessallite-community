"""Bug-8686: self-heal join_population_checks.inputs_fingerprint on schemas
where 0190 already ran without it.

Migration ``0190`` creates ``join_population_checks`` inside
``if not _table_exists(_CHECKS): op.create_table(...)``. As committed
(762208b1) that ``create_table`` call has always included
``inputs_fingerprint``. But on a schema where the table was brought into
existence by an earlier iteration of that same development effort — before
``inputs_fingerprint`` was added to the file — ``_table_exists`` now returns
True on every subsequent ``alembic upgrade``, so the create_table branch
(and its column list) is permanently skipped. The schema is left stuck at
whatever columns existed the first time the table was created, silently
diverging from both migration ``0190`` and ``shared/db/models.py``.

This is a repair migration, not a new feature: it adds exactly the one
column ``0190`` was always supposed to leave behind. PostgreSQL's
``ADD COLUMN IF NOT EXISTS`` makes it a no-op where ``0190`` ran cleanly and
also closes the check-then-add race between concurrent migration requests.

Revision ID: 0193
Revises: 0192
Create Date: 2026-08-04
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0193"
down_revision = "0192"
branch_labels = None
depends_on = None

_CHECKS = "join_population_checks"


def _table_exists(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _table_exists(_CHECKS):
        # The admin endpoint can launch two Alembic subprocesses for the same
        # tenant. PostgreSQL's IF NOT EXISTS keeps the repair idempotent after
        # both transactions wait on the table lock.
        op.execute(
            sa.text(
                'ALTER TABLE "join_population_checks" '
                'ADD COLUMN IF NOT EXISTS "inputs_fingerprint" VARCHAR(64)'
            )
        )


def downgrade() -> None:
    # Never drop the column on downgrade: 0190 is the migration that owns
    # this table's lifecycle (including dropping it entirely). Downgrading
    # past 0193 alone should not destroy data a schema may already be
    # relying on if it independently reached this state.
    pass
