"""NQ canonical population contract v1 — invalidate every pre-contract artifact.

Bug-9161 corrected Phase 1 (data-only migration, no schema change). Every Named
Query artifact that is ``fresh`` or carries a row manifest was built by a
SUPERSEDED population compiler (the pre-contract raw route or the incomplete
Phase-1 compile) and must NEVER serve again: the new serve-side gate
(``named_query_population_manifest_matches``) requires the current manifest
version, the live-build binding, and the v1 population fingerprint, which no
pre-contract artifact carries.

The UPDATE is unconditional for matching rows — belt-and-suspenders on top of
the serve gate (which alone would refuse these artifacts anyway): it also
clears the stale manifest/liveness pointer so the next refresh writes a fresh
one and no downstream consumer mistakes a pre-contract manifest for evidence.

``downgrade()`` is deliberately a no-op: a pre-contract artifact must never be
re-trusted by reverting this migration. The rebuild is the repair.

This is a TENANT-chain migration: it revises 0210 (the NQ tenant head). The
system branch remains at 0207, preserving exactly one head per migration mode.

Revision ID: 0211
Revises: 0210
Create Date: 2026-08-16
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0211"
down_revision = "0210"
branch_labels = None
depends_on = None

_REBUILD_REASON = "Rebuild required: NQ canonical population contract v1"


def upgrade() -> None:
    conn = op.get_bind()
    has_table = conn.execute(
        text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM information_schema.tables"
            "  WHERE table_schema = current_schema()"
            "  AND table_name = 'named_query_artifacts'"
            ")"
        )
    ).scalar()
    if not has_table:
        # Fresh database that has not reached 0210 yet, or a tenant schema
        # created after it: nothing to invalidate.
        return
    conn.execute(
        text(
            "UPDATE named_query_artifacts"
            "   SET status = 'stale',"
            "       row_manifest = NULL,"
            "       active_refresh_run_id = NULL,"
            "       failure_reason = :reason"
            " WHERE status = 'fresh' OR row_manifest IS NOT NULL"
        ),
        {"reason": _REBUILD_REASON},
    )


def downgrade() -> None:
    # Never re-trust a pre-contract artifact: the rebuild is the repair.
    pass
