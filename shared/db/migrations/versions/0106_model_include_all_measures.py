"""Add include_all_measures flag to models table.

When True (default), the optimizer includes ALL model measures in new aggregates,
not just the ones observed in query misses. Eliminates an entire class of misses
at the cost of wider aggregate tables.
"""
from alembic import op
import sqlalchemy as sa

revision = "0106"
down_revision = "0105"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "models",
        sa.Column(
            "include_all_measures",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("models", "include_all_measures")
