"""Versioning + deploy pointer.

Revision ID: 0021
Revises: 0020
Create Date: 2026-04-18

Phase 4 of the deploy/versioning bundle (work/action-plan-deploy-versioning.md).

Adds:

  - ``model_versions`` table — one row per Save click. Each row holds an
    immutable JSONB snapshot of the model's full state (tables, columns,
    joins, dimensions, measures, hierarchies, UDAs, aggregate
    definitions, refresh policies, AI scheduler config, model_settings,
    canvas_layout). Per-tenant; on tenant DB.

  - ``models.deployed_version_id`` — nullable FK into model_versions.
    When non-null, the model is deployed: query routing, XMLA gateway,
    scheduler, optimizer all read the snapshot; when null the model is
    metadata-only and routing returns 409.

  - ``models.last_deployed_at`` — for the projects-and-models tag.

Per F-2, the deploy pointer is the sole gating flag — no ``is_active`` /
``aggregations_enabled`` cutover lives in this bundle. ``aggregations_enabled``
(a prior, pre-deploy flag) keeps its narrower post-routing meaning of
"pause aggregate refresh" and is left untouched.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = :table AND column_name = :column"
        ),
        {"table": table, "column": column},
    )
    return result.scalar() is not None


def _table_exists(table: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = :t"
        ),
        {"t": table},
    )
    return result.scalar() is not None


def upgrade() -> None:
    if not _table_exists("model_versions"):
        op.create_table(
            "model_versions",
            sa.Column(
                "id",
                UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "model_id",
                UUID(as_uuid=True),
                sa.ForeignKey("models.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("version_number", sa.Integer, nullable=False),
            sa.Column("snapshot_json", JSONB, nullable=False),
            sa.Column("summary", sa.Text),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column("created_by", sa.String(255), nullable=False),
            sa.UniqueConstraint("model_id", "version_number"),
        )
        op.create_index(
            "idx_model_versions_model",
            "model_versions",
            ["model_id", sa.text("version_number DESC")],
        )

    if not _column_exists("models", "deployed_version_id"):
        op.add_column(
            "models",
            sa.Column(
                "deployed_version_id",
                UUID(as_uuid=True),
                sa.ForeignKey("model_versions.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
    if not _column_exists("models", "last_deployed_at"):
        op.add_column(
            "models",
            sa.Column(
                "last_deployed_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )


def downgrade() -> None:
    if _column_exists("models", "last_deployed_at"):
        op.drop_column("models", "last_deployed_at")
    if _column_exists("models", "deployed_version_id"):
        op.drop_column("models", "deployed_version_id")
    if _table_exists("model_versions"):
        op.drop_index("idx_model_versions_model", table_name="model_versions")
        op.drop_table("model_versions")
