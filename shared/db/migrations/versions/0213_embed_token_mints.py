"""Create tenant embed_token_mints inventory (F-021-08 / Bug-9315).

Mint, list, and revoke-stamp write EmbedTokenMint rows. Production tenant
schemas come from ``alembic upgrade tenant@head``, not ``create_all``. This
revision is the TENANT-chain create; it revises 0211 and must not merge with
system 0212.

Revision ID: 0213
Revises: 0211
Create Date: 2026-08-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0213"
down_revision = "0211"
branch_labels = None
depends_on = None

_TABLE = "embed_token_mints"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE in set(inspector.get_table_names()):
        return
    op.create_table(
        _TABLE,
        sa.Column("jti", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_email", sa.String(length=255), nullable=False),
        sa.Column("user_identity", sa.String(length=255), nullable=False),
        sa.Column("persona_id", sa.String(length=64), nullable=True),
        sa.Column("project_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("model_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("capabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("jti"),
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        return
    op.drop_table(_TABLE)
