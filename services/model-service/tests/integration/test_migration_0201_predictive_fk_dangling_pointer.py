"""Real-async DB guard for migration 0201's data precondition.

Migration 0201 adds the first-ever foreign key on
``models.predictive_built_for_version_id``. Postgres validates a plain
``ADD CONSTRAINT`` IMMEDIATELY against existing rows, so the migration is only
applicable if no row already points at a deleted ``model_versions`` id.

Rows CAN already point at a deleted version, on the default configuration:

* ``api/versions.py::revert_to_version`` deletes every version NEWER than the
  target, so a model stamped at v5 and reverted to v3 keeps a pointer to v5.
* ``api/versions.py::_prune_old_versions`` retains newest-N plus
  ``deployed_version_id`` only — ``predictive_built_for_version_id`` is not in
  the retained set, so a prune can delete the stamped version.

Without the pre-flight ``UPDATE ... SET NULL`` the whole tenant chain aborts
with a ForeignKeyViolation and the schema is stranded at 0200. That is not
hypothetical: it was reproduced on the seeded ``acme-demo`` tenant, whose
model ``modell`` carried exactly such a dangling pointer.

Test escape: the migration was originally verified only against disposable
EMPTY tenant schemas, which by construction have no revert/prune history and
therefore cannot exercise a data-shape precondition. This test seeds the
precondition explicitly.
Guard: this module.
Tier: T1 contract (a migration must apply to any supported persisted state).

Skipped unless ``TESSALLITE_VERSIONING_DB_URL`` (or the importer-harness URL)
points at a reachable Postgres.

Run:
    cd tessallite/services/model-service
    TESSALLITE_VERSIONING_DB_URL=postgresql+asyncpg://user:pw@localhost:5432/db \
      pytest tests/integration/test_migration_0201_predictive_fk_dangling_pointer.py -v
"""
from __future__ import annotations

import importlib.util
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shared.db.models import TenantBase

pytestmark = [pytest.mark.integration]

_DB_URL = os.environ.get("TESSALLITE_VERSIONING_DB_URL") or os.environ.get(
    "TESSALLITE_IMPORTER_REHYDRATION_DB_URL"
)

_MIGRATION = (
    Path(__file__).resolve().parents[4]
    / "shared" / "db" / "migrations" / "versions"
    / "0201_predictive_built_version_id_foreign_key.py"
)

_FK_NAME = "fk_models_predictive_built_for_version_id_model_versions"

pytestmark.append(
    pytest.mark.skipif(not _DB_URL, reason="no Postgres URL configured")
)


def _load_migration():
    """Load the revision the way Alembic does (no ``sys.modules`` entry)."""
    spec = importlib.util.spec_from_file_location("m0201_probe", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_upgrade(connection) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = _load_migration()
    ctx = MigrationContext.configure(connection)
    with Operations.context(ctx):
        module.upgrade()


@asynccontextmanager
async def _isolated_schema() -> AsyncIterator[tuple[object, str]]:
    schema = f"m0201_{uuid.uuid4().hex}"
    boot = create_async_engine(_DB_URL, future=True)
    async with boot.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'SET search_path TO "{schema}"'))
        await conn.run_sync(TenantBase.metadata.create_all)
        # Defensive: the ORM deliberately does NOT declare this FK (see the
        # comment at shared/db/models.py on predictive_built_for_version_id —
        # declaring it makes SQLAlchemy try to wire an implicit relationship to
        # ModelVersion at mapper-configuration time). So this DROP is inert
        # today. It is kept so the test still reproduces the pre-0201 state if
        # that convention ever changes; without it the migration would find the
        # FK already present and return early, asserting nothing.
        await conn.execute(
            text(f'ALTER TABLE "{schema}".models DROP CONSTRAINT IF EXISTS {_FK_NAME}')
        )
    await boot.dispose()

    engine = create_async_engine(_DB_URL, future=True)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_search_path(dbapi_connection, _record):  # noqa: ANN001
        cur = dbapi_connection.cursor()
        cur.execute(f'SET search_path TO "{schema}"')
        cur.close()

    try:
        yield engine, schema
    finally:
        drop = create_async_engine(_DB_URL, future=True)
        async with drop.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await drop.dispose()
        await engine.dispose()


async def _seed(engine, *, dangling: bool) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed one project + model through the ORM.

    The ORM is used rather than raw INSERTs because several columns on
    ``projects``/``models`` are NOT NULL with a PYTHON-side default and no
    server default; raw SQL would have to enumerate them and would silently
    rot as the schema grows.

    ``dangling`` decides whether the predictive pointer references a version
    row that exists.
    """
    from shared.db.models import Model, ModelVersion, Project

    version_id = uuid.uuid4()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        project = Project(slug=f"p{uuid.uuid4().hex[:8]}", display_name="P")
        session.add(project)
        await session.flush()

        model = Model(
            project_id=project.id,
            slug=f"m{uuid.uuid4().hex[:8]}",
            display_name="M",
            seed="a6-test",
        )
        session.add(model)
        await session.flush()

        if not dangling:
            session.add(
                ModelVersion(
                    id=version_id,
                    model_id=model.id,
                    version_number=1,
                    snapshot_json={},
                    created_by="a6-test",
                )
            )
            await session.flush()

        # Point at version_id either way — when dangling=True that row was
        # never inserted, which is exactly the revert/prune aftermath. Set it
        # via SQL so no ORM relationship validation intervenes.
        await session.execute(
            text(
                "UPDATE models SET predictive_built_for_version_id = :v "
                "WHERE id = :m"
            ),
            {"v": version_id, "m": model.id},
        )
        await session.commit()
        return model.id, version_id


@pytest.mark.asyncio
async def test_upgrade_survives_a_dangling_predictive_pointer():
    """The regression: a pointer to a deleted version must not abort 0201.

    Fails against the pre-fix migration with
    ``ForeignKeyViolation: ... is not present in table "model_versions"``.
    """
    async with _isolated_schema() as (engine, schema):
        model_id, _ = await _seed(engine, dangling=True)
        async with engine.begin() as conn:
            await conn.run_sync(_run_upgrade)

        async with engine.connect() as conn:
            pointer = (
                await conn.execute(
                    text(
                        "SELECT predictive_built_for_version_id FROM models "
                        "WHERE id = :m"
                    ),
                    {"m": model_id},
                )
            ).scalar()
            assert pointer is None, (
                "a pointer to a destroyed version means 'not built' and must be "
                f"cleared by the pre-flight, got {pointer!r}"
            )

            fk = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM information_schema.table_constraints "
                        "WHERE constraint_name = :n AND constraint_type = 'FOREIGN KEY' "
                        "  AND constraint_schema = :s"
                    ),
                    {"n": _FK_NAME, "s": schema},
                )
            ).scalar()
            assert fk == 1, "the foreign key must still be created"


@pytest.mark.asyncio
async def test_upgrade_preserves_a_valid_predictive_pointer():
    """The pre-flight must clear ONLY orphans — never a live stamp.

    Without this, a fix that simply NULLed the column unconditionally would
    also pass the regression test above while destroying real build state.
    """
    async with _isolated_schema() as (engine, schema):
        model_id, version_id = await _seed(engine, dangling=False)
        async with engine.begin() as conn:
            await conn.run_sync(_run_upgrade)

        async with engine.connect() as conn:
            pointer = (
                await conn.execute(
                    text(
                        "SELECT predictive_built_for_version_id FROM models "
                        "WHERE id = :m"
                    ),
                    {"m": model_id},
                )
            ).scalar()
            assert pointer == version_id, (
                "a pointer to a version that still exists is live build state "
                "and must be preserved"
            )


@pytest.mark.asyncio
async def test_upgrade_is_idempotent():
    """A second run must be a no-op, not a duplicate-constraint error."""
    async with _isolated_schema() as (engine, schema):
        await _seed(engine, dangling=True)
        async with engine.begin() as conn:
            await conn.run_sync(_run_upgrade)
        async with engine.begin() as conn:
            await conn.run_sync(_run_upgrade)

        async with engine.connect() as conn:
            fk = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM information_schema.table_constraints "
                        "WHERE constraint_name = :n AND constraint_type = 'FOREIGN KEY' "
                        "  AND constraint_schema = :s"
                    ),
                    {"n": _FK_NAME, "s": schema},
                )
            ).scalar()
            assert fk == 1
