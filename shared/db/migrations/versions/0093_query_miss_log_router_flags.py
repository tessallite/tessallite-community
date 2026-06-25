"""Add router flags to query_miss_logs.

Stores predicates_json, has_unresolvable_where, and has_complex_sql so
downstream consumers (optimizer, pocket suggester) read structured data
from the miss log instead of re-parsing raw SQL.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0093"
down_revision = "0092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "query_miss_logs",
        sa.Column("predicates_json", JSONB, nullable=True),
    )
    op.add_column(
        "query_miss_logs",
        sa.Column(
            "has_unresolvable_where",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column(
        "query_miss_logs",
        sa.Column(
            "has_complex_sql",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("query_miss_logs", "has_complex_sql")
    op.drop_column("query_miss_logs", "has_unresolvable_where")
    op.drop_column("query_miss_logs", "predicates_json")
