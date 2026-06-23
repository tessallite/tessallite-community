"""Durable glossary bootstrap job registry (replica-shared, bounded).

F-018-03: the async glossary bootstrap status lived in a per-process module
dict (``_GLOSSARY_BOOTSTRAP_JOBS``). On Cloud Run with >1 model-service
replica a status poll could land on a replica that never saw the job and 404,
wedging the UI spinner; the dict was also unbounded (completed results lived
forever) and lost on restart. This adds a small per-tenant table that holds
job status, message, and the serialized result so any replica can answer a
poll. The application sweeps rows by TTL and a per-model retention cap, so the
table stays bounded.

Revision ID: 0131
Revises: 0130
Create Date: 2026-06-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0131"
down_revision = "0130"
branch_labels = None
depends_on = None

_TABLE = "glossary_bootstrap_jobs"


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    if _table_exists(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "model_id",
            UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("message", sa.Text()),
        sa.Column("result", JSONB()),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(f"ix_{_TABLE}_project_id", _TABLE, ["project_id"])
    op.create_index(f"ix_{_TABLE}_model_id", _TABLE, ["model_id"])
    op.create_index(f"ix_{_TABLE}_created_at", _TABLE, ["created_at"])


def downgrade() -> None:
    if _table_exists(_TABLE):
        op.drop_index(f"ix_{_TABLE}_created_at", table_name=_TABLE)
        op.drop_index(f"ix_{_TABLE}_model_id", table_name=_TABLE)
        op.drop_index(f"ix_{_TABLE}_project_id", table_name=_TABLE)
        op.drop_table(_TABLE)
