"""Unique (named_set_id, version_number) on named_set_versions.

F-018-18: `_create_version` computed ``max(version_number)+1`` with no unique
constraint, so two concurrent named-set updates could mint the same version
number and break revert-by-number. This adds the unique constraint; the
application retries `_create_version` on collision.

The migration de-duplicates any pre-existing duplicate (named_set_id,
version_number) rows first by renumbering them densely per named set, so the
constraint can be added cleanly on an existing tenant.

Revision ID: 0138_named_set_version_unique
Revises: 0137
Create Date: 2026-06-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0138_named_set_version_unique"
down_revision = "0137"
branch_labels = None
depends_on = None

_TABLE = "named_set_versions"
_CONSTRAINT = "uq_named_set_version_number"


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def _constraint_exists(bind, table: str, name: str) -> bool:
    insp = sa.inspect(bind)
    uniques = {uc["name"] for uc in insp.get_unique_constraints(table)}
    return name in uniques


def upgrade() -> None:
    if not _table_exists(_TABLE):
        return
    bind = op.get_bind()
    if _constraint_exists(bind, _TABLE, _CONSTRAINT):
        return
    # Renumber any duplicate version numbers densely (1..N per named set,
    # ordered by changed_at then id) so the unique constraint applies cleanly.
    bind.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY named_set_id
                           ORDER BY version_number, changed_at, id
                       ) AS rn
                FROM named_set_versions
            )
            UPDATE named_set_versions v
            SET version_number = ranked.rn
            FROM ranked
            WHERE v.id = ranked.id
              AND v.version_number <> ranked.rn
            """
        )
    )
    op.create_unique_constraint(
        _CONSTRAINT, _TABLE, ["named_set_id", "version_number"]
    )


def downgrade() -> None:
    if not _table_exists(_TABLE):
        return
    bind = op.get_bind()
    if _constraint_exists(bind, _TABLE, _CONSTRAINT):
        op.drop_constraint(_CONSTRAINT, _TABLE, type_="unique")
