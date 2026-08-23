"""Bug-8140: detached durable post-commit physical cleanup outbox.

Aggregate and pocket target tables are external side effects: dropping them
inside the tenant metadata transaction makes rollback unable to restore them.
This tenant table records the complete detached target identity before model /
project metadata is deleted.  It intentionally carries no foreign key to any
row the delete may remove, and keeps credentials in the existing encrypted
credential envelope.

This is a TENANT-chain migration: it revises tenant head 0208.  The system
branch remains at 0207, preserving exactly one head per migration mode.

Revision ID: 0209
Revises: 0208
Create Date: 2026-08-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0209"
down_revision = "0208"
branch_labels = None
depends_on = None

_TABLE = "physical_cleanup_tasks"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE in set(inspector.get_table_names()):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_kind", sa.String(length=16), nullable=False),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_type", sa.String(length=32), nullable=False),
        sa.Column("connection_display_name", sa.String(length=255), nullable=False),
        sa.Column("encrypted_credentials", sa.LargeBinary(), nullable=False),
        sa.Column(
            "connection_config", postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.Column("target_schema", sa.String(length=512), nullable=False),
        sa.Column("qualified_table_name", sa.String(length=1024), nullable=False),
        sa.Column("requested_by", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "requested_at", sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("last_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "artifact_kind IN ('aggregate', 'pocket')",
            name="ck_physical_cleanup_tasks_artifact_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'failed', 'succeeded')",
            name="ck_physical_cleanup_tasks_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_physical_cleanup_tasks_model_id", _TABLE, ["model_id"], unique=False
    )
    op.create_index(
        "ix_physical_cleanup_tasks_project_id", _TABLE, ["project_id"], unique=False
    )
    op.create_index(
        "ix_physical_cleanup_tasks_next_attempt_at", _TABLE,
        ["next_attempt_at"], unique=False,
    )
    op.create_index(
        "ix_physical_cleanup_tasks_due", _TABLE,
        ["status", "next_attempt_at"], unique=False,
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        return
    op.drop_table(_TABLE)
