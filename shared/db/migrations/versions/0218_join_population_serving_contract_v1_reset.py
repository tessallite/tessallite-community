"""Invalidate materialised artifacts for the G3 join-population contract.

Bug-8621: G3 changes the deployed FROM graph.  A pre-G3 aggregate, pocket, or
Named Query can therefore have a fresh-looking physical table whose row
population differs from the source route.  This data-only migration bumps the
deploy epoch for every currently deployed model and applies the same
``artifact_incompatible_sql`` predicate used by deploy/revert.  The epoch is
the existing build/serve contract, so old artifacts cannot remain fresh and
servable and no manual-refresh assumption is required.

Revision ID: 0218
Revises: 0217
Create Date: 2026-08-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from shared.artifact_version_gate import artifact_incompatible_sql
from shared.semantic.join_population_serving import (
    JOIN_POPULATION_SERVING_CONTRACT_VERSION,
)

revision = "0218"
down_revision = "0217"
branch_labels = None
depends_on = None

_REBUILD_REASON = (
    "Rebuild required: G3 join-population serving contract "
    f"v{JOIN_POPULATION_SERVING_CONTRACT_VERSION}"
)


def _table(name: str, *columns: str):
    """Build a dependency-light SQLAlchemy table for migration DML."""
    return sa.table(name, *(sa.column(column) for column in columns))


def _stale_artifacts(bind, model_id, version_id, epoch, table_names: set[str]) -> None:
    """Use the runtime artifact incompatibility predicate for every family."""
    if "aggregate_definitions" in table_names:
        aggregates = _table(
            "aggregate_definitions",
            "model_id", "status", "is_stale", "built_for_version_id",
            "built_for_epoch", "retired_at", "invalid_reason",
        )
        bind.execute(
            sa.update(aggregates)
            .where(
                aggregates.c.model_id == model_id,
                aggregates.c.status == "active",
                aggregates.c.is_stale.is_(False),
                aggregates.c.retired_at.is_(None),
                artifact_incompatible_sql(
                    aggregates.c.built_for_version_id,
                    aggregates.c.built_for_epoch,
                    version_id,
                    epoch,
                ),
            )
            .values(is_stale=True, invalid_reason=_REBUILD_REASON)
        )

    if "pocket_definitions" in table_names:
        pockets = _table(
            "pocket_definitions",
            "model_id", "status", "retired_at", "built_for_version_id",
            "built_for_epoch", "failure_reason",
        )
        bind.execute(
            sa.update(pockets)
            .where(
                pockets.c.model_id == model_id,
                pockets.c.status == "fresh",
                pockets.c.retired_at.is_(None),
                artifact_incompatible_sql(
                    pockets.c.built_for_version_id,
                    pockets.c.built_for_epoch,
                    version_id,
                    epoch,
                ),
            )
            .values(status="stale", failure_reason=_REBUILD_REASON)
        )

    if {"named_query_artifacts", "named_queries"} <= table_names:
        named_queries = _table("named_queries", "id", "model_id")
        artifacts = _table(
            "named_query_artifacts",
            "named_query_id", "status", "retired_at", "built_for_version_id",
            "built_for_epoch", "failure_reason",
        )
        model_named_queries = sa.select(named_queries.c.id).where(
            named_queries.c.model_id == model_id,
        )
        bind.execute(
            sa.update(artifacts)
            .where(
                artifacts.c.named_query_id.in_(model_named_queries),
                artifacts.c.status == "fresh",
                artifacts.c.retired_at.is_(None),
                artifact_incompatible_sql(
                    artifacts.c.built_for_version_id,
                    artifacts.c.built_for_epoch,
                    version_id,
                    epoch,
                ),
            )
            .values(status="stale", failure_reason=_REBUILD_REASON)
        )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "models" not in table_names:
        return

    models = _table("models", "id", "deployed_version_id", "deploy_epoch")
    deployed = bind.execute(
        sa.select(
            models.c.id,
            models.c.deployed_version_id,
            models.c.deploy_epoch,
        ).where(models.c.deployed_version_id.is_not(None))
    ).mappings().all()

    for row in deployed:
        new_epoch = int(row["deploy_epoch"] or 0) + 1
        bind.execute(
            sa.update(models)
            .where(models.c.id == row["id"])
            .values(deploy_epoch=new_epoch)
        )
        _stale_artifacts(
            bind,
            row["id"],
            row["deployed_version_id"],
            new_epoch,
            table_names,
        )


def downgrade() -> None:
    # Never re-trust a pre-G3 artifact. Rebuild under the current contract.
    pass
