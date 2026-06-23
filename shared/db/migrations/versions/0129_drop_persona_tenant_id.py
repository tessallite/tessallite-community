"""Drop orphan tenant_id columns from project persona tables (Bug-1071).

Migration 0087 created ``project_personas`` and
``project_persona_model_scopes`` with a NOT NULL ``tenant_id`` column,
but the ORM models (``shared/db/models.py``) never mapped it and no code
path populates it — per-tenant ``{slug}_meta`` tables are isolated by
schema and carry no tenant_id, like every sibling table. Result: every
``POST /projects/{id}/agent/personas`` since 0087 has died with a
NotNullViolation 500 — the project persona feature was unusable.
Found during the H5 (F-023-08) live persona enforcement probe.

This drops the orphan columns so the table matches the ORM. Guarded so
it is a no-op where the column does not exist. Downgrade restores the
columns as NULLABLE (the original NOT NULL could never be satisfied by
the application anyway).

Revision ID: 0129
Revises: 0127
Create Date: 2026-06-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0129"
down_revision = "0127"
branch_labels = None
depends_on = None

_TABLES = ("project_personas", "project_persona_model_scopes")


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    return conn.execute(
        sa.text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM information_schema.columns"
            "  WHERE table_name = :t AND column_name = :c"
            "    AND table_schema = current_schema()"
            ")"
        ),
        {"t": table, "c": column},
    ).scalar()


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    for table in _TABLES:
        if _table_exists(table) and _column_exists(table, "tenant_id"):
            op.drop_column(table, "tenant_id")


def downgrade() -> None:
    for table in _TABLES:
        if _table_exists(table) and not _column_exists(table, "tenant_id"):
            op.add_column(table, sa.Column("tenant_id", sa.String(64), nullable=True))
