"""Add ON DELETE CASCADE FK on user_entity_preferences.model_id.

F-029-15 (Bug-2516): ``user_entity_preferences.model_id`` was a bare UUID
column with no foreign key, so deleting a model stranded its favourite /
recently-used preference rows forever. This adds the missing
``ForeignKey("models.id", ondelete="CASCADE")`` so the rows are purged with
the model, matching every other model-scoped table in this area.

Orphan rows (model_id no longer references a live model) would block the
constraint, so they are deleted first — they were already unreachable (the
read path only returns rows for an existing model the caller can load).

Idempotent: guarded on the live schema (search_path is set to the target
schema by env.py).

Revision ID: 0139
Revises: 0138_named_set_version_unique
Create Date: 2026-06-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0139"
down_revision = "0138_named_set_version_unique"
branch_labels = None
depends_on = None

_TABLE = "user_entity_preferences"
_FK_NAME = "fk_user_entity_pref_model_id"


def _has_table() -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return insp.has_table(_TABLE)


def _has_fk() -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    try:
        return any(fk["name"] == _FK_NAME for fk in insp.get_foreign_keys(_TABLE))
    except sa.exc.NoSuchTableError:
        return False


def upgrade() -> None:
    if not _has_table() or _has_fk():
        return
    # Purge orphan rows that would otherwise violate the new constraint.
    op.execute(
        sa.text(
            "DELETE FROM user_entity_preferences "
            "WHERE model_id NOT IN (SELECT id FROM models)"
        )
    )
    op.create_foreign_key(
        _FK_NAME,
        _TABLE,
        "models",
        ["model_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    if _has_fk():
        op.drop_constraint(_FK_NAME, _TABLE, type_="foreignkey")
