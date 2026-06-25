"""Create kpi_latest materialised metadata table.

The ``kpi_latest`` table stores the most recent evaluation result for each
KPI. It is upserted by the scheduler snapshot sweep and the evaluate-batch
endpoint, enabling BI clients to query KPI values directly via the
``Model$KPIs`` virtual table registered in the JDBC catalogue.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0117"
down_revision = "0116"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kpi_latest",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "model_id", UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kpi_id", UUID(as_uuid=True),
            sa.ForeignKey("kpis.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kpi_name", sa.String(255), nullable=False),
        sa.Column("value", sa.Numeric(), nullable=True),
        sa.Column("target", sa.Numeric(), nullable=True),
        sa.Column("status", sa.Integer(), nullable=True),
        sa.Column("status_label", sa.String(64), nullable=True),
        sa.Column("trend_pct", sa.Numeric(), nullable=True),
        sa.Column("formatted_value", sa.String(128), nullable=True),
        sa.Column(
            "evaluated_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("model_id", "kpi_id", name="uq_kpi_latest_model_kpi"),
    )
    op.create_index("ix_kpi_latest_model", "kpi_latest", ["model_id"])


def downgrade() -> None:
    op.drop_index("ix_kpi_latest_model", table_name="kpi_latest")
    op.drop_table("kpi_latest")
