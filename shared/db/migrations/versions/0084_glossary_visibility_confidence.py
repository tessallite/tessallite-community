"""Add visibility and confidence columns to glossary_entry."""
from alembic import op
import sqlalchemy as sa

revision = "0084"
down_revision = "0083"
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
    if not _table_exists("glossary_entry"):
        return
    op.add_column(
        "glossary_entry",
        sa.Column("visibility", sa.String(16), nullable=True),
    )
    op.add_column(
        "glossary_entry",
        sa.Column("confidence", sa.String(16), nullable=True),
    )


def downgrade() -> None:
    if not _table_exists("glossary_entry"):
        return
    op.drop_column("glossary_entry", "confidence")
    op.drop_column("glossary_entry", "visibility")
