"""Drop legacy-named FK constraints on log tables missed by 0091 (Bug-1019).

Migration 0091 (Bug-411) removed referential integrity from the log
tables because ON DELETE SET NULL collides with the nulls-not-distinct
unique key on ``query_miss_logs``. It dropped the default-named
constraints (``query_miss_logs_persona_id_fkey`` etc.), but migration
0042 had previously RENAMED the perspective-era FKs to
``fk_<table>_persona_id``, and 0018 created ``fk_query_logs_pocket_id``
explicitly — those names survived 0091 in every tenant schema.

Live symptom (found during the B1 personas/CLS verification): deleting a
persona 500s with ``uq_query_miss_logs_model_fingerprint_persona``
because the leftover ``fk_query_miss_logs_persona_id`` SET-NULLs the
persona's miss rows onto existing NULL-persona rows.

Revision ID: 0125
Revises: 0124
Create Date: 2026-06-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0125"
down_revision = "0124"
branch_labels = None
depends_on = None


_FK_DROPS = [
    ("query_logs", "fk_query_logs_persona_id"),
    ("query_logs", "fk_query_logs_pocket_id"),
    ("query_miss_logs", "fk_query_miss_logs_persona_id"),
]

_FK_RESTORE = [
    ("query_logs", "fk_query_logs_persona_id",
     "personas", ["persona_id"], ["id"], "SET NULL"),
    ("query_logs", "fk_query_logs_pocket_id",
     "pocket_definitions", ["pocket_id"], ["id"], "SET NULL"),
    ("query_miss_logs", "fk_query_miss_logs_persona_id",
     "personas", ["persona_id"], ["id"], "SET NULL"),
]


def _fk_exists(table: str, constraint: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM information_schema.table_constraints"
            "  WHERE constraint_name = :c AND table_name = :t"
            "    AND table_schema = current_schema()"
            "    AND constraint_type = 'FOREIGN KEY'"
            ")"
        ),
        {"c": constraint, "t": table},
    )
    return result.scalar()


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    for table, constraint in _FK_DROPS:
        if _table_exists(table) and _fk_exists(table, constraint):
            op.drop_constraint(constraint, table, type_="foreignkey")


def downgrade() -> None:
    for table, constraint, ref_table, local_cols, remote_cols, ondelete in _FK_RESTORE:
        if _table_exists(table) and not _fk_exists(table, constraint):
            op.create_foreign_key(
                constraint, table, ref_table, local_cols, remote_cols,
                ondelete=ondelete,
            )
