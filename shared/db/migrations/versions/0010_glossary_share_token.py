"""Glossary share-token registry (revocable public links).

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-13

Phase 4 of the semantic-layer plan (docs/architecture/architecture_semantic-layer.md). Closes the
security gap where public glossary JWTs were unrevokable: every issued
token now has a row in `glossary_share_token` that records the token id
(jti) and a revoked_at timestamp. The public payload endpoints check
this table and return 404 for any token whose row was revoked.

New table:
  - glossary_share_token — one row per issued public share token.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = :name"
        ),
        {"name": name},
    )
    return result.scalar() is not None


def upgrade() -> None:
    if not _table_exists("glossary_share_token"):
        op.create_table(
            "glossary_share_token",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "model_id",
                UUID(as_uuid=True),
                sa.ForeignKey("models.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "created_by",
                UUID(as_uuid=True),
                nullable=True,
                comment="modeller who issued the token",
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "revoked_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
        op.create_index(
            "idx_glossary_share_token_model",
            "glossary_share_token",
            ["model_id"],
        )


def downgrade() -> None:
    if _table_exists("glossary_share_token"):
        op.drop_index(
            "idx_glossary_share_token_model", table_name="glossary_share_token"
        )
        op.drop_table("glossary_share_token")
