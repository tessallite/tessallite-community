"""Bug-8473 — re-pointing a target or a connection invalidates its caches.

The shared primitive is unit-tested in ``tessallite/tests/unit/
test_artifact_target_binding.py``. This file pins the WIRING, which is the half
that silently rots: a control-plane writer that computes the right thing and
never calls it leaves the exposure fully open while every unit test stays green.

The exposure: an aggregate or pocket names its cache by
``(target_schema, physical_table_name)``, which identifies a table only WITHIN a
database. A modeller re-pointing ``DataTarget.project_connection_id``, or an
admin editing that connection's endpoint, changes which database those names
resolve to. If the new database holds a same-named table, the cache serves ITS
rows — and under row-level security the injected predicate is evaluated against
that foreign table's column, so a row the principal's policy never covered comes
back.

Test escape: no test asserted that a target/connection edit took the caches
built on the old location out of the serving pool. Tier: T1.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from shared.db.models import DataTarget
from shared.schemas.pydantic_models import DataTargetUpdate

from .conftest import FakeResult


def _target(conn_id):
    t = DataTarget(
        id=uuid.uuid4(),
        model_id=uuid.uuid4(),
        project_connection_id=conn_id,
        target_type="postgresql",
        display_name="warehouse target",
        config={"schema": "analytics"},
    )
    now = datetime.now(timezone.utc)
    t.created_at = now
    t.updated_at = now
    return t


class _DB:
    """Fake session that answers ``get`` per ENTITY TYPE.

    Bug-8922: this used to return the SAME row for every class, so the handler
    could not be run against the real connector-authority validator
    (``_validate_target_connection``, which fetches the ProjectConnection
    through this same session). The validator was stubbed with a bare
    ``AsyncMock()`` instead, and its return value carried a MOCK
    ``connection_type`` — which no comparison can match, so every target test
    in this file died on a 422 ``target_connection_type_mismatch`` long before
    reaching the invalidation it exists to assert. Answering by class lets the
    real validator run, which removes the stub and its drift entirely.
    """

    def __init__(self, entity, *rows):
        self._rows = {type(row): row for row in (entity, *rows)}
        self.committed = False
        # ORDER log, so "same transaction as the edit" is asserted rather than
        # assumed: an invalidation moved after ``commit()`` is a DIFFERENT
        # transaction, and ``committed is True`` alone cannot see that.
        self.events: list[str] = []

    def register(self, row):
        self._rows[type(row)] = row
        return row

    async def get(self, cls, _id):
        return self._rows.get(cls)

    async def commit(self):
        self.events.append("commit")
        self.committed = True

    async def refresh(self, _obj):
        return None


def _tenant_db(db):
    async def _gen(_tenant_id):
        yield db
    return _gen


def _connection_row(conn_id, project_id, connection_type):
    """A real ProjectConnection row for the connection a target resolves to.

    ``DataTarget.target_type`` is legacy descriptive metadata; the CONNECTION
    is the connector authority, and the product rejects on purpose a target
    whose label disagrees with it. A faithful stand-in therefore agrees —
    ``connection_type`` is DERIVED from the target under test rather than
    hardcoded, so changing the fixture's connector cannot silently
    reintroduce the 422 this file used to die on.
    """
    from shared.db.models import ProjectConnection

    c = ProjectConnection(
        id=conn_id,
        project_id=project_id,
        display_name="warehouse",
        connection_type=connection_type,
        encrypted_credentials=None,
        config={"host": "db-a", "database": "warehouse"},
    )
    now = datetime.now(timezone.utc)
    c.created_at = now
    c.updated_at = now
    return c


async def _call_update_target(db, target, body):
    from src.api import targets as targets_mod

    project_id = uuid.uuid4()
    model = types.SimpleNamespace(id=target.model_id, project_id=project_id)
    # The EFFECTIVE connection + connector label the write resolves to — the
    # body's when the edit supplies one, otherwise what is already on the row.
    # Mirrors ``update_target``'s own effective-value resolution. The real
    # ``_validate_target_connection`` reads the row back off this same session;
    # only the project/model scope check above it is stubbed.
    updates = body.model_dump(exclude_unset=True)
    effective_conn_id = (
        updates.get("project_connection_id") or target.project_connection_id
    )
    effective_type = updates.get("target_type") or target.target_type
    db.register(_connection_row(effective_conn_id, project_id, effective_type))

    async def _record(*_a, **_kw):
        db.events.append("invalidate")
        return (1, 2)

    with patch.object(targets_mod, "get_tenant_db", _tenant_db(db)), \
         patch.object(
             targets_mod, "ensure_model_in_project", AsyncMock(return_value=model)
         ), \
         patch.object(
             targets_mod, "invalidate_artifacts_for_target",
             AsyncMock(side_effect=_record),
         ) as invalidate:
        await targets_mod.update_target(
            project_id=project_id,
            model_id=target.model_id,
            target_id=target.id,
            body=body,
            current_user=types.SimpleNamespace(
                tenant_id="acme", email="m@x", raw_token="t"
            ),
        )
    return invalidate, db


@pytest.mark.asyncio
async def test_rebinding_a_target_to_another_connection_invalidates_its_caches():
    target = _target(uuid.uuid4())
    db = _DB(target)
    invalidate, db = await _call_update_target(
        db, target, DataTargetUpdate(project_connection_id=uuid.uuid4())
    )

    invalidate.assert_awaited_once()
    assert invalidate.await_args.args[1] == target.id
    # Same transaction as the edit: a reader can never see the new location with
    # artifacts built against the old one. Assert the ORDER, not merely that a
    # commit happened — an invalidation moved after ``commit()`` still leaves
    # ``committed is True`` while opening exactly the window this guards.
    assert db.committed is True
    assert db.events == ["invalidate", "commit"], (
        "the invalidation must be AWAITED and must run inside the same "
        f"transaction as the edit; observed {db.events}"
    )


@pytest.mark.asyncio
async def test_changing_the_target_config_invalidates_its_caches():
    """``config`` carries the schema / BigQuery dataset the cache table is
    qualified with, so editing it re-points the table exactly like a connection
    swap does."""
    target = _target(uuid.uuid4())
    invalidate, _ = await _call_update_target(
        _DB(target), target, DataTargetUpdate(config={"schema": "analytics_eu"})
    )
    invalidate.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_non_routing_edit_does_not_invalidate():
    """A display-name change must not force every cache on the target to
    rebuild — over-invalidation here is a real operational cost."""
    target = _target(uuid.uuid4())
    invalidate, db = await _call_update_target(
        _DB(target), target, DataTargetUpdate(display_name="renamed")
    )
    invalidate.assert_not_awaited()
    # The edit itself must still have landed. Without this, a handler that
    # silently did NOTHING would satisfy ``assert_not_awaited`` too, and this
    # test would be asserting the absence of a call it never reached.
    assert db.events == ["commit"], f"observed {db.events}"


@pytest.mark.asyncio
async def test_rewriting_the_config_with_an_equal_value_does_not_invalidate():
    target = _target(uuid.uuid4())
    invalidate, db = await _call_update_target(
        _DB(target), target, DataTargetUpdate(config={"schema": "analytics"})
    )
    invalidate.assert_not_awaited()
    assert db.events == ["commit"], f"observed {db.events}"


# ---------------------------------------------------------------------------
# The connection side — one edit re-points every target that resolves through it
# ---------------------------------------------------------------------------

def _connection():
    from shared.db.models import ProjectConnection

    c = ProjectConnection(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        display_name="warehouse",
        connection_type="postgresql",
        encrypted_credentials=None,
        config={"host": "db-a", "database": "warehouse"},
    )
    now = datetime.now(timezone.utc)
    c.created_at = now
    c.updated_at = now
    return c


async def _call_update_connection(conn, body):
    from src.api import connections as connections_mod

    db = _DB(conn)
    with patch.object(connections_mod, "get_tenant_db", _tenant_db(db)), \
         patch.object(connections_mod, "enforce_demo_source_locked", lambda *_a: None), \
         patch.object(connections_mod, "audit_required", AsyncMock()), \
         patch.object(
             connections_mod, "invalidate_artifacts_for_connection",
             AsyncMock(return_value=(3, 4)),
         ) as invalidate:
        await connections_mod.update_connection(
            project_id=conn.project_id,
            connection_id=conn.id,
            body=body,
            current_user=types.SimpleNamespace(
                tenant_id="acme", email="a@x", raw_token="t"
            ),
        )
    return invalidate, db


@pytest.mark.asyncio
async def test_moving_a_connection_endpoint_invalidates_every_dependent_cache():
    from shared.schemas.pydantic_models import ConnectionUpdate

    conn = _connection()
    invalidate, db = await _call_update_connection(
        conn, ConnectionUpdate(config={"host": "db-b", "database": "warehouse"})
    )

    invalidate.assert_awaited_once()
    assert db.committed is True
    assert invalidate.await_args.args[1] == conn.id


@pytest.mark.asyncio
async def test_renaming_a_connection_does_not_invalidate():
    from shared.schemas.pydantic_models import ConnectionUpdate

    conn = _connection()
    invalidate, _ = await _call_update_connection(
        conn, ConnectionUpdate(display_name="warehouse (prod)")
    )
    invalidate.assert_not_awaited()


# ---------------------------------------------------------------------------
# The SOURCE side (Bug-8602) — re-pointing DataSource.project_connection_id
# ---------------------------------------------------------------------------
#
# Round-2 review finding: this lever shipped with a STATIC guard only
# (``_awaited_calls_in`` in tessallite/tests/unit/test_artifact_target_binding.py),
# which proves a call NAME appears somewhere inside ``update_source`` and
# nothing else. Four mutations were verified GREEN under it and RED here:
# dropping the ``await``; inverting the ``_conn_before != ...`` condition;
# moving the call after ``db.commit()``; passing ``source_id`` instead of
# ``model_id``. Each one silently leaves every already-built aggregate and
# pocket of the model serving rows materialised from the PREVIOUS database
# while the source-route fallback for the same query reads the new one.
#
# Test escape: the target lever had this behavioural wiring test from Bug-8473;
# the source lever got only the static guard. Guard: the three tests below.
# Tier: T1.

def _source(model_id, conn_id):
    from shared.db.models import DataSource

    s = DataSource(
        id=uuid.uuid4(),
        model_id=model_id,
        project_connection_id=conn_id,
        source_type="postgresql",
        display_name="sales warehouse",
        default_schema="public",
        config={},
    )
    now = datetime.now(timezone.utc)
    s.created_at = now
    s.updated_at = now
    return s


class _OrderedDB(_DB):
    """``_DB`` plus ``execute``. The ORDER log lives on ``_DB`` itself — the
    target lever needs it too (Bug-8922), and "a call moved after ``commit()``
    is a different transaction" is exactly the mutation a static guard cannot
    see."""

    async def execute(self, _stmt):
        # Bug-8602 round 2: the re-point handler also enumerates any OTHER
        # model whose tables read through this DataSource. No such row exists
        # in these fixtures, so an empty result is the faithful answer.
        return FakeResult([])


async def _call_update_source(source, body):
    from src.api import sources as sources_mod

    db = _OrderedDB(source)

    async def _record(*_a, **_kw):
        db.events.append("invalidate")
        return (1, 2)

    invalidate = AsyncMock(side_effect=_record)
    with patch.object(sources_mod, "get_tenant_db", _tenant_db(db)), \
         patch.object(sources_mod, "ensure_model_in_project", AsyncMock()), \
         patch.object(sources_mod, "acquire_model_definition_lock", AsyncMock()), \
         patch.object(sources_mod, "_validate_source_connection", AsyncMock()), \
         patch.object(sources_mod, "invalidate_artifacts_for_model", invalidate):
        await sources_mod.update_source(
            project_id=uuid.uuid4(),
            model_id=source.model_id,
            source_id=source.id,
            body=body,
            current_user=types.SimpleNamespace(
                tenant_id="acme", email="m@x", raw_token="t"
            ),
        )
    return invalidate, db


@pytest.mark.asyncio
async def test_repointing_a_source_invalidates_the_models_caches():
    from shared.schemas.pydantic_models import DataSourceUpdate

    model_id = uuid.uuid4()
    source = _source(model_id, uuid.uuid4())
    invalidate, db = await _call_update_source(
        source, DataSourceUpdate(project_connection_id=uuid.uuid4())
    )

    invalidate.assert_awaited_once()
    # Scoped on the MODEL: neither ProjectConnection row changed, so a
    # connection-keyed invalidator structurally cannot see this edit.
    assert invalidate.await_args.args[1] == model_id
    assert db.events == ["invalidate", "commit"], (
        "the invalidation must be AWAITED and must run inside the same "
        f"transaction as the edit; observed {db.events}"
    )


@pytest.mark.asyncio
async def test_a_non_routing_source_edit_does_not_invalidate():
    """Over-invalidation costs every aggregate on the model a rebuild."""
    from shared.schemas.pydantic_models import DataSourceUpdate

    source = _source(uuid.uuid4(), uuid.uuid4())
    invalidate, _ = await _call_update_source(
        source, DataSourceUpdate(display_name="renamed")
    )
    invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_rewriting_the_same_source_connection_does_not_invalidate():
    from shared.schemas.pydantic_models import DataSourceUpdate

    conn_id = uuid.uuid4()
    source = _source(uuid.uuid4(), conn_id)
    invalidate, _ = await _call_update_source(
        source, DataSourceUpdate(project_connection_id=conn_id)
    )
    invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_repoint_widens_invalidation_to_every_model_reading_the_source():
    """Round-2 finding 3's ``also_models`` shipped with no assertion at all.

    ``ModelTable.source_id`` carries no composite FK back to
    ``(model_id, source_id)``, so a legacy/imported table CAN read another
    model's DataSource — a state ``model_ids_for_source_connection`` already
    declares it must catch. A re-point handler scoped only on the DataSource's
    OWNING model leaves the borrowing model's aggregates and pockets serving
    rows materialised from the previous database.

    Test escape: deleting ``also_models=`` from update_source was GREEN under
    every shipped test (round-3 review, mutation-verified). Tier: T1.
    """
    from shared.schemas.pydantic_models import DataSourceUpdate
    from src.api import sources as sources_mod

    model_id = uuid.uuid4()
    borrowing_model_id = uuid.uuid4()
    source = _source(model_id, uuid.uuid4())
    db = _OrderedDB(source)
    invalidate = AsyncMock(return_value=(0, 1))
    with patch.object(sources_mod, "get_tenant_db", _tenant_db(db)), \
         patch.object(sources_mod, "ensure_model_in_project", AsyncMock()), \
         patch.object(sources_mod, "acquire_model_definition_lock", AsyncMock()), \
         patch.object(sources_mod, "_validate_source_connection", AsyncMock()), \
         patch.object(
             sources_mod, "model_ids_reading_source",
             AsyncMock(return_value=[model_id, borrowing_model_id]),
         ), \
         patch.object(sources_mod, "invalidate_artifacts_for_model", invalidate):
        await sources_mod.update_source(
            project_id=uuid.uuid4(),
            model_id=model_id,
            source_id=source.id,
            body=DataSourceUpdate(project_connection_id=uuid.uuid4()),
            current_user=types.SimpleNamespace(
                tenant_id="acme", email="m@x", raw_token="t"
            ),
        )

    invalidate.assert_awaited_once()
    assert borrowing_model_id in set(
        invalidate.await_args.kwargs.get("also_models") or ()
    ), (
        "the re-point handler must invalidate every model whose tables read "
        "through this DataSource, not only the DataSource's owning model"
    )
