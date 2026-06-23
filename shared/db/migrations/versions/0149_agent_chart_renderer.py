"""Add agent chart renderer setting.

Revision ID: 0149
Revises: 0148
Create Date: 2026-06-21
"""
from alembic import op
import sqlalchemy as sa

revision = "0149"
down_revision = "0148"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "project_agent_configs" not in table_names:
        return

    columns = {col["name"] for col in inspector.get_columns("project_agent_configs")}
    if "chart_renderer" not in columns:
        op.add_column(
            "project_agent_configs",
            sa.Column(
                "chart_renderer",
                sa.Text(),
                nullable=False,
                server_default="echarts",
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "project_agent_configs" not in table_names:
        return

    columns = {col["name"] for col in inspector.get_columns("project_agent_configs")}
    if "chart_renderer" in columns:
        op.drop_column("project_agent_configs", "chart_renderer")
