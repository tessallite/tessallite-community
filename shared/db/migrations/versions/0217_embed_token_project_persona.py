"""Record the agent ProjectPersona on embed-token inventory rows.

The JWT already carries the claim separately from the query-router/model
``persona_id``. The tenant inventory must retain both values so an issued token
can be audited without conflating the two persona namespaces.

Revision ID: 0217
Revises: 0216
Create Date: 2026-08-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0217"
down_revision = "0216"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {
        column["name"]
        for column in inspector.get_columns("embed_token_mints")
    } if "embed_token_mints" in set(inspector.get_table_names()) else set()
    if "project_persona_id" not in columns:
        op.add_column(
            "embed_token_mints",
            sa.Column("project_persona_id", sa.String(length=64), nullable=True),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {
        column["name"]
        for column in inspector.get_columns("embed_token_mints")
    } if "embed_token_mints" in set(inspector.get_table_names()) else set()
    if "project_persona_id" in columns:
        op.drop_column("embed_token_mints", "project_persona_id")
