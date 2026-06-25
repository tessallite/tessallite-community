"""Add declared primary-key metadata to semantic model columns.

Looker generated views require reliable ``primary_key: yes`` declarations for
join inference and symmetric aggregate behavior. This flag is modeller-owned
metadata and is exported in model snapshots.
"""
from alembic import op
import sqlalchemy as sa

revision = "0112"
down_revision = "0111"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_columns",
        sa.Column(
            "is_primary_key",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("model_columns", "is_primary_key")
