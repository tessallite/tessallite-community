"""Add downstream_assets, downstream_asset_columns, and gateway_query_references tables."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "downstream_assets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "model_id",
            UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("asset_type", sa.String(32), nullable=False),
        sa.Column("asset_name", sa.String(512), nullable=False),
        sa.Column("asset_url", sa.Text(), nullable=True),
        sa.Column("owner", sa.String(255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "downstream_asset_columns",
        sa.Column(
            "asset_id",
            UUID(as_uuid=True),
            sa.ForeignKey("downstream_assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "model_column_id",
            UUID(as_uuid=True),
            sa.ForeignKey("model_columns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("asset_id", "model_column_id"),
    )

    op.create_table(
        "gateway_query_references",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "model_id",
            UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("queried_table", sa.String(512), nullable=False),
        sa.Column("query_user", sa.String(255), nullable=True),
        sa.Column("query_text_hash", sa.String(64), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_table("gateway_query_references")
    op.drop_table("downstream_asset_columns")
    op.drop_table("downstream_assets")
