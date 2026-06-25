"""Add queries_served and avg_latency_ms_improvement to ai_aggregate_recommendations."""
from alembic import op
import sqlalchemy as sa

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_aggregate_recommendations",
        sa.Column("queries_served", sa.Integer(), nullable=True),
    )
    op.add_column(
        "ai_aggregate_recommendations",
        sa.Column("avg_latency_ms_improvement", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ai_aggregate_recommendations", "queries_served")
    op.drop_column("ai_aggregate_recommendations", "avg_latency_ms_improvement")
