"""Pocket fingerprint backfill — guarded no-op.

Revision ID: 0023
Revises: 0022
Create Date: 2026-04-19

The original backfill logic depended on shared SQL parser functions removed
during pocket-pipeline-unification (migration 0093). Rather than silently
passing, this migration checks whether any legacy pocket rows exist that
lack query_fingerprint (the only identity column present at this revision).
If so, it fails with an actionable message directing the operator to retire
those rows before continuing the migration chain.

Note: predicate_set_hash is added later in migration 0027; this guard only
checks columns that exist at revision 0022.

Safe for:
- Fresh databases: pocket_definitions table does not yet exist or is empty.
- Databases already past 0023: this migration already ran.
- Databases paused at 0022 with no pocket rows: guard passes, no-op.
"""
from alembic import op
from sqlalchemy import text

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    has_table = conn.execute(
        text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM information_schema.tables"
            "  WHERE table_name = 'pocket_definitions'"
            ")"
        )
    ).scalar()
    if not has_table:
        return
    legacy_count = conn.execute(
        text(
            "SELECT count(*) FROM pocket_definitions "
            "WHERE query_fingerprint IS NULL "
            "   OR query_fingerprint = ''"
        )
    ).scalar()
    if legacy_count and legacy_count > 0:
        raise RuntimeError(
            f"Migration 0023: found {legacy_count} pocket_definitions row(s) "
            f"missing query_fingerprint. These were created before "
            f"pocket-pipeline-unification and cannot be backfilled "
            f"automatically. Retire or delete them before upgrading: "
            f"UPDATE pocket_definitions SET status = 'stale', "
            f"retired_at = now() WHERE query_fingerprint IS NULL "
            f"OR query_fingerprint = '';"
        )


def downgrade() -> None:
    pass
