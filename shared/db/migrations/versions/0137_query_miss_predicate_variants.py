"""Add predicate_variants_json to query_miss_logs.

F-005-14 (Bug-2256): a QueryMissLog row is keyed on the literal-free
fingerprint, so different literal variants of one query shape (country='GB'
vs country='US') collapse into a single row whose ``predicates_json`` keeps
only the LATEST variant while ``occurrence_count`` sums ALL variants. The
pocket suggester would then justify a US-only pocket with the combined GB+US
hit count. ``predicate_variants_json`` records a per-variant breakdown so the
pocket path sizes and scores each literal slice on its own hits.

Additive and nullable: the aggregate optimizer (which groups by grain/measures
and intentionally sums across literals) ignores this column.

Idempotent: guard the add/drop on the live schema (search_path is set to the
target schema by env.py).

Revision ID: 0137
Revises: 0136
Create Date: 2026-06-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0137"
down_revision = "0136"
branch_labels = None
depends_on = None

_TABLE = "query_miss_logs"
_COLUMN = "predicate_variants_json"


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
            sa.Column(_COLUMN, postgresql.JSONB(), nullable=True),
        )


def downgrade() -> None:
    if _has_column():
        op.drop_column(_TABLE, _COLUMN)
