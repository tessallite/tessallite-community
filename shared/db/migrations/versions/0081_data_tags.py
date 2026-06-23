"""Add data_tags, data_tag_columns, and persona_tag_restrictions tables."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "data_tags",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "model_id",
            UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("tag_name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("model_id", "tag_name", name="uq_data_tag_model_name"),
    )

    op.create_table(
        "data_tag_columns",
        sa.Column(
            "tag_id",
            UUID(as_uuid=True),
            sa.ForeignKey("data_tags.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "model_column_id",
            UUID(as_uuid=True),
            sa.ForeignKey("model_columns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tag_id", "model_column_id"),
    )

    op.create_table(
        "persona_tag_restrictions",
        sa.Column(
            "persona_id",
            UUID(as_uuid=True),
            sa.ForeignKey("personas.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "data_tag_id",
            UUID(as_uuid=True),
            sa.ForeignKey("data_tags.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("persona_id", "data_tag_id"),
    )


def downgrade() -> None:
    op.drop_table("persona_tag_restrictions")
    op.drop_table("data_tag_columns")
    op.drop_table("data_tags")
