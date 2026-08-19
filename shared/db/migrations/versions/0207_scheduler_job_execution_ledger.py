"""Create tess_system.scheduler_job_executions — durable scheduler job ledger.

F-012-05 / Bug-8132. Operators could see a job's NEXT run time but never its
LAST outcome: ``GET /scheduler/jobs`` served only ``next_run_time`` and no
common execution ledger existed, so a healthy no-op, a failed run and a dropped
Cloud Run tick were indistinguishable. This creates the durable per-run ledger
the APScheduler start/success/error/misfire listeners (and the manual
``POST /scheduler/trigger/{job_id}`` endpoint, Bug-8133) write to.

Migration slotting (critical): the ledger is a ``tess_system`` (SystemBase)
table — the sweeps run system-wide, so their history is platform metadata, not
tenant data. This chain has two INTENTIONAL heads (``system`` rooted at 0001,
``tenant`` rooted at 0002); deployments run ``alembic upgrade system@head`` and
``tenant@head`` separately and bare ``upgrade head`` is unsupported (see
env.py). This revision therefore chains onto the SYSTEM head (``down_revision =
"0206"``, the SSO/replay migration; system chain 0016->0128->0189->0206), so
``system@head`` creates it in ``tess_system``. ``branch_labels = None`` inherits
the ``system`` label from root 0001. It does NOT chain onto the tenant head
0204 (that would leave the table absent from ``tess_system`` and every ledger
write would 500), and it authors NO merge revision (which env.py forbids — a
merge would collapse the two heads and break MIGRATE_MODE schema isolation).

Idempotent create, guarded on the live schema, so a partially-migrated
environment converges cleanly. Reversible downgrade drops the table.

Revision ID: 0207
Revises: 0206
Create Date: 2026-08-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0207"
down_revision = "0206"
branch_labels = None
depends_on = None

_SCHEMA = "tess_system"
_TABLE = "scheduler_job_executions"
_UQ_CORRELATION = "uq_scheduler_job_executions_correlation"
_IX_STARTED = "ix_scheduler_job_executions_job_started"


def _has_table(conn, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = :s AND table_name = :t"
            ),
            {"s": _SCHEMA, "t": table},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()

    if not _has_table(conn, _TABLE):
        op.create_table(
            _TABLE,
            sa.Column(
                "id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                primary_key=True,
            ),
            sa.Column("job_id", sa.String(128), nullable=False),
            sa.Column(
                "scheduled_fire_time", sa.TIMESTAMP(timezone=True), nullable=True
            ),
            sa.Column(
                "trigger_source",
                sa.String(16),
                nullable=False,
                server_default=sa.text("'scheduled'"),
            ),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column(
                "started_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
            sa.Column("outcome", sa.Text(), nullable=True),
            sa.Column("error_text", sa.Text(), nullable=True),
            # B05: matches the ORM (Mapped[datetime], NOT NULL) — the first
            # revision created this column nullable, diverging from the model.
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            # B02: the correlation key is UNIQUE so start/terminal events for one
            # run upsert into ONE row (execution_ledger.record_*), even reordered.
            sa.UniqueConstraint(
                "job_id",
                "scheduled_fire_time",
                "trigger_source",
                name=_UQ_CORRELATION,
            ),
            schema=_SCHEMA,
        )
        op.create_index(
            _IX_STARTED, _TABLE, ["job_id", "started_at"], schema=_SCHEMA
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, _TABLE):
        op.drop_index(_IX_STARTED, table_name=_TABLE, schema=_SCHEMA)
        # The unique constraint is dropped with the table.
        op.drop_table(_TABLE, schema=_SCHEMA)
