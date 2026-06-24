"""Enforce one-fact-per-model at the database (F-013-11).

The one-fact-per-model rule was enforced only by a check-then-act API guard in
``tables.py::_assert_at_most_one_fact`` — count existing facts, then insert.
Two concurrent ``POST /tables`` (or a create racing a dim->fact PATCH) can both
pass the count and produce a two-fact model, which the aggregate matcher /
rewriter silently mis-handle. A partial unique index closes the race at the
storage layer; the API guard stays for the friendly 409 message.

Idempotent: guarded on index existence; search_path is set to the target
schema by env.py so this applies per tenant schema.

Revision ID: 0136
Revises: 0135
Create Date: 2026-06-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0136"
down_revision = "0135"
branch_labels = None
depends_on = None

_TABLE = "model_tables"
_INDEX = "uq_model_tables_one_fact_per_model"


def _table_exists() -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    try:
        insp.get_columns(_TABLE)
        return True
    except sa.exc.NoSuchTableError:
        return False


def _index_exists() -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    try:
        return any(ix["name"] == _INDEX for ix in insp.get_indexes(_TABLE))
    except sa.exc.NoSuchTableError:
        return False


def upgrade() -> None:
    if not _table_exists() or _index_exists():
        return
    # Partial unique index: at most one row per model_id where the table is a
    # fact. Dimension / mapping tables are unconstrained.
    op.create_index(
        _INDEX,
        _TABLE,
        ["model_id"],
        unique=True,
        postgresql_where=sa.text("table_type = 'fact'"),
    )


def downgrade() -> None:
    if _index_exists():
        op.drop_index(_INDEX, table_name=_TABLE)
