"""Real-Postgres proof that two high-severity ROUTES adopted the body-FK guards.

``tests/integration/test_scope_body_fk_db.py`` proves the ``_scope`` primitives
themselves behave against a real database. This file proves the two routes that
adopted them actually refuse a foreign reference and still accept an owned one —
the difference between "the helper works" and "the handler calls it".

Both sites are T3 (cross-project isolation, wrong numbers, destructive writes):

* ``aggregates.py::create_aggregate`` — ``target_id`` is a NOT NULL body foreign
  key to ``data_targets``, written straight into the definition. A foreign
  ``target_id`` points the materialisation CTAS and every scheduled refresh at
  another project's warehouse connection. The optimizer's lifecycle twin has
  validated this since Bug-8026; the model-service API path had not.
* ``models.py::update_model`` — Bug-8026, the ``models.target_id`` half.
  ``ModelUpdate.target_id`` arrives in the PATCH body and was applied by a
  blanket ``setattr`` loop with nothing proving the target belonged to the path
  model. Every downstream materialiser that trusts ``models.target_id`` — the
  optimizer sweep, predictive build and AI runner — would then build this
  model's aggregates through the foreign target's ``project_connection_id``.
* ``tables.py::update_table`` — Bug-8878. ``calendar_table_id`` arrives in the
  PATCH body and was applied by a blanket ``setattr`` loop. Downstream it is
  dereferenced without an ownership re-check by hierarchies.py,
  hierarchy_health.py, measures.py, and — in a DIFFERENT SERVICE — by
  query-router ``rewrite/calendar_support.py::_resolve_calendar_binding``, whose
  result reaches ``rewrite/source_sql.py`` as
  ``LEFT JOIN <calendar.table_name> AS cal``.

Both answer 422, the uniform convention of the ``_scope`` body-FK family: the
addressed PATH resource is fine and already proven to belong to the caller, the
submitted PAYLOAD is not. Bug-8878's intake note proposed 404; that note is
superseded rather than given a second convention.

A mocked session cannot prove any of this — it does not evaluate a WHERE clause,
so an accept-everything guard and the deny-everything guard that shipped as
Bug-8864 both pass under a fake. Hence real Postgres, real handlers, real rows.

Skipped unless ``TESSALLITE_VERSIONING_DB_URL`` (or the importer-harness URL)
points at a reachable Postgres.

Run:
    cd tessallite/services/model-service
    TESSALLITE_VERSIONING_DB_URL=postgresql+asyncpg://user:pw@localhost:5432/db \
      pytest tests/integration/test_body_fk_route_adoption_db.py -v
"""
from __future__ import annotations

import types
import uuid
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from shared.db.models import (
    AggregateDefinition,
    AggregateRefreshPolicy,
    DataTarget,
    ModelTable,
)
from shared.db.models import Model
from shared.schemas.pydantic_models import (
    AggregateDefinitionCreate,
    ModelTableUpdate,
    ModelUpdate,
)
from src.api.aggregates import create_aggregate
from src.api.models import update_model
from src.api.tables import update_table

from tests.integration.test_scope_body_fk_db import _Fixture, _seed
from tests.integration.test_versioning_consistency_db import _DB_URL, _isolated_schema

pytestmark = [pytest.mark.integration]

# ``user_id``/``email`` are read by ``update_model``'s audit record; the other
# two handlers ignore them.
_TENANT = types.SimpleNamespace(
    tenant_id="acme", raw_token="t", user_id="u@test", email="u@test",
)


async def _seed_targets(session, f: _Fixture) -> dict[uuid.UUID, uuid.UUID]:
    """One DataTarget per model, so every hop has a near-miss to be wrong about.

    The connection is reused from the model's own project, which is what makes
    the fixture honest: the ONLY thing separating ``target_a`` from ``target_a2``
    and ``target_b`` is which model owns it.
    """
    from shared.db.models import DataSource

    sources = {
        s.model_id: s.project_connection_id
        for s in (await session.execute(select(DataSource))).scalars().all()
    }
    targets: dict[uuid.UUID, uuid.UUID] = {}
    for model_id in (f.model_a, f.model_a2, f.model_b):
        tid = uuid.uuid4()
        targets[model_id] = tid
        session.add(
            DataTarget(
                id=tid,
                model_id=model_id,
                project_connection_id=sources[model_id],
                target_type="postgresql",
                display_name="T",
                config={},
            )
        )
    await session.commit()
    return targets


@asynccontextmanager
async def _routes_on(db):
    """Point both routers' tenant-session dependency at the isolated session.

    ``get_setting`` is stubbed to a fixed cron: the default-cron lookup walks the
    settings tables and is not the boundary under test. The advisory lock is NOT
    stubbed — it is real SQL and works against a real database.
    """
    async def _tenant_db(_tenant_id):
        yield db

    async def _cron(*_a, **_kw):
        return "0 * * * *"

    with (
        patch("src.api.aggregates.get_tenant_db", new=_tenant_db),
        patch("src.api.models.get_tenant_db", new=_tenant_db),
        patch("src.api.tables.get_tenant_db", new=_tenant_db),
        patch("src.api.aggregates.get_setting", new=_cron),
    ):
        yield


def _agg_body(target_id) -> AggregateDefinitionCreate:
    return AggregateDefinitionCreate(
        target_id=target_id, grain=[], measure_names=[], creation_reason="manual"
    )


# ---------------------------------------------------------------------------
# create_aggregate — Bug-8026, the model-service half
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_create_aggregate_accepts_a_target_owned_by_the_path_model():
    """The positive side first: the guard must not be a blanket denial.

    Bug-8864 was a scope guard that rejected essentially every table and was
    caught by a positive test, not a denial test.
    """
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)
            targets = await _seed_targets(db, f)

            async with _routes_on(db):
                resp = await create_aggregate(
                    f.project_a, f.model_a, _agg_body(targets[f.model_a]),
                    current_user=_TENANT,
                )

            assert resp.target_id == targets[f.model_a]
            rows = (
                await db.execute(
                    select(AggregateDefinition).where(
                        AggregateDefinition.model_id == f.model_a
                    )
                )
            ).scalars().all()
            assert len(rows) == 1
            assert rows[0].target_id == targets[f.model_a]

            # The accept path really does register a materialisation job. This
            # is what makes the denial tests' "no enabled policy exists"
            # assertion meaningful rather than vacuously true.
            policies = (
                await db.execute(
                    select(AggregateRefreshPolicy).where(
                        AggregateRefreshPolicy.is_enabled.is_(True)
                    )
                )
            ).scalars().all()
            assert len(policies) == 1
            assert policies[0].aggregate_definition_id == rows[0].id


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
@pytest.mark.parametrize("foreign", ["sibling_model", "other_project", "unknown"])
async def test_create_aggregate_refuses_a_target_it_does_not_own(foreign):
    """Every near-miss is refused, and NOTHING reaches a materialisation job.

    This is the end-to-end property, not merely the status code. The scheduler
    reaches an aggregate through ``select(AggregateRefreshPolicy).where(
    is_enabled)`` and then ``db.get(AggregateDefinition, ...)``
    (``services/scheduler/src/jobs/sweep.py``). The assertions below run that
    exact enumeration against the real schema after the refusal: no policy row
    exists, so the sweep has nothing to enumerate; no definition row exists, so
    there is nothing for it to resolve; and no row anywhere carries the foreign
    target id. A refused create therefore cannot become a CTAS against another
    project's warehouse connection.
    """
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)
            targets = await _seed_targets(db, f)
            bad = {
                "sibling_model": targets[f.model_a2],   # same project, other model
                "other_project": targets[f.model_b],    # different project
                "unknown": uuid.uuid4(),                # no such row at all
            }[foreign]

            async with _routes_on(db):
                with pytest.raises(HTTPException) as exc:
                    await create_aggregate(
                        f.project_a, f.model_a, _agg_body(bad),
                        current_user=_TENANT,
                    )

            assert exc.value.status_code == 422
            detail = exc.value.detail
            assert detail["error_code"] == "REF_NOT_IN_MODEL"
            assert detail["field"] == "target_id"
            assert detail["ids"] == [str(bad)]

            await db.rollback()
            assert (
                await db.execute(
                    select(AggregateRefreshPolicy).where(
                        AggregateRefreshPolicy.is_enabled.is_(True)
                    )
                )
            ).scalars().all() == [], "the scheduler sweep must enumerate nothing"
            assert (
                await db.execute(select(AggregateDefinition))
            ).scalars().all() == [], "no definition may survive a refused create"
            assert (
                await db.execute(
                    select(AggregateDefinition).where(
                        AggregateDefinition.target_id == bad
                    )
                )
            ).scalars().all() == []


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_create_aggregate_reports_foreign_and_unknown_targets_identically():
    """Anti-oracle: the response must not confirm that a target exists elsewhere
    in the tenant. Only the echoed (caller-supplied) id may differ."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)
            targets = await _seed_targets(db, f)

            async with _routes_on(db):
                with pytest.raises(HTTPException) as foreign:
                    await create_aggregate(
                        f.project_a, f.model_a, _agg_body(targets[f.model_b]),
                        current_user=_TENANT,
                    )
                await db.rollback()
                with pytest.raises(HTTPException) as unknown:
                    await create_aggregate(
                        f.project_a, f.model_a, _agg_body(uuid.uuid4()),
                        current_user=_TENANT,
                    )

            assert foreign.value.status_code == unknown.value.status_code
            a, b = dict(foreign.value.detail), dict(unknown.value.detail)
            assert a.pop("ids") != b.pop("ids")
            a.pop("message"), b.pop("message")
            assert a == b


# ---------------------------------------------------------------------------
# update_table — Bug-8878
# ---------------------------------------------------------------------------


async def _source_of(db, model_id):
    from shared.db.models import DataSource

    return (
        await db.execute(select(DataSource).where(DataSource.model_id == model_id))
    ).scalars().one().id


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_update_table_accepts_a_calendar_owned_by_the_path_model():
    """Positive side: an in-model calendar still binds, and the id persists."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)
            source_a = await _source_of(db, f.model_a)

            async with _routes_on(db):
                resp = await update_table(
                    f.project_a, f.model_a, source_a, f.table_a,
                    ModelTableUpdate(calendar_table_id=f.calendar_a),
                    current_user=_TENANT,
                )

            assert resp.calendar_table_id == f.calendar_a
            row = await db.get(ModelTable, f.table_a)
            await db.refresh(row)
            assert row.calendar_table_id == f.calendar_a


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
@pytest.mark.parametrize("foreign", ["sibling_model", "other_project", "unknown"])
async def test_update_table_refuses_a_calendar_it_does_not_own(foreign):
    """Bug-8878. The foreign id must never land on the row.

    The persisted-state assertion is the one that matters: a 422 that still
    committed the binding would leave query-router's
    ``_resolve_calendar_binding`` free to walk ``ModelTable.calendar_table_id``
    into another project's ``CalendarTable`` and emit its physical table name
    into generated SQL.
    """
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)
            source_a = await _source_of(db, f.model_a)
            bad = {
                "sibling_model": f.calendar_a2,   # same project, other model
                "other_project": f.calendar_b,    # different project
                "unknown": uuid.uuid4(),          # no such row at all
            }[foreign]

            async with _routes_on(db):
                with pytest.raises(HTTPException) as exc:
                    await update_table(
                        f.project_a, f.model_a, source_a, f.table_a,
                        ModelTableUpdate(calendar_table_id=bad),
                        current_user=_TENANT,
                    )

            assert exc.value.status_code == 422
            detail = exc.value.detail
            assert detail["error_code"] == "CALENDAR_TABLE_NOT_IN_MODEL"
            assert detail["field"] == "calendar_table_id"
            assert detail["ids"] == [str(bad)]

            await db.rollback()
            row = await db.get(ModelTable, f.table_a)
            await db.refresh(row)
            assert row.calendar_table_id is None
            assert (
                await db.execute(
                    select(ModelTable).where(ModelTable.calendar_table_id == bad)
                )
            ).scalars().all() == []


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_update_table_still_unbinds_and_still_edits_other_fields():
    """The guard must not turn a legitimate PATCH into a wall.

    Two shapes that must stay legal: an explicit ``null`` (unbind), and a PATCH
    that never mentions the calendar at all — ``exclude_unset`` is what
    separates them, and only a real round-trip proves the distinction survives.
    """
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)
            source_a = await _source_of(db, f.model_a)

            async with _routes_on(db):
                await update_table(
                    f.project_a, f.model_a, source_a, f.table_a,
                    ModelTableUpdate(calendar_table_id=f.calendar_a),
                    current_user=_TENANT,
                )
                renamed = await update_table(
                    f.project_a, f.model_a, source_a, f.table_a,
                    ModelTableUpdate(display_name="Renamed"),
                    current_user=_TENANT,
                )
                assert renamed.display_name == "Renamed"
                assert renamed.calendar_table_id == f.calendar_a, (
                    "a PATCH that does not mention the calendar must not "
                    "silently unbind it"
                )

                unbound = await update_table(
                    f.project_a, f.model_a, source_a, f.table_a,
                    ModelTableUpdate(calendar_table_id=None),
                    current_user=_TENANT,
                )
                assert unbound.calendar_table_id is None

            row = await db.get(ModelTable, f.table_a)
            await db.refresh(row)
            assert row.calendar_table_id is None


# ---------------------------------------------------------------------------
# update_model — Bug-8026, the models.target_id half
# ---------------------------------------------------------------------------
#
# create_aggregate above closes the aggregate's OWN target_id. This closes the
# field that feeds it: models.target_id is what the optimizer sweep, predictive
# build and AI runner all read and then persist onto the aggregates they build,
# so a foreign value here reaches the same source connection by a different
# route — one with no HTTP request in it at all.


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_update_model_accepts_a_target_owned_by_the_path_model():
    """Positive side first — the guard must not be a blanket denial."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)
            targets = await _seed_targets(db, f)

            async with _routes_on(db):
                resp = await update_model(
                    f.project_a, f.model_a,
                    ModelUpdate(target_id=targets[f.model_a]),
                    current_user=_TENANT,
                )

            assert resp.target_id == targets[f.model_a]
            row = await db.get(Model, f.model_a)
            await db.refresh(row)
            assert row.target_id == targets[f.model_a]


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
@pytest.mark.parametrize("foreign", ["sibling_model", "other_project", "unknown"])
async def test_update_model_refuses_a_target_it_does_not_own(foreign):
    """Every near-miss is refused, and the model's binding is left untouched.

    The persisted-state assertion is the point: a 422 that still wrote the row
    would be worse than no guard at all, because the caller would believe the
    repoint failed while every optimizer path read the new value.
    """
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)
            targets = await _seed_targets(db, f)

            # Start from a legitimate binding so "unchanged" is a real claim
            # rather than "still NULL".
            async with _routes_on(db):
                await update_model(
                    f.project_a, f.model_a,
                    ModelUpdate(target_id=targets[f.model_a]),
                    current_user=_TENANT,
                )

            bad = {
                "sibling_model": targets[f.model_a2],   # same project, other model
                "other_project": targets[f.model_b],    # different project
                "unknown": uuid.uuid4(),                # no such row at all
            }[foreign]

            async with _routes_on(db):
                with pytest.raises(HTTPException) as exc:
                    await update_model(
                        f.project_a, f.model_a,
                        ModelUpdate(target_id=bad),
                        current_user=_TENANT,
                    )

            assert exc.value.status_code == 422
            detail = exc.value.detail
            assert detail["error_code"] == "REF_NOT_IN_MODEL"
            assert detail["field"] == "target_id"
            assert detail["ids"] == [str(bad)]

            await db.rollback()
            row = await db.get(Model, f.model_a)
            await db.refresh(row)
            assert row.target_id == targets[f.model_a], (
                "a refused repoint must leave the previous binding intact"
            )
            assert (
                await db.execute(select(Model).where(Model.target_id == bad))
            ).scalars().all() == [], (
                "no model anywhere may end up bound to the refused target"
            )


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_update_model_still_unbinds_and_still_edits_other_fields():
    """The two shapes that must stay legal: explicit null, and never mentioned.

    ``models.target_id`` is nullable (ON DELETE SET NULL), so an explicit
    ``null`` is a legitimate unbind and must not be refused; and a PATCH that
    never mentions the target must not silently clear it. ``exclude_unset`` is
    what separates them, which is why the guard keys on field PRESENCE.
    """
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)
            targets = await _seed_targets(db, f)

            async with _routes_on(db):
                await update_model(
                    f.project_a, f.model_a,
                    ModelUpdate(target_id=targets[f.model_a]),
                    current_user=_TENANT,
                )
                renamed = await update_model(
                    f.project_a, f.model_a,
                    ModelUpdate(display_name="Renamed"),
                    current_user=_TENANT,
                )
                assert renamed.display_name == "Renamed"
                assert renamed.target_id == targets[f.model_a], (
                    "a PATCH that does not mention the target must not "
                    "silently unbind it"
                )

                unbound = await update_model(
                    f.project_a, f.model_a,
                    ModelUpdate(target_id=None),
                    current_user=_TENANT,
                )
                assert unbound.target_id is None

            row = await db.get(Model, f.model_a)
            await db.refresh(row)
            assert row.target_id is None
