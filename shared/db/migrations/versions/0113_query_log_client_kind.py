"""Add BI client telemetry label to query logs."""
from alembic import op
import sqlalchemy as sa

revision = "0113"
down_revision = "0112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "query_logs",
        sa.Column("client_kind", sa.String(length=32), nullable=True),
    )
    op.create_index("ix_query_logs_client_kind", "query_logs", ["client_kind"])


def downgrade() -> None:
    op.drop_index("ix_query_logs_client_kind", table_name="query_logs")
    op.drop_column("query_logs", "client_kind")
