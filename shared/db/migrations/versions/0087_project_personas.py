"""Add project_personas, model scopes, and conversation persona_id.

Revision ID: 0087
Revises: 0086
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_personas",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
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
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.UniqueConstraint("project_id", "name", name="uq_project_persona_name"),
        sa.UniqueConstraint("project_id", "slug", name="uq_project_persona_slug"),
    )

    op.create_table(
        "project_persona_model_scopes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_persona_id",
            UUID(as_uuid=True),
            sa.ForeignKey("project_personas.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "model_id",
            UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "included_measure_ids",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "included_dimension_ids",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "project_persona_id",
            "model_id",
            name="uq_project_persona_model_scope",
        ),
    )

    op.add_column(
        "agent_conversations",
        sa.Column(
            "persona_id",
            UUID(as_uuid=True),
            sa.ForeignKey("project_personas.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_conversations", "persona_id")
    op.drop_table("project_persona_model_scopes")
    op.drop_table("project_personas")
