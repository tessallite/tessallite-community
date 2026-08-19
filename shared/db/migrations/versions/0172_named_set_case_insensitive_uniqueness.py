"""Add case-insensitive unique index on named_sets (Bug-7927).

The resolver normalizes named-set keys to lowercase, so case variants
(``emea`` vs ``EMEA``) silently shadow each other at query time. This
migration adds a unique index on ``(model_id, lower(name))`` to enforce
case-insensitive uniqueness at the database level.

If legacy duplicate rows exist (same model_id + lowercase name), the
migration FAILS with a clear remediation message rather than silently
deleting any row.

Tenant-schema guarded (skip when the schema has no ``named_sets`` table),
idempotent, reversible.

Revision ID: 0172
Revises: 0171
Create Date: 2026-07-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0172"
down_revision = "0171"
branch_labels = None
depends_on = None

_INDEX_NAME = "uq_named_set_model_lower_name"


def _table_exists(conn, table_name: str) -> bool:
    result = conn.execute(
        sa.text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM information_schema.tables"
            "  WHERE table_schema = current_schema()"
            "    AND table_name = :t"
            ")"
        ),
        {"t": table_name},
    )
    return result.scalar()


def _index_exists(conn, index_name: str) -> bool:
    result = conn.execute(
        sa.text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM pg_indexes"
            "  WHERE schemaname = current_schema()"
            "    AND indexname = :i"
            ")"
        ),
        {"i": index_name},
    )
    return result.scalar()


def upgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, "named_sets"):
        return
    if _index_exists(conn, _INDEX_NAME):
        return

    # Check for legacy case-variant duplicates BEFORE creating the index.
    dupes = conn.execute(
        sa.text(
            "SELECT model_id, lower(name) AS lname, count(*) AS cnt "
            "FROM named_sets "
            "GROUP BY model_id, lower(name) "
            "HAVING count(*) > 1"
        )
    ).fetchall()
    if dupes:
        detail = "; ".join(
            f"model_id={row[0]}, name='{row[1]}' ({row[2]} rows)"
            for row in dupes
        )
        raise RuntimeError(
            f"Cannot create case-insensitive unique index on named_sets: "
            f"duplicate lowercase names exist. Resolve these manually before "
            f"re-running the migration: {detail}"
        )

    op.create_index(
        _INDEX_NAME,
        "named_sets",
        [sa.text("model_id"), sa.text("lower(name)")],
        unique=True,
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, "named_sets"):
        return
    if _index_exists(conn, _INDEX_NAME):
        op.drop_index(_INDEX_NAME, table_name="named_sets")
