"""Bug-8071 — per-reason history on a query miss row.

Adds ``query_miss_logs.miss_reason_counts_json`` (JSONB).

A ``QueryMissLog`` row is a ROLLUP: it is unique on
``(model_id, query_fingerprint, persona_id)`` and its ``occurrence_count``
accumulates across every repeat of that query shape. ``miss_reason``, however,
was overwritten by the conflict-update on every repeat, so one row was being
used as both cumulative candidate state AND event history — and only the last
event survived.

Two consequences this column removes:

* A modeller could not prove WHY a query kept missing over time. A pattern that
  missed 400 times because no aggregate covered its grain, then 3 times because
  the aggregate was stale, read as "stale".
* The optimizer could not tell an ABSENT aggregate (build one) from a stale or
  structurally ineligible one (a new aggregate fixes nothing), because no
  consumer read ``miss_reason`` at all.

Shape: a bounded list of
``{"reason", "occurrence_count", "first_seen_at", "last_seen_at"}``, merged in
Python on upsert exactly like the existing ``predicate_variants_json`` breakdown
on the same table. Bounded so a pathological reason cardinality cannot grow the
row without limit.

``miss_reason`` is KEPT and keeps its current meaning — the most recent reason —
so every existing reader is unaffected.

Upgrade posture: nullable, no backfill. NULL means "no per-reason history
recorded yet"; consumers treat it as unknown and fall back to ``miss_reason``,
so an upgrade cannot change any existing decision. Backfilling would invent
history that was never observed.

Tenant-schema guarded, idempotent, reversible.

Revision ID: 0186
Revises: 0185
Create Date: 2026-07-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0186"
down_revision = "0185"
branch_labels = None
depends_on = None

_TABLE = "query_miss_logs"
_COLUMN = "miss_reason_counts_json"


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return any(c["name"] == column for c in sa.inspect(bind).get_columns(table))


def upgrade() -> None:
    if not _table_exists(_TABLE):
        return
    if _has_column(_TABLE, _COLUMN):
        return
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    if not _table_exists(_TABLE):
        return
    if not _has_column(_TABLE, _COLUMN):
        return
    op.drop_column(_TABLE, _COLUMN)
