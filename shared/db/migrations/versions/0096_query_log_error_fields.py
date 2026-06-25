"""Add status, error_type, error_detail columns to query_logs.

Extends query_logs to durably store failed queries alongside successes.
Existing rows default to status='success' with NULL error fields.
"""
from alembic import op
import sqlalchemy as sa

revision = "0096"
down_revision = "0095"


def upgrade() -> None:
    op.add_column(
        "query_logs",
        sa.Column("status", sa.String(16), nullable=False, server_default="success"),
    )
    op.add_column(
        "query_logs",
        sa.Column("error_type", sa.String(64), nullable=True),
    )
    op.add_column(
        "query_logs",
        sa.Column("error_detail", sa.Text(), nullable=True),
    )
    op.create_index("ix_query_logs_status", "query_logs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_query_logs_status", table_name="query_logs")
    op.drop_column("query_logs", "error_detail")
    op.drop_column("query_logs", "error_type")
    op.drop_column("query_logs", "status")
