"""Bug-8639: stale aggregates built by the optimizer creator's own pre-fix
join renderer.

``optimizer/src/lifecycle/creator.py::_build_source_from_clause`` was a THIRD
independent renderer of ``Join.join_type`` (docs/architecture/architecture_
join-orientation-and-cardinality.md's "Known gap"): it emits the aggregate
CTAS at CREATION time, while ``shared/semantic/sql_builder.py`` (fixed by
Bug-8628 / migration ``0191``) emits the scheduler's REFRESH CTAS for the
SAME aggregate. Its bug was worse than Bug-8628's: it had no per-token map at
all, only ``"LEFT JOIN" if join_type.lower() != "inner" else "INNER JOIN"`` —
every right/full/legacy token collapsed onto LEFT JOIN, with no ``flipped``
concept whatsoever.

Migration ``0191`` already staled every aggregate/pocket affected by
``sql_builder.py``'s narrower Bug-8628 defect. This migration covers the
residual gap: any AGGREGATE (creator.py only builds aggregates, never
pockets) whose CREATE-time rows could have been miscompiled by creator.py's
OWN pre-fix history, which is not identical to ``sql_builder.py``'s — a bare
``"full"`` join_type is the concrete case where the two disagree (see
``shared/semantic/join_orientation_invalidation.py`` module docstring).
Running this predicate is mostly a no-op re-confirmation for tokens 0191
already caught (left/right/aliases), and closes the one token it could not
have caught for creator-built aggregates (bare "full").

Like ``0191``, the token set fed to the predicate is the UNION of the live
``joins`` rows and every stored model-version snapshot's join tokens: an
aggregate's physical rows reflect the tokens in force WHEN IT WAS BUILT, and a
join edited after the build (and not redeployed) would otherwise leave the
live graph looking clean while the aggregate still holds pre-fix rows.

Revision ID: 0192
Revises: 0191
Create Date: 2026-08-04
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from shared.semantic.join_orientation_invalidation import (
    creator_model_ids_needing_rebuild,
)

revision = "0192"
down_revision = "0191"
branch_labels = None
depends_on = None

_JOINS = "joins"
_AGGREGATE_DEFINITIONS = "aggregate_definitions"


def _table_exists(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if not _table_exists(_JOINS) or not _table_exists(_AGGREGATE_DEFINITIONS):
        return

    bind = op.get_bind()

    rows = bind.execute(
        sa.text(f"SELECT model_id, join_type FROM {_JOINS}")
    ).fetchall()

    token_rows: list[tuple[object, object]] = [
        (row.model_id, row.join_type) for row in rows
    ]
    if _table_exists("model_versions"):
        for version in bind.execute(
            sa.text(
                "SELECT model_id, snapshot_json FROM model_versions "
                "WHERE snapshot_unavailable IS NOT TRUE"
            )
        ).fetchall():
            snapshot = version.snapshot_json
            if not isinstance(snapshot, dict):
                continue
            for join in snapshot.get("joins") or []:
                if isinstance(join, dict):
                    token_rows.append((version.model_id, join.get("join_type")))

    if not token_rows:
        return

    affected = creator_model_ids_needing_rebuild(token_rows)
    if not affected:
        return
    model_ids = [str(m) for m in affected]

    # ``is_stale`` is the aggregate matcher's own hard refusal
    # (aggregate_matcher.py:153) AND the scheduler's always-due signal, so one
    # flag both stops the artifact serving and queues its rebuild. Pockets are
    # NOT touched here: creator.py builds aggregates only, never pockets.
    bind.execute(
        sa.text(
            f"UPDATE {_AGGREGATE_DEFINITIONS} SET is_stale = true "
            "WHERE status = 'active' AND is_stale = false "
            "AND model_id = ANY(CAST(:ids AS uuid[]))"
        ),
        {"ids": model_ids},
    )


def downgrade() -> None:
    # The staleness flag is deliberately NOT reverted: an aggregate built by
    # the pre-fix creator.py renderer is wrong under the fixed one and wrong
    # again if the code is rolled back to build it a third way. Leaving it
    # stale forces one rebuild under whichever builder is actually running.
    pass
