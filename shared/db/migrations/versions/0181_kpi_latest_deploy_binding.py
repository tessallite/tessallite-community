"""Bind kpi_latest rows to the deployed version/epoch they were evaluated for.

Bug-7982 (Codex re-gate residual 2): the $KPIs virtual table serves values
straight from ``kpi_latest`` with no binding to the model's deployed definition.
After a definition-changing revert (which bumps ``models.deploy_epoch``) the
stale cached value was served for the NEW definition — a mixed-version wrong
number (e.g. a reverted profit KPI still serving the old revenue value).

This adds ``evaluated_for_version_id`` + ``evaluated_for_epoch`` so the serve
path can require an exact match against the model's current deployed pointer and
epoch, and treat an unknown (NULL) epoch as incompatible (fail-closed).

Tenant-schema guarded (skip when the schema has no ``kpi_latest`` table),
idempotent, reversible.

Revision ID: 0181
Revises: 0180
Create Date: 2026-07-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0181"
down_revision = "0180"
branch_labels = None
depends_on = None

_TABLE = "kpi_latest"


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return any(c["name"] == column for c in sa.inspect(bind).get_columns(table))


def upgrade() -> None:
    if not _table_exists(_TABLE):
        return
    if not _has_column(_TABLE, "evaluated_for_version_id"):
        op.add_column(
            _TABLE,
            sa.Column("evaluated_for_version_id", UUID(as_uuid=True), nullable=True),
        )
    if not _has_column(_TABLE, "evaluated_for_epoch"):
        op.add_column(
            _TABLE,
            sa.Column("evaluated_for_epoch", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    if not _table_exists(_TABLE):
        return
    if _has_column(_TABLE, "evaluated_for_epoch"):
        op.drop_column(_TABLE, "evaluated_for_epoch")
    if _has_column(_TABLE, "evaluated_for_version_id"):
        op.drop_column(_TABLE, "evaluated_for_version_id")
