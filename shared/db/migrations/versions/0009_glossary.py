"""Glossary v1 — entries, synonyms, attachments.

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-13

Phase 3 of the semantic-layer plan (docs/architecture/architecture_semantic-layer.md). Adds the
business glossary that modellers curate (with an LLM bootstrap helper)
and that downstream phases distribute through the public glossary HTML
page (Phase 4) and into the gateway descriptions (Phase 4) so non-
technical Excel users see the curated text in their pivot field lists.

New tables:
  - glossary_entry       — one row per glossary term, versioned, with
                           provenance (llm | user | llm_approved).
  - glossary_synonym     — alternate names per entry, many-to-one.
  - glossary_attachment  — links an entry to a Dimension, Measure, or
                           ModelColumn (or to no specific object for
                           future business-concept entries).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "0009"
down_revision = "0008"
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
    if not _table_exists("glossary_entry"):
        op.create_table(
            "glossary_entry",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "model_id",
                UUID(as_uuid=True),
                sa.ForeignKey("models.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("term", sa.String(length=255), nullable=False),
            sa.Column("definition", sa.Text(), nullable=False),
            sa.Column("context_notes", sa.Text(), nullable=True),
            sa.Column(
                "source",
                sa.String(length=16),
                nullable=False,
                comment="llm | user | llm_approved",
            ),
            sa.Column(
                "status",
                sa.String(length=16),
                nullable=False,
                server_default="pending_review",
                comment="pending_review | approved | rejected",
            ),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column(
                "superseded_by",
                UUID(as_uuid=True),
                sa.ForeignKey("glossary_entry.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "created_by",
                UUID(as_uuid=True),
                nullable=True,
                comment="user id of the modeller who authored or approved this version",
            ),
            sa.Column("proposed_is_hidden", sa.Boolean(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )
        op.create_index(
            "idx_glossary_entry_model_status",
            "glossary_entry",
            ["model_id", "status"],
        )
        op.create_index(
            "idx_glossary_entry_model_term",
            "glossary_entry",
            ["model_id", "term"],
        )

    if not _table_exists("glossary_synonym"):
        op.create_table(
            "glossary_synonym",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "entry_id",
                UUID(as_uuid=True),
                sa.ForeignKey("glossary_entry.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("synonym", sa.String(length=255), nullable=False),
        )
        op.create_index(
            "idx_glossary_synonym_entry",
            "glossary_synonym",
            ["entry_id"],
        )

    if not _table_exists("glossary_attachment"):
        op.create_table(
            "glossary_attachment",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "entry_id",
                UUID(as_uuid=True),
                sa.ForeignKey("glossary_entry.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "target_type",
                sa.String(length=16),
                nullable=False,
                comment="dimension | measure | column | concept",
            ),
            sa.Column("target_id", UUID(as_uuid=True), nullable=True),
        )
        op.create_index(
            "idx_glossary_attachment_target",
            "glossary_attachment",
            ["target_type", "target_id"],
        )


def downgrade() -> None:
    if _table_exists("glossary_attachment"):
        op.drop_index("idx_glossary_attachment_target", table_name="glossary_attachment")
        op.drop_table("glossary_attachment")
    if _table_exists("glossary_synonym"):
        op.drop_index("idx_glossary_synonym_entry", table_name="glossary_synonym")
        op.drop_table("glossary_synonym")
    if _table_exists("glossary_entry"):
        op.drop_index("idx_glossary_entry_model_term", table_name="glossary_entry")
        op.drop_index("idx_glossary_entry_model_status", table_name="glossary_entry")
        op.drop_table("glossary_entry")
