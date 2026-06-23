"""Add builder_definition, list_type, governance fields to named_sets and kpis."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("named_sets", sa.Column("builder_definition", JSONB, nullable=True))
    op.add_column("named_sets", sa.Column("list_type", sa.String(32), nullable=True, server_default="advanced_mdx"))
    op.add_column("named_sets", sa.Column("certification_status", sa.String(32), nullable=False, server_default="draft"))
    op.add_column("named_sets", sa.Column("owner_user_id", sa.String(255), nullable=True))

    op.add_column("kpis", sa.Column("certification_status", sa.String(32), nullable=False, server_default="draft"))
    op.add_column("kpis", sa.Column("owner_user_id", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("kpis", "owner_user_id")
    op.drop_column("kpis", "certification_status")

    op.drop_column("named_sets", "owner_user_id")
    op.drop_column("named_sets", "certification_status")
    op.drop_column("named_sets", "list_type")
    op.drop_column("named_sets", "builder_definition")
