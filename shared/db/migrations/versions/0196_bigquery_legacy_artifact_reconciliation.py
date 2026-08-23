"""Bug-8761 - take pre-validation BigQuery artifacts out of serving.

Rows created before the one-project target contract may have been built through
a BigQuery target whose caller-owned ``target_type`` or config disagreed with
the connection the router uses to scan. Their physical storage is preserved,
but every artifact on a BigQuery-marked legacy target must rebuild before it can
serve. The migration is tenant-schema safe: it touches only tables in the
schema Alembic is currently upgrading and is idempotent on repeated execution.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0196"
down_revision = "0195"
branch_labels = None
depends_on = None

_REQUIRED_TABLES = (
    "data_targets",
    "project_connections",
    "aggregate_definitions",
    "pocket_definitions",
)


def _tables_exist() -> bool:
    inspector = sa.inspect(op.get_bind())
    return all(inspector.has_table(table) for table in _REQUIRED_TABLES)


def _legacy_bigquery_target_predicate() -> str:
    """Target rows whose pre-contract state cannot prove one-project routing.

    ``connection_type`` catches the caller-label bypass; ``target_type`` also
    catches a historical target labelled BigQuery against another connection.
    Rebuilding all such pre-existing artifacts is intentionally conservative:
    pre-migration rows did not record enough validated authority to distinguish
    the safe subset without decrypting credentials in a schema migration.
    """
    return """
        target_id IN (
            SELECT dt.id
            FROM data_targets AS dt
            JOIN project_connections AS pc ON pc.id = dt.project_connection_id
            WHERE lower(coalesce(pc.connection_type, '')) = 'bigquery'
               OR lower(coalesce(dt.target_type, '')) = 'bigquery'
        )
    """


def upgrade() -> None:
    if not _tables_exist():
        return
    predicate = _legacy_bigquery_target_predicate()
    # Do not drop physical storage. The normal refresh lifecycle owns cleanup;
    # this transition only removes unsafe artifacts from matcher eligibility.
    op.execute(sa.text(f"""
        UPDATE pocket_definitions
        SET status = 'stale',
            row_manifest = NULL,
            active_refresh_run_id = NULL,
            failure_reason = 'BigQuery target requires rebuild after one-project routing transition'
        WHERE {predicate}
          AND retired_at IS NULL
          AND status <> 'stale'
    """))
    op.execute(sa.text(f"""
        UPDATE aggregate_definitions
        SET status = CASE WHEN status = 'disabled' THEN 'disabled' ELSE 'pending' END,
            is_stale = TRUE,
            active_refresh_run_id = NULL
        WHERE {predicate}
          AND retired_at IS NULL
          AND status <> 'retired'
          AND (is_stale IS DISTINCT FROM TRUE OR active_refresh_run_id IS NOT NULL)
    """))


def downgrade() -> None:
    # This migration deliberately does not reactivate cache rows: a rebuild is
    # the only evidence that can safely restore their serving eligibility.
    return
