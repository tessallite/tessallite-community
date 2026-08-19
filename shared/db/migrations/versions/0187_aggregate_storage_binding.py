"""Bug-8481 - bind completed aggregate builds to their storage routing identity.

Adds ``aggregate_definitions.built_for_storage_binding`` (JSONB, nullable).
NULL means no completed build has recorded where its physical table was written.
Existing rows remain routable under the established control-plane invalidation
contract; their next refresh records the binding. The field is live physical
build metadata and is excluded from model snapshots.

Revision ID: 0187
Revises: 0186
Create Date: 2026-08-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0187"
down_revision = "0186"
branch_labels = None
depends_on = None

_TABLE = "aggregate_definitions"
_COLUMN = "built_for_storage_binding"


def _table_exists(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_column(table: str, column: str) -> bool:
    return any(
        c["name"] == column
        for c in sa.inspect(op.get_bind()).get_columns(table)
    )


def upgrade() -> None:
    if not _table_exists(_TABLE) or _has_column(_TABLE, _COLUMN):
        return
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    if not _table_exists(_TABLE) or not _has_column(_TABLE, _COLUMN):
        return
    op.drop_column(_TABLE, _COLUMN)
