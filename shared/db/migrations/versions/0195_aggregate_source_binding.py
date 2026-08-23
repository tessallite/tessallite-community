"""Bug-8602 - bind completed aggregate builds to their SOURCE routing identity.

Adds ``aggregate_definitions.built_for_source_binding`` (JSONB, nullable), the
sibling of ``built_for_storage_binding`` (0187) for the other side of the build:
WHICH database the CTAS read its rows FROM, rather than where it wrote them.

NULL means no completed build has recorded its source identity. Existing rows
stay routable under the control-plane invalidation contract (which now covers
the source side too); their next refresh records the binding.  The field is live
physical build metadata and is excluded from model snapshots.

Revision ID: 0195
Revises: 0194
Create Date: 2026-08-05
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0195"
down_revision = "0194"
branch_labels = None
depends_on = None

_TABLE = "aggregate_definitions"
_COLUMN = "built_for_source_binding"


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
