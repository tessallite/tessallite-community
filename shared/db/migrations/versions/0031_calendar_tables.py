"""Calendar tables for time-intelligence variants.

Revision ID: 0031
Revises: 0030
Create Date: 2026-04-22

Phase 2 Step 1 of work/phase-2-time-intelligence-action-plan.md.
Adds the catalog entry that the time-variant rewriter joins against to
resolve period semantics (PY, YTD, QTD, MTD, etc).

Decision (open-questions Q4 + follow-up clarification):
  - Calendar binding is per data source (not per project connection),
    so different models within a project may use different calendar
    layouts and avoid hybrid cross-source joins at variant time.
  - Tessallite never autosynthesises a calendar in dialect-bound SQL;
    the modeller either points at an existing physical table or uses
    Tessallite's provisioning helper (Phase 2 Step 2) to populate one.

Adds:
  calendar_tables (one row per registered calendar in a data source)
  data_sources.calendar_table_id  uuid  nullable, FK -> calendar_tables.id

``period_columns`` (year_column, quarter_column, month_column, week_column,
day_column, half_column, date_column) are nullable individually; at least
one of {year_column, date_column} must be present (enforced at the
service / API boundary, not in the DB).

``dialect`` records the SQL dialect of the source the calendar lives in
(``postgresql``, ``bigquery``, ``hadoop_spark``) so the rewriter knows
which date-arithmetic forms to emit when joining against the calendar.

``autocreated`` records whether Tessallite populated the table itself
(true) or the modeller registered an existing one (false).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = :table AND column_name = :column"
        ),
        {"table": table, "column": column},
    )
    return result.scalar() is not None


def _table_exists(table: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() "
            "AND table_name = :table"
        ),
        {"table": table},
    )
    return result.scalar() is not None


def upgrade() -> None:
    if not _table_exists("calendar_tables"):
        op.create_table(
            "calendar_tables",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "data_source_id",
                UUID(as_uuid=True),
                sa.ForeignKey("data_sources.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("table_name", sa.String(512), nullable=False),
            sa.Column("dialect", sa.String(32), nullable=False),
            sa.Column("date_column", sa.String(255), nullable=True),
            sa.Column("year_column", sa.String(255), nullable=True),
            sa.Column("half_column", sa.String(255), nullable=True),
            sa.Column("quarter_column", sa.String(255), nullable=True),
            sa.Column("month_column", sa.String(255), nullable=True),
            sa.Column("week_column", sa.String(255), nullable=True),
            sa.Column("day_column", sa.String(255), nullable=True),
            sa.Column(
                "autocreated",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
                onupdate=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint("data_source_id", "table_name", name="uq_calendar_data_source_table"),
        )

    if not _column_exists("data_sources", "calendar_table_id"):
        op.add_column(
            "data_sources",
            sa.Column(
                "calendar_table_id",
                UUID(as_uuid=True),
                sa.ForeignKey("calendar_tables.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )


def downgrade() -> None:
    if _column_exists("data_sources", "calendar_table_id"):
        op.drop_column("data_sources", "calendar_table_id")
    if _table_exists("calendar_tables"):
        op.drop_table("calendar_tables")
