"""Add chart config fields to project_agent_configs and rendered_output to agent_turns."""
from alembic import op
import sqlalchemy as sa

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("project_agent_configs",
        sa.Column("agent_output_format", sa.Text(), nullable=False, server_default="json"))
    op.add_column("project_agent_configs",
        sa.Column("chart_type_selector", sa.Text(), nullable=False, server_default="none"))
    op.add_column("project_agent_configs",
        sa.Column("chart_max_rows", sa.Integer(), nullable=False, server_default="500"))
    op.add_column("project_agent_configs",
        sa.Column("chart_color_palette", sa.Text(), nullable=False, server_default="default"))
    op.add_column("project_agent_configs",
        sa.Column("chart_size", sa.Text(), nullable=False, server_default="md"))
    op.add_column("project_agent_configs",
        sa.Column("include_data_table", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("agent_turns",
        sa.Column("rendered_output", sa.Text(), nullable=True))


def downgrade() -> None:
    for col in ["agent_output_format", "chart_type_selector", "chart_max_rows",
                "chart_color_palette", "chart_size", "include_data_table"]:
        op.drop_column("project_agent_configs", col)
    op.drop_column("agent_turns", "rendered_output")
