"""Add attribute_source/attribute_claim_name to row_security_rules;
add security_rules_applied to query_logs.

Revision ID: 0066
Revises: 0065
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "row_security_rules",
        sa.Column(
            "attribute_source",
            sa.Text(),
            nullable=False,
            server_default="jwt_role",
        ),
    )
    op.add_column(
        "row_security_rules",
        sa.Column("attribute_claim_name", sa.Text(), nullable=True),
    )
    op.add_column(
        "query_logs",
        sa.Column("security_rules_applied", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("query_logs", "security_rules_applied")
    op.drop_column("row_security_rules", "attribute_claim_name")
    op.drop_column("row_security_rules", "attribute_source")
