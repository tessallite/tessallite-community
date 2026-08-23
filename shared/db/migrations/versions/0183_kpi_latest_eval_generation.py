"""Strictly-increasing kpi_latest ordering token (Bug-7982 R7 finding 2).

R6 ordered same-epoch ``kpi_latest`` writes by ``eval_started_at``, sourced from
``clock_timestamp()``. That value is NOT unique (the external gate sampled it
100k times live and got 82,242 duplicates), so an exact tie let the ``<=``
ordering comparison admit both writers and last-commit-wins resurfaced.

This migration adds:
  * ``kpi_eval_generation_seq`` — a tenant-schema sequence whose ``nextval`` is
    atomic and never repeats, giving a TOTAL order over evaluation starts;
  * ``kpi_latest.eval_generation`` — the allocated token, the new primary
    ordering key. ``eval_started_at`` stays as metadata / fallback order.

Existing rows keep ``eval_generation IS NULL``, which the ordering guard treats
as the oldest token, so the first stamped write after this migration wins — the
correct direction (it is the fresher evaluation).

Tenant-schema guarded (skip when the schema has no ``kpi_latest`` table),
idempotent, reversible.

Revision ID: 0183
Revises: 0182
Create Date: 2026-07-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0183"
down_revision = "0182"
branch_labels = None
depends_on = None

_KPI_LATEST = "kpi_latest"
_SEQUENCE = "kpi_eval_generation_seq"


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return any(c["name"] == column for c in sa.inspect(bind).get_columns(table))


def upgrade() -> None:
    # Only tenant schemas carry the model tables; gate on kpi_latest so this is a
    # no-op in the system schema.
    if not _table_exists(_KPI_LATEST):
        return
    # Unqualified: lands in the connection's search_path schema, the same
    # placement rule op.add_column uses for the tenant tables.
    op.execute(sa.text(f"CREATE SEQUENCE IF NOT EXISTS {_SEQUENCE}"))
    if not _has_column(_KPI_LATEST, "eval_generation"):
        op.add_column(
            _KPI_LATEST,
            sa.Column("eval_generation", sa.BigInteger(), nullable=True),
        )


def downgrade() -> None:
    if _table_exists(_KPI_LATEST) and _has_column(_KPI_LATEST, "eval_generation"):
        op.drop_column(_KPI_LATEST, "eval_generation")
    op.execute(sa.text(f"DROP SEQUENCE IF EXISTS {_SEQUENCE}"))
