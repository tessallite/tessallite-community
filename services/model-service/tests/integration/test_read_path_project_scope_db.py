"""Real-Postgres proof that the downstream-asset READ path is project-scoped.

The body-FK programme (``_scope.py`` + its adoption sites) guards what a request
is about to WRITE. It says nothing about what is ALREADY STORED, and this file
covers the other half for ``downstream_assets.py``:

* **F-01** — ``update_downstream_asset`` / ``delete_downstream_asset`` selected
  solely by ``asset_id`` and compared ``asset.model_id`` afterwards. With
  ``selectinload(DownstreamAsset.columns)`` attached (added to repair a genuine
  HTTP 500 on the PUT path), submitting another project's asset UUID pulled that
  asset AND every associated ``ModelColumn`` into memory before answering 404.
  Refusing after the read is not refusing. The ownership predicate is now in the
  SELECT, and the tests below assert that through the EMITTED SQL — a status
  assertion alone passes against the pre-fix handler, which also answered 404.

* **F-02** — ``ensure_refs_in_model`` protects a ``column_ids`` collection the
  client SUBMITS. Rows already in ``downstream_asset_columns`` were resolved by
  the pre-guard handler with no ownership predicate at all, so an association
  pointing at another project's column survived, and the relationship load that
  built the response returned it. A rename answered **200 carrying a foreign
  column UUID** — a successful disclosure, not a refusal. The legacy-data tests
  below insert that association DIRECTLY, bypassing the API, because the API now
  refuses to create it; that is the only way to prove the read path is closed.

Disposition for legacy foreign associations is OMIT, not refuse and not delete:
refusing would make an asset permanently unreadable over data the user never
knowingly created, and deleting would be a destructive write on a GET. A client
that rewrites ``column_ids`` still purges them, because that is a write it asked
for — asserted here too.

A mocked session cannot prove any of this: it does not evaluate a WHERE clause
and it never performs a lazy load. Hence real Postgres, real handlers, real rows.

Guard: this file. Tier: T3 (cross-project isolation).
Test escape: every existing downstream-asset test built its fixture through the
API, so no association ever pointed outside the path model, and no test ever
looked at WHICH rows a refused request had already read.

Skipped unless ``TESSALLITE_VERSIONING_DB_URL`` (or the importer-harness URL)
points at a reachable Postgres.

Run:
    cd tessallite/services/model-service
    TESSALLITE_VERSIONING_DB_URL=postgresql+asyncpg://user:pw@localhost:5432/db \
      pytest tests/integration/test_read_path_project_scope_db.py -v
"""
from __future__ import annotations

import types
import uuid
from contextlib import asynccontextmanager, contextmanager
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.engine import Engine

from shared.db.models import DownstreamAsset, downstream_asset_columns
from shared.schemas.pydantic_models import (
    DownstreamAssetCreate,
    DownstreamAssetUpdate,
)
from src.api.downstream_assets import (
    create_downstream_asset,
    delete_downstream_asset,
    list_downstream_assets,
    update_downstream_asset,
)

from tests.integration.test_scope_body_fk_db import _Fixture, _seed
from tests.integration.test_versioning_consistency_db import _DB_URL, _isolated_schema

pytestmark = [pytest.mark.integration]

_TENANT = types.SimpleNamespace(
    tenant_id="acme", raw_token="t", email="u@test", role="modeler",
    user_id="u@test",
)


@asynccontextmanager
async def _routes_on(db):
    async def _tenant_db(_tenant_id):
        yield db

    with patch("src.api.downstream_assets.get_tenant_db", new=_tenant_db):
        yield


@contextmanager
def _recorded_sql():
    """Every SQL statement the handler emits, in order.

    This is what makes the F-01 tests an oracle rather than theatre. The
    pre-fix handler ALSO answered 404 for a foreign asset, so a status
    assertion cannot tell the two apart. What changed is which rows were read
    on the way there, and only the emitted SQL shows that.
    """
    seen: list[str] = []

    def _record(_conn, _cursor, statement, _params, _context, _many):
        seen.append(statement)

    event.listen(Engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(Engine, "before_cursor_execute", _record)


async def _seed_foreign_asset(db, f: _Fixture) -> uuid.UUID:
    """An asset that belongs to project B's model, with project B's column on it.

    Built through the API on ITS OWN project's route, so it is an ordinary,
    legitimately-created asset — the only thing wrong is that a caller in
    project A will name its id.
    """
    async with _routes_on(db):
        created = await create_downstream_asset(
            f.project_b, f.model_b,
            DownstreamAssetCreate(
                asset_type="dashboard", asset_name="B's dashboard",
                column_ids=[f.column_b],
            ),
            current_user=_TENANT,
        )
    return created.id


async def _seed_legacy_foreign_association(
    db, f: _Fixture, *, asset_id: uuid.UUID, column_id: uuid.UUID
) -> None:
    """Persist the association the pre-guard handler could create.

    Written straight to ``downstream_asset_columns`` on purpose: the API now
    refuses this exact row, so the only way to reach the state a real tenant is
    already in is to bypass the API, which is precisely what "legacy data"
    means. ``model_columns.id`` is tenant-schema-wide, so the foreign key
    constraint does not object.
    """
    await db.execute(
        downstream_asset_columns.insert().values(
            asset_id=asset_id, model_column_id=column_id
        )
    )
    await db.commit()


# ---------------------------------------------------------------------------
# F-01 — a foreign asset is never read, not merely refused after the read
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_update_downstream_asset_never_reads_a_foreign_asset():
    """F-01. Project B's asset UUID on project A's route must not load B's
    asset row, and must not trigger the associated-column SELECT."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)
            foreign_asset_id = await _seed_foreign_asset(db, f)

            async with _routes_on(db):
                with _recorded_sql() as sql:
                    with pytest.raises(HTTPException) as exc:
                        await update_downstream_asset(
                            f.project_a, f.model_a, foreign_asset_id,
                            DownstreamAssetUpdate(asset_name="Renamed"),
                            current_user=_TENANT,
                        )

            assert exc.value.status_code == 404
            assert exc.value.detail == "Downstream asset not found"

            asset_reads = [s for s in sql if "downstream_assets" in s]
            assert asset_reads, "the handler must actually look the asset up"
            assert all(
                "models" in s and "model_id" in s for s in asset_reads
            ), (
                "the ownership predicate must be IN the SELECT: "
                f"{asset_reads}"
            )
            assert not [s for s in sql if "model_columns" in s], (
                "a foreign asset's columns must never be loaded — the eager "
                f"loader must not fire at all: {sql}"
            )

            # And nothing was written on the way out.
            await db.rollback()
            row = await db.get(DownstreamAsset, foreign_asset_id)
            await db.refresh(row)
            assert row.asset_name == "B's dashboard"


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_update_downstream_asset_still_updates_its_own_asset():
    """The positive direction. Bug-8864 shipped a scope guard that denied
    essentially everything; only a positive test catches that."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            async with _routes_on(db):
                created = await create_downstream_asset(
                    f.project_a, f.model_a,
                    DownstreamAssetCreate(
                        asset_type="dashboard", asset_name="Sales",
                        column_ids=[f.column_a],
                    ),
                    current_user=_TENANT,
                )
                renamed = await update_downstream_asset(
                    f.project_a, f.model_a, created.id,
                    DownstreamAssetUpdate(asset_name="Renamed"),
                    current_user=_TENANT,
                )

            assert renamed.asset_name == "Renamed"
            assert renamed.column_ids == [f.column_a]


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_an_asset_with_no_columns_reports_an_empty_list_throughout():
    """The exact contract the T0 deployed-session scenario pins.

    ``tests/live/test_model_service_crud_live.py``
    ``LIVE-MODEL-DOWNSTREAM-ASSET-001`` asserts ``column_ids == []`` on create,
    on list, and on a PUT that renames — and its own docstring records that
    this was a MOCK-HIDDEN escape, only visible against a real async ORM
    session. Every line that produces those three values was rewritten by this
    lane (the relationship load became a scoped join, and ``_to_response`` no
    longer falls back to ``asset.columns``), so the same three values are
    re-proved here against real Postgres rather than left entirely to a live
    tier that cannot run before the image is rebuilt.
    """
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            async with _routes_on(db):
                created = await create_downstream_asset(
                    f.project_a, f.model_a,
                    DownstreamAssetCreate(
                        asset_type="dashboard", asset_name="Sales",
                        asset_url="https://bi.example.invalid/live-test",
                        owner="live-test",
                    ),
                    current_user=_TENANT,
                )
                assert created.column_ids == []

                listed = await list_downstream_assets(
                    f.project_a, f.model_a, current_user=_TENANT,
                )
                assert [a.id for a in listed] == [created.id]
                assert listed[0].column_ids == []

                renamed = await update_downstream_asset(
                    f.project_a, f.model_a, created.id,
                    DownstreamAssetUpdate(
                        asset_name="Renamed", notes="live update",
                    ),
                    current_user=_TENANT,
                )
                assert renamed.asset_name == "Renamed"
                assert renamed.notes == "live update"
                assert renamed.column_ids == []

                await delete_downstream_asset(
                    f.project_a, f.model_a, created.id, current_user=_TENANT,
                )
                after = await list_downstream_assets(
                    f.project_a, f.model_a, current_user=_TENANT,
                )
                assert after == []


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_delete_downstream_asset_never_reads_a_foreign_asset():
    """F-01, delete path. Same shape, same fix, and the foreign asset survives."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)
            foreign_asset_id = await _seed_foreign_asset(db, f)

            async with _routes_on(db):
                with _recorded_sql() as sql:
                    with pytest.raises(HTTPException) as exc:
                        await delete_downstream_asset(
                            f.project_a, f.model_a, foreign_asset_id,
                            current_user=_TENANT,
                        )

            assert exc.value.status_code == 404
            asset_reads = [s for s in sql if "downstream_assets" in s]
            assert asset_reads
            assert all("models" in s and "model_id" in s for s in asset_reads), (
                f"the delete path must scope its lookup too: {asset_reads}"
            )

            await db.rollback()
            assert await db.get(DownstreamAsset, foreign_asset_id) is not None, (
                "another project's asset must survive"
            )
            assert (
                await db.execute(
                    select(downstream_asset_columns.c.model_column_id)
                )
            ).scalars().all() == [f.column_b], "and so must its associations"


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_delete_downstream_asset_removes_an_owned_asset_with_columns():
    """The positive direction for delete, WITH associations attached — one of
    them a legacy FOREIGN association.

    ``db.delete(parent)`` on a ``secondary`` relationship makes SQLAlchemy load
    the collection at flush time to clear the association rows, which reads
    every associated ``ModelColumn`` — including the foreign one. The delete
    therefore uses explicit DML, and this asserts both halves: the request
    completes, every association goes with the asset, and no ``model_columns``
    row is read to achieve it."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            async with _routes_on(db):
                created = await create_downstream_asset(
                    f.project_a, f.model_a,
                    DownstreamAssetCreate(
                        asset_type="dashboard", asset_name="Sales",
                        column_ids=[f.column_a, f.column_a_other],
                    ),
                    current_user=_TENANT,
                )
            await _seed_legacy_foreign_association(
                db, f, asset_id=created.id, column_id=f.column_b
            )

        # A FRESH session, exactly like a production request: nothing about the
        # asset is already loaded, so an implicit collection load would be lazy.
        async with factory() as db2:
            async with _routes_on(db2):
                with _recorded_sql() as sql:
                    await delete_downstream_asset(
                        f.project_a, f.model_a, created.id,
                        current_user=_TENANT,
                    )

            assert not [s for s in sql if "model_columns" in s], (
                "clearing the associations must not read the columns they "
                f"point at: {sql}"
            )
            assert await db2.get(DownstreamAsset, created.id) is None
            assert (
                await db2.execute(
                    select(downstream_asset_columns.c.model_column_id)
                )
            ).scalars().all() == []


# ---------------------------------------------------------------------------
# F-02 — legacy foreign associations are omitted from every read
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
@pytest.mark.parametrize("foreign_kind", ["other_project", "sibling_model"])
async def test_list_downstream_assets_omits_a_legacy_foreign_column(foreign_kind):
    """F-02, legacy data. A ``downstream_asset_columns`` row written before the
    body-FK guard existed must not be reported, and the OWNED column must still
    be — a guard that hides everything is the Bug-8864 failure direction.

    ``sibling_model`` is covered because a guard that only checks the project
    passes the obvious cross-project case and still leaks to another model in
    the same project."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)
            foreign_column = {
                "other_project": f.column_b,
                "sibling_model": f.column_a2,
            }[foreign_kind]

            async with _routes_on(db):
                created = await create_downstream_asset(
                    f.project_a, f.model_a,
                    DownstreamAssetCreate(
                        asset_type="dashboard", asset_name="Sales",
                        column_ids=[f.column_a],
                    ),
                    current_user=_TENANT,
                )
            await _seed_legacy_foreign_association(
                db, f, asset_id=created.id, column_id=foreign_column
            )

            stored = (
                await db.execute(
                    select(downstream_asset_columns.c.model_column_id)
                )
            ).scalars().all()
            assert sorted(map(str, stored)) == sorted(
                map(str, [f.column_a, foreign_column])
            ), "the fixture must really contain the legacy row"

            async with _routes_on(db):
                listed = await list_downstream_assets(
                    f.project_a, f.model_a, current_user=_TENANT,
                )

            assert len(listed) == 1
            assert listed[0].column_ids == [f.column_a], (
                "the foreign column id must be omitted and the owned one kept"
            )


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_update_omits_a_legacy_foreign_column_without_deleting_it():
    """F-02, legacy data, on the PUT that does not send ``column_ids``.

    The response must not carry the foreign id, and the association row must
    still be there afterwards: a rename is not a licence to destroy stored
    data, and the disposition for a read is OMIT, not purge."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            async with _routes_on(db):
                created = await create_downstream_asset(
                    f.project_a, f.model_a,
                    DownstreamAssetCreate(
                        asset_type="dashboard", asset_name="Sales",
                        column_ids=[f.column_a],
                    ),
                    current_user=_TENANT,
                )
            await _seed_legacy_foreign_association(
                db, f, asset_id=created.id, column_id=f.column_b
            )

            async with _routes_on(db):
                renamed = await update_downstream_asset(
                    f.project_a, f.model_a, created.id,
                    DownstreamAssetUpdate(asset_name="Renamed"),
                    current_user=_TENANT,
                )

            assert renamed.asset_name == "Renamed"
            assert renamed.column_ids == [f.column_a], (
                "a successful 200 must not carry another project's column id"
            )
            stored = (
                await db.execute(
                    select(downstream_asset_columns.c.model_column_id)
                )
            ).scalars().all()
            assert sorted(map(str, stored)) == sorted(
                map(str, [f.column_a, f.column_b])
            ), "a read must not delete stored rows"


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_rewriting_column_ids_purges_a_legacy_foreign_association():
    """F-02, the write side of the same data. When the client actually rewrites
    the collection, the legacy foreign row goes — that is a write it asked for,
    and it is what lets a tenant clean the state up through the product."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            async with _routes_on(db):
                created = await create_downstream_asset(
                    f.project_a, f.model_a,
                    DownstreamAssetCreate(
                        asset_type="dashboard", asset_name="Sales",
                        column_ids=[f.column_a],
                    ),
                    current_user=_TENANT,
                )
            await _seed_legacy_foreign_association(
                db, f, asset_id=created.id, column_id=f.column_b
            )

            async with _routes_on(db):
                rewritten = await update_downstream_asset(
                    f.project_a, f.model_a, created.id,
                    DownstreamAssetUpdate(column_ids=[f.column_a_other]),
                    current_user=_TENANT,
                )

            assert rewritten.column_ids == [f.column_a_other]
            stored = (
                await db.execute(
                    select(downstream_asset_columns.c.model_column_id)
                )
            ).scalars().all()
            assert stored == [f.column_a_other], (
                "the legacy foreign association must not survive a rewrite"
            )


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_a_legacy_foreign_column_is_never_selected_on_the_read_path():
    """F-02, the stronger form. Omitting the id from the response is necessary
    but not sufficient — the foreign ``model_columns`` ROW must not be read at
    all, which a Python-side filter over an unfiltered relationship load would
    not achieve."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            async with _routes_on(db):
                created = await create_downstream_asset(
                    f.project_a, f.model_a,
                    DownstreamAssetCreate(
                        asset_type="dashboard", asset_name="Sales",
                        column_ids=[f.column_a],
                    ),
                    current_user=_TENANT,
                )
            await _seed_legacy_foreign_association(
                db, f, asset_id=created.id, column_id=f.column_b
            )

            async with _routes_on(db):
                with _recorded_sql() as sql:
                    await list_downstream_assets(
                        f.project_a, f.model_a, current_user=_TENANT,
                    )

            column_reads = [s for s in sql if "model_columns" in s]
            assert column_reads, "the response still needs the owned column ids"
            assert all(
                "model_tables" in s and "models" in s for s in column_reads
            ), (
                "every column read must be joined up to the owning model: "
                f"{column_reads}"
            )
