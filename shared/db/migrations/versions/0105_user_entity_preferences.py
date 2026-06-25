"""Create user_entity_preferences table for favourites and recently used tracking."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0105"
down_revision = "0104"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_entity_preferences",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.String(255), nullable=False, index=True),
        sa.Column("model_id", UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", UUID(as_uuid=True), nullable=False),
        sa.Column("preference_type", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_user_entity_pref_lookup",
        "user_entity_preferences",
        ["user_id", "model_id", "entity_type", "preference_type"],
    )
    op.create_unique_constraint(
        "uq_user_entity_pref",
        "user_entity_preferences",
        ["user_id", "model_id", "entity_type", "entity_id", "preference_type"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_user_entity_pref", "user_entity_preferences", type_="unique")
    op.drop_index("ix_user_entity_pref_lookup", table_name="user_entity_preferences")
    op.drop_table("user_entity_preferences")
