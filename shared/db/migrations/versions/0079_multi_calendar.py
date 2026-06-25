"""Add calendar_type to calendar_tables and variant resolution columns to measures."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "calendar_tables",
        sa.Column("calendar_type", sa.String(32), nullable=False, server_default="standard"),
    )

    op.add_column(
        "measures",
        sa.Column("hierarchy_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_measure_hierarchy",
        "measures",
        "hierarchy_definitions",
        ["hierarchy_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column(
        "measures",
        sa.Column("resolved_calendar_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_measure_resolved_cal",
        "measures",
        "calendar_tables",
        ["resolved_calendar_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column(
        "measures",
        sa.Column("resolved_date_col_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_measure_resolved_date_col",
        "measures",
        "model_columns",
        ["resolved_date_col_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column(
        "measures",
        sa.Column("date_dimension_column_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_measure_date_dim_col",
        "measures",
        "model_columns",
        ["date_dimension_column_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Data migration: populate resolved_calendar_id on variant measures
    # that have calendar_model_table_id set. Walk the legacy chain:
    # measures.calendar_model_table_id → model_tables.calendar_table_id
    op.execute(sa.text("""
        UPDATE measures m
        SET resolved_calendar_id = mt.calendar_table_id
        FROM model_tables mt
        WHERE m.calendar_model_table_id = mt.id
          AND mt.calendar_table_id IS NOT NULL
          AND m.variant_kind IS NOT NULL
          AND m.resolved_calendar_id IS NULL
    """))

    # Populate resolved_date_col_id from the calendar table's date_column
    # by finding the matching model_column on the calendar alias table
    op.execute(sa.text("""
        UPDATE measures m
        SET resolved_date_col_id = mc.id
        FROM model_tables mt
        JOIN calendar_tables ct ON ct.id = mt.calendar_table_id
        JOIN model_columns mc ON mc.model_table_id = mt.id
                              AND mc.column_name = ct.date_column
        WHERE m.calendar_model_table_id = mt.id
          AND m.variant_kind IS NOT NULL
          AND m.resolved_date_col_id IS NULL
    """))


def downgrade() -> None:
    op.drop_constraint("fk_measure_date_dim_col", "measures")
    op.drop_column("measures", "date_dimension_column_id")

    op.drop_constraint("fk_measure_resolved_date_col", "measures")
    op.drop_column("measures", "resolved_date_col_id")

    op.drop_constraint("fk_measure_resolved_cal", "measures")
    op.drop_column("measures", "resolved_calendar_id")

    op.drop_constraint("fk_measure_hierarchy", "measures")
    op.drop_column("measures", "hierarchy_id")

    op.drop_column("calendar_tables", "calendar_type")
