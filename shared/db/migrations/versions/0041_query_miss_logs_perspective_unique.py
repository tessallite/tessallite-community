"""Composite unique key on query_miss_logs — Phase 8.C.2.

Revision ID: 0041
Revises: 0040
Create Date: 2026-04-24

Replaces the pre-8.C unique key ``(model_id, query_fingerprint)`` on
``query_miss_logs`` with the composite ``(model_id, query_fingerprint,
perspective_id)`` so the same fingerprint missed under two different
perspectives is tracked as two distinct rows instead of being
collapsed.

Uses ``NULLS NOT DISTINCT`` so a global (perspective_id NULL) miss is
still deduped across repeat occurrences — under default PostgreSQL
semantics NULL values are treated as distinct, which would let the
same global miss insert over and over.

Requires PostgreSQL >= 15.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


_TABLE = "query_miss_logs"
_NEW_NAME = "uq_query_miss_logs_model_fingerprint_perspective"


def _find_existing_unique_constraint_name() -> str | None:
    """Return the existing UNIQUE(model_id, query_fingerprint) constraint
    name (auto-named or hand-named), or None if it no longer exists.
    Scoped to current_schema so shared-database deployments do not pick
    up another tenant's constraint.
    """
    conn = op.get_bind()
    return conn.execute(
        sa.text(
            """
            SELECT c.conname
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE c.contype = 'u'
              AND t.relname = :table
              AND n.nspname = current_schema()
              AND (
                SELECT array_agg(a.attname ORDER BY u.ord)
                FROM unnest(c.conkey) WITH ORDINALITY AS u(attnum, ord)
                JOIN pg_attribute a
                  ON a.attrelid = c.conrelid AND a.attnum = u.attnum
              ) = ARRAY['model_id', 'query_fingerprint']::name[]
            LIMIT 1
            """
        ),
        {"table": _TABLE},
    ).scalar()


def upgrade() -> None:
    existing = _find_existing_unique_constraint_name()
    if existing is not None:
        op.drop_constraint(existing, _TABLE, type_="unique")

    # PostgreSQL 15+ supports NULLS NOT DISTINCT so NULL perspective_id
    # rows still dedupe under the composite key.
    op.execute(
        f'ALTER TABLE "{_TABLE}" ADD CONSTRAINT "{_NEW_NAME}" '
        'UNIQUE NULLS NOT DISTINCT (model_id, query_fingerprint, perspective_id)'
    )


def downgrade() -> None:
    op.execute(f'ALTER TABLE "{_TABLE}" DROP CONSTRAINT IF EXISTS "{_NEW_NAME}"')
    op.create_unique_constraint(
        "uq_query_miss_logs_model_fingerprint",
        _TABLE,
        ["model_id", "query_fingerprint"],
    )
