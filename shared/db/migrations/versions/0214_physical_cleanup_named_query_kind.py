"""F-013-07: allow 'named_query' as a physical_cleanup_tasks artifact_kind.

Named Query artifacts are the third materialised family (after aggregates and
pockets). Model / project delete must schedule a DROP of their target table, but
the ``ck_physical_cleanup_tasks_artifact_kind`` CHECK only admitted
``'aggregate'`` and ``'pocket'``, so a scheduled NQ cleanup row would abort the
delete transaction. Widen the CHECK to include ``'named_query'``.

This is a TENANT-chain migration: it revises tenant head 0213. The system branch
remains at 0212. Idempotent.

Revision ID: 0214
Revises: 0213
Create Date: 2026-08-18
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0214"
down_revision = "0213"
branch_labels = None
depends_on = None

_TABLE = "physical_cleanup_tasks"
_CONSTRAINT = "ck_physical_cleanup_tasks_artifact_kind"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        return
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT} "
        "CHECK (artifact_kind IN ('aggregate', 'pocket', 'named_query'))"
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        return
    # Best-effort: only narrow back if no named_query rows exist, else leave the
    # widened constraint (a narrower CHECK would reject live rows).
    has_nq = op.get_bind().execute(
        sa.text(
            f"SELECT 1 FROM {_TABLE} WHERE artifact_kind = 'named_query' LIMIT 1"
        )
    ).first()
    if has_nq is not None:
        return
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT} "
        "CHECK (artifact_kind IN ('aggregate', 'pocket'))"
    )
