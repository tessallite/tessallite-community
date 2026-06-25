"""Add ON DELETE CASCADE to aggregate_refresh_runs.aggregate_definition_id FK."""
from alembic import op
import sqlalchemy as sa

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM information_schema.tables"
            "  WHERE table_name = :t AND table_schema = current_schema()"
            ")"
        ),
        {"t": name},
    )
    return result.scalar()


def upgrade() -> None:
    if not _table_exists("aggregate_refresh_runs"):
        return
    op.drop_constraint(
        "aggregate_refresh_runs_aggregate_definition_id_fkey",
        "aggregate_refresh_runs",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "aggregate_refresh_runs_aggregate_definition_id_fkey",
        "aggregate_refresh_runs",
        "aggregate_definitions",
        ["aggregate_definition_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    if not _table_exists("aggregate_refresh_runs"):
        return
    op.drop_constraint(
        "aggregate_refresh_runs_aggregate_definition_id_fkey",
        "aggregate_refresh_runs",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "aggregate_refresh_runs_aggregate_definition_id_fkey",
        "aggregate_refresh_runs",
        "aggregate_definitions",
        ["aggregate_definition_id"],
        ["id"],
    )
