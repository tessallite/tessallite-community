"""Add replacement_id FK to named_sets and kpis for deprecation linking."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0104"
down_revision = "0103"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("named_sets", sa.Column("replacement_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_named_sets_replacement_id", "named_sets",
        "named_sets", ["replacement_id"], ["id"], ondelete="SET NULL",
    )
    op.add_column("kpis", sa.Column("replacement_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_kpis_replacement_id", "kpis",
        "kpis", ["replacement_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_kpis_replacement_id", "kpis", type_="foreignkey")
    op.drop_column("kpis", "replacement_id")
    op.drop_constraint("fk_named_sets_replacement_id", "named_sets", type_="foreignkey")
    op.drop_column("named_sets", "replacement_id")
