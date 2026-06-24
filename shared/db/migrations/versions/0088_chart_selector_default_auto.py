"""Change chart_type_selector default from 'none' to 'auto' and update existing rows."""
from alembic import op

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE project_agent_configs "
        "SET chart_type_selector = 'auto' "
        "WHERE chart_type_selector = 'none'"
    )
    op.alter_column(
        "project_agent_configs",
        "chart_type_selector",
        server_default="auto",
    )


def downgrade() -> None:
    op.alter_column(
        "project_agent_configs",
        "chart_type_selector",
        server_default="none",
    )
