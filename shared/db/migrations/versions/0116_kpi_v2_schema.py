"""KPI v2 schema — expression DSL, aggregation modes, thresholds, snapshots.

Adds ~25 new columns to the ``kpis`` table, creates the ``kpi_snapshots``
table for scheduled freeze results, and adds ``expose_kpis_inline`` and
``fiscal_year_start_month`` to ``models``.

Existing KPIs are backfilled: value_measure_id / goal_measure_id are
converted to expression/target notation so the v2 evaluation engine can
consume them. Legacy columns (value_measure_id, goal_measure_id,
status_expression, trend_expression, status_graphic, trend_graphic) are
retained as nullable for the compatibility window and will be dropped in
a future migration.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0116"
down_revision = "0115"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. New columns on ``kpis``
    # ------------------------------------------------------------------

    # Expression DSL
    op.add_column("kpis", sa.Column("kpi_type", sa.String(32), nullable=True))
    op.add_column("kpis", sa.Column("expression", sa.Text(), nullable=True))
    op.add_column("kpis", sa.Column(
        "calc_agg_mode", sa.String(32), nullable=False, server_default="automatic",
    ))
    op.add_column("kpis", sa.Column("inner_agg", sa.String(32), nullable=True))
    op.add_column("kpis", sa.Column("inner_grain", sa.String(128), nullable=True))
    op.add_column("kpis", sa.Column("outer_agg", sa.String(32), nullable=True))

    # Semi-additive
    op.add_column("kpis", sa.Column("at_grain", sa.String(32), nullable=True))
    op.add_column("kpis", sa.Column("non_additive_agg", sa.String(16), nullable=True))
    op.add_column("kpis", sa.Column(
        "carry_forward", sa.Boolean(), nullable=False, server_default=sa.text("false"),
    ))

    # Target
    op.add_column("kpis", sa.Column("target_type", sa.String(32), nullable=True))
    op.add_column("kpis", sa.Column("target_value", sa.Numeric(), nullable=True))
    op.add_column("kpis", sa.Column("target_measure_id", UUID(as_uuid=True), nullable=True))
    op.add_column("kpis", sa.Column("target_expression", sa.Text(), nullable=True))
    op.add_column("kpis", sa.Column("target_period", sa.String(32), nullable=True))

    # Direction and thresholds
    op.add_column("kpis", sa.Column(
        "direction", sa.String(32), nullable=False, server_default="higher_is_better",
    ))
    op.add_column("kpis", sa.Column("presentation_type", sa.String(32), nullable=True))
    op.add_column("kpis", sa.Column("presentation_meta", JSONB, nullable=True))

    # Trend
    op.add_column("kpis", sa.Column(
        "trend_period", sa.String(16), nullable=False, server_default="month",
    ))
    op.add_column("kpis", sa.Column(
        "trend_threshold", sa.Numeric(), nullable=False, server_default=sa.text("0.01"),
    ))
    op.add_column("kpis", sa.Column(
        "trend_sparkline_periods", sa.Integer(), nullable=False, server_default=sa.text("12"),
    ))

    # Formatting
    op.add_column("kpis", sa.Column("format_token", sa.String(32), nullable=True))
    op.add_column("kpis", sa.Column("format_custom", sa.String(128), nullable=True))
    op.add_column("kpis", sa.Column("unit_label", sa.String(32), nullable=True))
    op.add_column("kpis", sa.Column(
        "null_display_value", sa.String(32), nullable=False, server_default="N/A",
    ))

    # Hierarchy / composition
    op.add_column("kpis", sa.Column(
        "indicator_type", sa.String(16), nullable=False, server_default="none",
    ))
    op.add_column("kpis", sa.Column("evaluation_order", sa.Integer(), nullable=True))

    # Time dimension binding
    op.add_column("kpis", sa.Column("time_dimension_id", UUID(as_uuid=True), nullable=True))

    # Deployment
    op.add_column("kpis", sa.Column(
        "is_deployed", sa.Boolean(), nullable=False, server_default=sa.text("false"),
    ))
    op.add_column("kpis", sa.Column("deployed_at", sa.TIMESTAMP(timezone=True), nullable=True))

    # Snapshot schedule
    op.add_column("kpis", sa.Column("snapshot_frequency", sa.String(128), nullable=True))
    op.add_column("kpis", sa.Column(
        "snapshot_retention", sa.Integer(), nullable=False, server_default=sa.text("90"),
    ))

    # created_by (missing from v1)
    op.add_column("kpis", sa.Column("created_by", sa.String(255), nullable=True))

    # ------------------------------------------------------------------
    # 2. Backfill existing KPIs
    # ------------------------------------------------------------------
    # Convert value_measure_id -> expression.
    # We do a two-step approach: first set kpi_type for all, then build
    # expression strings from measure names.
    op.execute(sa.text("""
        UPDATE kpis SET kpi_type = 'simple_measure'
        WHERE kpi_type IS NULL
    """))
    # Build expression from value_measure: expression = 'measure("name")'
    op.execute(sa.text("""
        UPDATE kpis k
        SET expression = 'measure("' || m.name || '")'
        FROM measures m
        WHERE k.value_measure_id = m.id
          AND k.expression IS NULL
    """))
    # Build target from goal_measure: target_type = 'measure'
    op.execute(sa.text("""
        UPDATE kpis k
        SET target_type = 'measure',
            target_measure_id = k.goal_measure_id
        WHERE k.goal_measure_id IS NOT NULL
          AND k.target_type IS NULL
    """))
    # Set calc_agg_mode to aggregate_first for existing KPIs
    # (not automatic, to preserve deterministic behaviour)
    op.execute(sa.text("""
        UPDATE kpis SET calc_agg_mode = 'aggregate_first'
        WHERE calc_agg_mode = 'automatic'
          AND expression IS NOT NULL
    """))

    # ------------------------------------------------------------------
    # 3. ``kpi_snapshots`` table
    # ------------------------------------------------------------------
    op.create_table(
        "kpi_snapshots",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("kpi_id", UUID(as_uuid=True), sa.ForeignKey("kpis.id", ondelete="CASCADE"), nullable=False),
        sa.Column("snapshot_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("value", sa.Numeric(), nullable=True),
        sa.Column("target", sa.Numeric(), nullable=True),
        sa.Column("status", sa.Integer(), nullable=True),
        sa.Column("status_label", sa.String(64), nullable=True),
        sa.Column("trend_pct", sa.Numeric(), nullable=True),
        sa.Column("filters_applied", JSONB, nullable=True),
        sa.Column("evaluation_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_kpi_snapshots_kpi_at", "kpi_snapshots", ["kpi_id", sa.text("snapshot_at DESC")])

    # ------------------------------------------------------------------
    # 4. New columns on ``models``
    # ------------------------------------------------------------------
    op.add_column("models", sa.Column(
        "expose_kpis_inline", sa.Boolean(), nullable=False, server_default=sa.text("false"),
    ))
    op.add_column("models", sa.Column("fiscal_year_start_month", sa.Integer(), nullable=True))


def downgrade() -> None:
    # models
    op.drop_column("models", "fiscal_year_start_month")
    op.drop_column("models", "expose_kpis_inline")

    # kpi_snapshots
    op.drop_index("ix_kpi_snapshots_kpi_at", table_name="kpi_snapshots")
    op.drop_table("kpi_snapshots")

    # kpis — drop all v2 columns
    for col in [
        "created_by", "snapshot_retention", "snapshot_frequency",
        "deployed_at", "is_deployed", "time_dimension_id",
        "evaluation_order", "indicator_type",
        "null_display_value", "unit_label", "format_custom", "format_token",
        "trend_sparkline_periods", "trend_threshold", "trend_period",
        "presentation_meta", "presentation_type", "direction",
        "target_period", "target_expression", "target_measure_id",
        "target_value", "target_type",
        "carry_forward", "non_additive_agg", "at_grain",
        "outer_agg", "inner_grain", "inner_agg",
        "calc_agg_mode", "expression", "kpi_type",
    ]:
        op.drop_column("kpis", col)
