"""Add ``provider`` column to ``agent_cost_ledger``.

F-023-28: the per-turn cost ledger recorded ``llm_config_id`` but not the
provider string the spend was costed against, so the ``/cost`` report could
only infer answer-vs-judge spend rather than splitting per provider from
data, and the F-023-04 budget fix could not be validated directly.

This adds a nullable, indexed ``provider`` ``VARCHAR(64)`` column. It is
populated on every write going forward (``record_turn_cost`` /
``persist_turn`` already carry the provider). Existing rows keep NULL and
surface as ``"unknown"`` in the report — no backfill is attempted because
the historical provider is not recoverable from the ledger row alone.

Idempotent: guarded on the live schema (search_path is set to the target
schema by env.py).

Revision ID: 0140
Revises: 0139
Create Date: 2026-06-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0140"
down_revision = "0139"
branch_labels = None
depends_on = None

_TABLE = "agent_cost_ledger"
_COLUMN = "provider"
_INDEX = "ix_agent_cost_ledger_provider"


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


def _has_index() -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    try:
        return any(ix["name"] == _INDEX for ix in insp.get_indexes(_TABLE))
    except sa.exc.NoSuchTableError:
        return False


def upgrade() -> None:
    if not _has_table():
        return
    if not _has_column():
        op.add_column(
            _TABLE,
            sa.Column("provider", sa.String(length=64), nullable=True),
        )
    if not _has_index():
        op.create_index(_INDEX, _TABLE, ["provider"])


def downgrade() -> None:
    if _has_index():
        op.drop_index(_INDEX, table_name=_TABLE)
    if _has_column():
        op.drop_column(_TABLE, _COLUMN)
