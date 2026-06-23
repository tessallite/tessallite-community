"""Add glossary_max_distinct to models and sample_values to glossary_entry."""
from alembic import op
import sqlalchemy as sa

revision = "0089"
down_revision = "0088"
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
    return bool(result.scalar())


def upgrade() -> None:
    op.execute(sa.text(
        "ALTER TABLE models "
        "ADD COLUMN IF NOT EXISTS glossary_max_distinct INTEGER NOT NULL DEFAULT 50"
    ))
    if _table_exists("glossary_entry"):
        op.execute(sa.text(
            "ALTER TABLE glossary_entry "
            "ADD COLUMN IF NOT EXISTS sample_values JSONB"
        ))


def downgrade() -> None:
    if _table_exists("glossary_entry"):
        op.drop_column("glossary_entry", "sample_values")
    op.drop_column("models", "glossary_max_distinct")
