"""Bug-8862: table routes must bind project -> model -> source -> table.

Test escape: all eight handlers in ``src/api/tables.py`` declared ``project_id``
as a path parameter and then never referenced it, chaining only source -> model.
RBAC (``require_role``, ``src/auth/rbac.py``) reads ``project_id`` from the path
and checks the CALLER'S binding for that project; when ``model_id`` is also in
the path it is used only to PREFER a model-scoped binding, falling back to the
project-scoped one. Nothing proved the model in the path belonged to that
project. So a caller holding a legitimate ``modeler``/``viewer`` binding in
project A could send ``project_id=A`` together with a model/source/table from
project B in the same tenant and read or MUTATE B.

Guard: these tests. Tier: T1.

Every denial test asserts the rejection REASON, not just the 404 status: the
pre-fix handlers already returned 404 for the unrelated "table does not belong
to this source/model" reason, so a bare status assertion would pass against the
vulnerable code. Each fixture therefore keeps the source/table perfectly
consistent with the path model, leaving the project mismatch as the ONLY
possible cause of rejection.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from shared.db.models import DataSource, Model, ModelTable
from shared.schemas.pydantic_models import ModelTableCreate, ModelTableUpdate
from src.api.tables import (
    ApplyClassificationRequest,
    analyze_table_endpoint,
    apply_classification,
    create_table,
    delete_table,
    get_table,
    list_tables,
    rename_preview,
    update_table,
)

pytestmark = pytest.mark.unit

TENANT = types.SimpleNamespace(tenant_id="acme")

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
CALLER_PROJECT = uuid.uuid4()
MODEL_ID = uuid.uuid4()
SOURCE_ID = uuid.uuid4()
TABLE_ID = uuid.uuid4()

CREATE_BODY = ModelTableCreate(
    # Bug-8930 / Bug-8876: no body `source_id` — it is a path parameter.
    table_type="dim_detail",
    physical_name="public.customers",
    display_name="Customers",
)
UPDATE_BODY = ModelTableUpdate(display_name="Renamed")
CLASSIFY_BODY = ApplyClassificationRequest(table_type=None, overrides=[])


def _column(name: str, dtype: str):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        column_name=name,
        data_type=dtype,
        is_nullable=True,
        is_hidden=False,
        display_name=None,
        description=None,
        cardinality_estimate=None,
        last_stats_at=None,
    )


def _db(*, model_project_id, source_model_id=MODEL_ID, table_model_id=MODEL_ID,
        table_source_id=SOURCE_ID):
    """A db double whose Model/DataSource/ModelTable belong to the given owners.

    Defaults make the source and the table perfectly consistent with the path
    ``model_id``/``source_id``, so a rejection can only come from the project
    link unless a test overrides them.
    """
    table = types.SimpleNamespace(
        id=TABLE_ID,
        model_id=table_model_id,
        source_id=table_source_id,
        table_type="dim_detail",
        physical_name="public.customers",
        alias="customers",
        display_name="Customers",
        description=None,
        row_count_estimate=None,
        last_stats_at=None,
        calendar_table_id=None,
        created_at=NOW,
        updated_at=NOW,
        columns=[_column("customer_id", "integer"), _column("name", "varchar")],
    )

    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = table
    result.scalar.return_value = 0
    result.scalars.return_value.all.return_value = []
    result.all.return_value = []
    db.execute = AsyncMock(return_value=result)
    db.add = MagicMock()  # Session.add is sync; an AsyncMock child leaks a coroutine
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.delete = AsyncMock()

    async def _get(entity, entity_id):
        if entity is Model:
            if model_project_id is None:
                return None
            return types.SimpleNamespace(id=entity_id, project_id=model_project_id)
        if entity is DataSource:
            return types.SimpleNamespace(id=entity_id, model_id=source_model_id)
        if entity is ModelTable:
            return table
        return None

    db.get = AsyncMock(side_effect=_get)
    db._table = table
    return db


def _patch(db):
    """Patch the tenant session and neutralise the advisory lock.

    The lock is real SQL; the doubles here are not a database. Patching it does
    not weaken the assertion under test — the guard being verified runs before
    (project -> model) and after (the entity read) it.
    """
    async def _tenant_db(_tenant_id):
        yield db

    return (
        patch("src.api.tables.get_tenant_db", new=_tenant_db),
        patch("src.api.tables.acquire_model_definition_lock", new=AsyncMock()),
    )


async def _expect_denied(db, detail, coro_factory):
    tenant_patch, lock_patch = _patch(db)
    with tenant_patch, lock_patch, pytest.raises(HTTPException) as exc:
        await coro_factory()
    assert exc.value.status_code == 404
    assert exc.value.detail == detail
    db.commit.assert_not_awaited()
    db.delete.assert_not_awaited()


# ---------------------------------------------------------------------------
# The model in the path belongs to ANOTHER project — every handler must refuse.
# The source and the table are consistent with the path ids, so "Model not
# found" is the only admissible reason.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_table_rejects_model_from_another_project():
    """The write path matters most: no cross-project table creation."""
    db = _db(model_project_id=uuid.uuid4())
    await _expect_denied(db, "Model not found", lambda: create_table(
        CALLER_PROJECT, MODEL_ID, SOURCE_ID, CREATE_BODY, current_user=TENANT
    ))


@pytest.mark.asyncio
async def test_list_tables_rejects_model_from_another_project():
    db = _db(model_project_id=uuid.uuid4())
    await _expect_denied(db, "Model not found", lambda: list_tables(
        CALLER_PROJECT, MODEL_ID, SOURCE_ID, current_user=TENANT
    ))


@pytest.mark.asyncio
async def test_get_table_rejects_model_from_another_project():
    db = _db(model_project_id=uuid.uuid4())
    await _expect_denied(db, "Model not found", lambda: get_table(
        CALLER_PROJECT, MODEL_ID, SOURCE_ID, TABLE_ID, current_user=TENANT
    ))


@pytest.mark.asyncio
async def test_update_table_rejects_model_from_another_project():
    db = _db(model_project_id=uuid.uuid4())
    await _expect_denied(db, "Model not found", lambda: update_table(
        CALLER_PROJECT, MODEL_ID, SOURCE_ID, TABLE_ID, UPDATE_BODY,
        current_user=TENANT,
    ))


@pytest.mark.asyncio
async def test_delete_table_rejects_model_from_another_project():
    db = _db(model_project_id=uuid.uuid4())
    await _expect_denied(db, "Model not found", lambda: delete_table(
        CALLER_PROJECT, MODEL_ID, SOURCE_ID, TABLE_ID, current_user=TENANT
    ))


@pytest.mark.asyncio
async def test_analyze_table_rejects_model_from_another_project():
    db = _db(model_project_id=uuid.uuid4())
    await _expect_denied(db, "Model not found", lambda: analyze_table_endpoint(
        CALLER_PROJECT, MODEL_ID, SOURCE_ID, TABLE_ID, current_user=TENANT
    ))


@pytest.mark.asyncio
async def test_rename_preview_rejects_model_from_another_project():
    db = _db(model_project_id=uuid.uuid4())
    await _expect_denied(db, "Model not found", lambda: rename_preview(
        CALLER_PROJECT, MODEL_ID, SOURCE_ID, TABLE_ID, "renamed",
        current_user=TENANT,
    ))


@pytest.mark.asyncio
async def test_apply_classification_rejects_model_from_another_project():
    db = _db(model_project_id=uuid.uuid4())
    await _expect_denied(db, "Model not found", lambda: apply_classification(
        CALLER_PROJECT, MODEL_ID, SOURCE_ID, TABLE_ID, CLASSIFY_BODY,
        current_user=TENANT,
    ))


@pytest.mark.asyncio
async def test_handlers_reject_a_model_that_does_not_exist():
    """A deleted / bogus model_id must 404 on the model, not fall through."""
    db = _db(model_project_id=None)
    await _expect_denied(db, "Model not found", lambda: get_table(
        CALLER_PROJECT, MODEL_ID, SOURCE_ID, TABLE_ID, current_user=TENANT
    ))


# ---------------------------------------------------------------------------
# The model is correctly scoped, but the nested resource is foreign.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_table_rejects_source_from_another_model():
    db = _db(model_project_id=CALLER_PROJECT, source_model_id=uuid.uuid4())
    await _expect_denied(db, "DataSource not found", lambda: create_table(
        CALLER_PROJECT, MODEL_ID, SOURCE_ID, CREATE_BODY, current_user=TENANT
    ))


@pytest.mark.asyncio
async def test_get_table_rejects_table_from_another_model():
    db = _db(model_project_id=CALLER_PROJECT, table_model_id=uuid.uuid4())
    await _expect_denied(db, "ModelTable not found", lambda: get_table(
        CALLER_PROJECT, MODEL_ID, SOURCE_ID, TABLE_ID, current_user=TENANT
    ))


@pytest.mark.asyncio
async def test_update_table_rejects_table_from_another_source():
    db = _db(model_project_id=CALLER_PROJECT, table_source_id=uuid.uuid4())
    await _expect_denied(db, "ModelTable not found", lambda: update_table(
        CALLER_PROJECT, MODEL_ID, SOURCE_ID, TABLE_ID, UPDATE_BODY,
        current_user=TENANT,
    ))


@pytest.mark.asyncio
async def test_delete_table_rejects_table_from_another_model():
    db = _db(model_project_id=CALLER_PROJECT, table_model_id=uuid.uuid4())
    await _expect_denied(db, "ModelTable not found", lambda: delete_table(
        CALLER_PROJECT, MODEL_ID, SOURCE_ID, TABLE_ID, current_user=TENANT
    ))


@pytest.mark.asyncio
async def test_analyze_table_rejects_table_from_another_model():
    db = _db(model_project_id=CALLER_PROJECT, table_model_id=uuid.uuid4())
    await _expect_denied(db, "ModelTable not found", lambda: analyze_table_endpoint(
        CALLER_PROJECT, MODEL_ID, SOURCE_ID, TABLE_ID, current_user=TENANT
    ))


@pytest.mark.asyncio
async def test_rename_preview_rejects_table_from_another_model():
    db = _db(model_project_id=CALLER_PROJECT, table_model_id=uuid.uuid4())
    await _expect_denied(db, "ModelTable not found", lambda: rename_preview(
        CALLER_PROJECT, MODEL_ID, SOURCE_ID, TABLE_ID, "renamed",
        current_user=TENANT,
    ))


@pytest.mark.asyncio
async def test_apply_classification_rejects_table_from_another_model():
    db = _db(model_project_id=CALLER_PROJECT, table_model_id=uuid.uuid4())
    await _expect_denied(db, "ModelTable not found", lambda: apply_classification(
        CALLER_PROJECT, MODEL_ID, SOURCE_ID, TABLE_ID, CLASSIFY_BODY,
        current_user=TENANT,
    ))


# ---------------------------------------------------------------------------
# The guard must not be a blanket denial — a correctly scoped chain still works.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_correctly_scoped_list_is_allowed():
    db = _db(model_project_id=CALLER_PROJECT)
    tenant_patch, lock_patch = _patch(db)
    with tenant_patch, lock_patch:
        assert await list_tables(
            CALLER_PROJECT, MODEL_ID, SOURCE_ID, current_user=TENANT
        ) == []


@pytest.mark.asyncio
async def test_correctly_scoped_get_is_allowed():
    """Closes the blanket-denial hole on ``get_table``.

    Without this, a regression that turned the handler into an unconditional
    404 would leave every denial test above green — they would simply pass for
    the wrong reason, which is the same trap the reason-asserting denial tests
    were written to avoid, sitting on the positive side.
    """
    db = _db(model_project_id=CALLER_PROJECT)
    tenant_patch, lock_patch = _patch(db)
    with tenant_patch, lock_patch:
        resp = await get_table(
            CALLER_PROJECT, MODEL_ID, SOURCE_ID, TABLE_ID, current_user=TENANT
        )
    assert resp.id == TABLE_ID
    assert resp.model_id == MODEL_ID
    assert resp.source_id == SOURCE_ID
    assert resp.alias == "customers"


@pytest.mark.asyncio
async def test_correctly_scoped_update_is_allowed():
    """Closes the blanket-denial hole on ``update_table`` and proves the write
    actually lands rather than being swallowed by the new guard."""
    db = _db(model_project_id=CALLER_PROJECT)
    tenant_patch, lock_patch = _patch(db)
    with tenant_patch, lock_patch:
        resp = await update_table(
            CALLER_PROJECT, MODEL_ID, SOURCE_ID, TABLE_ID, UPDATE_BODY,
            current_user=TENANT,
        )
    assert resp.display_name == "Renamed"
    assert resp.id == TABLE_ID
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_correctly_scoped_rename_preview_is_allowed():
    db = _db(model_project_id=CALLER_PROJECT)
    tenant_patch, lock_patch = _patch(db)
    with tenant_patch, lock_patch:
        assert await rename_preview(
            CALLER_PROJECT, MODEL_ID, SOURCE_ID, TABLE_ID, "renamed",
            current_user=TENANT,
        ) == []


@pytest.mark.asyncio
async def test_correctly_scoped_write_is_allowed():
    """A write on a correctly scoped chain commits — the guard is not a wall.

    Bug-8864: this also pins the handler's success path past the analyzer.
    ``apply_classification`` built ``Dimension(..., data_type=...)`` but
    ``Dimension`` has no ``data_type`` column, so the declarative constructor
    raised TypeError and the endpoint returned HTTP 500 for every column
    classified as dimension/date_key. Both fixture columns classify as
    dimensions, so a regression turns this assertion red rather than silently
    creating nothing.
    """
    db = _db(model_project_id=CALLER_PROJECT)
    tenant_patch, lock_patch = _patch(db)
    with tenant_patch, lock_patch:
        resp = await apply_classification(
            CALLER_PROJECT, MODEL_ID, SOURCE_ID, TABLE_ID, CLASSIFY_BODY,
            current_user=TENANT,
        )
    assert resp.dimensions_created == 2
    assert resp.measures_created == 0
    assert db.add.call_count == 2
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_correctly_scoped_analyze_is_allowed():
    db = _db(model_project_id=CALLER_PROJECT)
    tenant_patch, lock_patch = _patch(db)
    with tenant_patch, lock_patch:
        resp = await analyze_table_endpoint(
            CALLER_PROJECT, MODEL_ID, SOURCE_ID, TABLE_ID, current_user=TENANT
        )
    assert resp.table_id == str(TABLE_ID) or resp.table_id == TABLE_ID


# ---------------------------------------------------------------------------
# Bug-8878 — the BODY foreign key ``calendar_table_id``
# ---------------------------------------------------------------------------
#
# Bug-8862 (above) closed the PATH chain. ``ModelTableUpdate`` also exposes
# ``calendar_table_id``, which the handler applied through the blanket
# ``for k, v in updates.items(): setattr(t, k, v)`` loop. ``calendar_tables.id``
# is tenant-schema-wide, so a project-B calendar id satisfies the foreign key
# and a modeler in project A could bind their own table to it. The reference is
# then dereferenced without an ownership re-check by hierarchies.py,
# hierarchy_health.py and measures.py — and, in a DIFFERENT SERVICE, by
# query-router ``rewrite/calendar_support.py::_resolve_calendar_binding``, whose
# result reaches ``rewrite/source_sql.py`` as
# ``LEFT JOIN <calendar.table_name> AS cal``. The foreign calendar's physical
# table name and column meanings therefore end up in emitted SQL.
#
# Test escape: no test ever sent ``calendar_table_id`` on the PATCH body at all,
# so the field was written by a loop nothing inspected.
# Guard: these tests plus the real-Postgres route tests in
# ``tests/integration/test_body_fk_route_adoption_db.py``. Tier: T3.
#
# STATUS CODE: 422, not the 404 Bug-8878's intake note proposed. The addressed
# path resource is fine and already proven to belong to the caller; what is
# wrong is the submitted PAYLOAD. This is the uniform convention of the
# ``_scope`` body-FK primitive family (see its STATUS-CODE POLICY) and of the
# ``data_tags.py`` precedent. The intake note's 404 is superseded.

FOREIGN_CALENDAR_ID = uuid.uuid4()
OWNED_CALENDAR_ID = uuid.uuid4()


def _db_with_calendar_lookup(*, resolves):
    """``_db`` with the calendar ownership SELECT scripted.

    ``update_table`` issues no ``execute`` before the guard — the path chain and
    the table read both go through ``db.get`` — so the FIRST ``execute`` is the
    guard's scoped SELECT. ``resolves`` is the row that scoped query finds, or
    ``None`` for "no calendar of that id inside this project+model", which is
    the single outcome the primitive produces for an unknown id and a foreign
    id alike.
    """
    db = _db(model_project_id=CALLER_PROJECT)
    default = db.execute.return_value
    calls = {"n": 0}

    async def _execute(*_a, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            found = MagicMock()
            found.scalars.return_value.one_or_none.return_value = resolves
            found.scalars.return_value.all.return_value = (
                [resolves] if resolves is not None else []
            )
            return found
        return default

    db.execute = AsyncMock(side_effect=_execute)
    db._execute_calls = calls
    return db


@pytest.mark.asyncio
async def test_update_table_rejects_a_calendar_outside_the_model():
    """A calendar id that does not resolve inside the path project+model is
    refused, and the foreign id never reaches the row or the commit."""
    db = _db_with_calendar_lookup(resolves=None)
    body = ModelTableUpdate(calendar_table_id=FOREIGN_CALENDAR_ID)
    tenant_patch, lock_patch = _patch(db)

    with tenant_patch, lock_patch, pytest.raises(HTTPException) as exc:
        await update_table(
            CALLER_PROJECT, MODEL_ID, SOURCE_ID, TABLE_ID, body,
            current_user=TENANT,
        )

    assert exc.value.status_code == 422
    detail = exc.value.detail
    assert isinstance(detail, dict), (
        "must be the body-FK error shape, not a bare string or FastAPI's own "
        "422 validation list"
    )
    assert detail["error_code"] == "CALENDAR_TABLE_NOT_IN_MODEL"
    assert detail["field"] == "calendar_table_id"
    assert detail["ids"] == [str(FOREIGN_CALENDAR_ID)]
    assert "a calendar table" in detail["message"]
    # The value must not have been applied even transiently: the guard runs
    # before the setattr loop precisely because the session autoflushes.
    assert db._table.calendar_table_id is None
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_table_accepts_a_calendar_owned_by_the_model():
    """The guard is an ownership check, not a blanket denial.

    Bug-8864 shipped a scope guard that denied essentially everything; only a
    positive test catches that direction. This also pins the producer side —
    the accepted id is what actually lands on the row.
    """
    owned = types.SimpleNamespace(id=OWNED_CALENDAR_ID)
    db = _db_with_calendar_lookup(resolves=owned)
    body = ModelTableUpdate(calendar_table_id=OWNED_CALENDAR_ID)
    tenant_patch, lock_patch = _patch(db)

    with tenant_patch, lock_patch:
        resp = await update_table(
            CALLER_PROJECT, MODEL_ID, SOURCE_ID, TABLE_ID, body,
            current_user=TENANT,
        )

    assert resp.calendar_table_id == OWNED_CALENDAR_ID
    assert db._table.calendar_table_id == OWNED_CALENDAR_ID
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_update_table_still_allows_unbinding_a_calendar():
    """An explicit ``null`` means "unbind" and must stay legal.

    The guard keys on the field being PRESENT, not on it being truthy, so a
    reader cannot mistake this for a branch that skips validation. The helper
    returns ``None`` for an absent optional FK rather than raising.
    """
    db = _db_with_calendar_lookup(resolves=None)
    db._table.calendar_table_id = uuid.uuid4()
    body = ModelTableUpdate(calendar_table_id=None)
    assert "calendar_table_id" in body.model_dump(exclude_unset=True)
    tenant_patch, lock_patch = _patch(db)

    with tenant_patch, lock_patch:
        resp = await update_table(
            CALLER_PROJECT, MODEL_ID, SOURCE_ID, TABLE_ID, body,
            current_user=TENANT,
        )

    assert resp.calendar_table_id is None
    assert db._table.calendar_table_id is None
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_update_table_without_the_field_issues_no_calendar_lookup():
    """A PATCH that does not mention the calendar must not be rejected by the
    guard, and must leave the existing binding alone — ``exclude_unset`` is what
    separates "unbind" from "not mentioned"."""
    db = _db_with_calendar_lookup(resolves=None)
    existing = uuid.uuid4()
    db._table.calendar_table_id = existing
    tenant_patch, lock_patch = _patch(db)

    with tenant_patch, lock_patch:
        resp = await update_table(
            CALLER_PROJECT, MODEL_ID, SOURCE_ID, TABLE_ID,
            ModelTableUpdate(display_name="Renamed"), current_user=TENANT,
        )

    assert resp.display_name == "Renamed"
    assert db._table.calendar_table_id == existing
    db.commit.assert_awaited()
