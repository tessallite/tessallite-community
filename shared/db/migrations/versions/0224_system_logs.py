"""Raw application logs in the system database.

Revision ID: 0224
Revises: 0212 (system branch)
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0224"
down_revision = "0212"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "system_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("timestamp", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("service", sa.String(64), nullable=False),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("logger", sa.String(255), nullable=False),
        sa.Column("instance", sa.String(255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        schema="tess_system",
    )
    op.create_index(
        "ix_system_logs_timestamp_id",
        "system_logs",
        ["timestamp", "id"],
        schema="tess_system",
    )
    op.create_index(
        "ix_system_logs_errors_timestamp",
        "system_logs",
        ["timestamp"],
        schema="tess_system",
        postgresql_where=sa.text("level IN ('ERROR', 'CRITICAL')"),
    )


def downgrade():
    op.drop_table("system_logs", schema="tess_system")
