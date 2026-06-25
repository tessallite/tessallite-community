"""Add hidden_reason column to model_columns for join auto-hide tracking."""
from alembic import op
import sqlalchemy as sa

revision = "0114"
down_revision = "0113"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    # Check if column exists in the CURRENT schema (first in search_path).
    # Without table_schema filter, the query matches columns in ANY schema,
    # causing the check to incorrectly skip adding the column when another
    # tenant already has it.
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'model_columns' AND column_name = 'hidden_reason'"
        )
    )
    if result.fetchone() is None:
        op.add_column(
            "model_columns",
            sa.Column("hidden_reason", sa.String(16), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("model_columns", "hidden_reason")
