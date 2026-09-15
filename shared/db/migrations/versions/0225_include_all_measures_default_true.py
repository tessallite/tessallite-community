"""Default new models to include all measures; preserve existing values."""
import sqlalchemy as sa
from alembic import op

revision = "0225"
down_revision = "0223"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "models", "include_all_measures", existing_type=sa.Boolean(),
        existing_nullable=False, server_default=sa.text("true"),
    )


def downgrade():
    op.alter_column(
        "models", "include_all_measures", existing_type=sa.Boolean(),
        existing_nullable=False, server_default=sa.text("false"),
    )
