"""CORRECTNESS-lane delete-guard integrity tests.

Covers three registry bugs where a delete handler bypassed dependency cleanup:

  - Bug-7794 — delete_source DB-cascaded every table, skipping the Bug-6225
    strip/purge/hierarchy-sweep cleanup. Now every table under the source is
    routed through the shared cleanup helper.
  - Bug-7792 — the physical-column delete guard missed HierarchyLevel key /
    attribute references, leaving a dangling key_attribute_id.
  - Bug-7795 — delete_target raised a raw IntegrityError (500) on an in-use
    target and silently repointed Model.target_id.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from .result_fakes import FakeScalarResult


@pytest.fixture(autouse=True)
def _noop_model_lock():
    """Bug-7982 R6: delete_source now acquires the per-model advisory lock (one
    extra ``db.execute``). These mocked delete-guard tests assert on the cleanup /
    cascade behaviour, not the lock; the lock is covered by test_model_lock_coverage
    + the live-DB lock suites. No-op it so the mocked execute stream is unshifted."""
    with patch("src.api.sources.acquire_model_definition_lock", AsyncMock()):
        yield


from shared.db.models import DataSource, DataTarget, Model, ModelTable
from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    make_mock_db,
    make_model,
)

pytestmark = pytest.mark.anyio


class _Res:
    """Result supporting .scalars().all(), .all(), .fetchall(), .scalar()."""

    def __init__(self, rows=None, scalar_val=None):
        self._rows = list(rows or [])
        self._scalar = scalar_val

    def scalars(self):
        return FakeScalarResult(self._rows)

    def all(self):
        return list(self._rows)

    def fetchall(self):
        return list(self._rows)

    def scalar(self):
        return self._scalar


def _execute_queue(*results):
    queue = list(results)

    async def _side(*_a, **_kw):
        if queue:
            return queue.pop(0)
        return _Res([])

    return AsyncMock(side_effect=_side)


def _get_by_class(by_class):
    async def _side(cls, _id):
        return by_class.get(cls)

    return AsyncMock(side_effect=_side)


# --------------------------------------------------------------------------- #
# Bug-7794 — delete_source routes each table through the shared cleanup
# --------------------------------------------------------------------------- #

SOURCES_PREFIX = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/sources"
)


async def test_delete_source_cleans_up_every_table(client):
    source_id = uuid.uuid4()
    t1, t2 = uuid.uuid4(), uuid.uuid4()
    model = make_model()
    source = types.SimpleNamespace(id=source_id, model_id=TEST_MODEL_ID)

    db = make_mock_db()
    db.get = _get_by_class({Model: model, DataSource: source})
    # Route issues: source FOR UPDATE lock, then the table-id lookup. The RLS
    # pre-check (real assert_table_not_rls_mapping) and per-table cleanup then
    # run; cleanup is mocked out so we assert it is CALLED per table, and the
    # RLS-check selects fall through to the empty queue fallback (no rules).
    db.execute = _execute_queue(
        _Res([]),          # source FOR UPDATE lock
        _Res([t1, t2]),    # table ids under the source
    )

    cleanup = AsyncMock()
    revalidate = AsyncMock()

    with (
        patch("src.api.sources.get_tenant_db", async_gen_from(db)),
        patch("src.api._table_cleanup.cleanup_table_dependents", cleanup),
        patch("shared.semantic.model_validator.revalidate_model", revalidate),
    ):
        resp = await client.delete(f"{SOURCES_PREFIX}/{source_id}")

    assert resp.status_code == 204, resp.text
    # The Bug-6225 cleanup ran once per table — no bare cascade.
    cleaned = {c.kwargs["table_id"] for c in cleanup.call_args_list}
    assert cleaned == {t1, t2}
    # Revalidation ran after the source went away.
    revalidate.assert_awaited_once()
    db.delete.assert_awaited_once_with(source)


async def test_delete_source_not_found(client):
    model = make_model()
    db = make_mock_db()
    db.get = _get_by_class({Model: model, DataSource: None})

    with patch("src.api.sources.get_tenant_db", async_gen_from(db)):
        resp = await client.delete(f"{SOURCES_PREFIX}/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_delete_source_blocked_when_table_is_rls_mapping(client):
    """RowSecurityRule.mapping_table_id is ondelete=RESTRICT — deleting a source
    whose table maps an RLS rule must 409 (not flush to a raw 500), and must
    NOT run any cleanup or delete the source."""
    source_id = uuid.uuid4()
    t1 = uuid.uuid4()
    model = make_model()
    source = types.SimpleNamespace(id=source_id, model_id=TEST_MODEL_ID)

    db = make_mock_db()
    db.get = _get_by_class({Model: model, DataSource: source})
    db.execute = _execute_queue(
        _Res([]),                   # source FOR UPDATE lock
        _Res([t1]),                 # table ids under the source
        _Res([]),                   # t1 FOR UPDATE lock (in RLS check)
        _Res(["region_map_rule"]),  # RLS rule names mapping table t1
    )

    cleanup = AsyncMock()
    with (
        patch("src.api.sources.get_tenant_db", async_gen_from(db)),
        patch("src.api._table_cleanup.cleanup_table_dependents", cleanup),
    ):
        resp = await client.delete(f"{SOURCES_PREFIX}/{source_id}")

    assert resp.status_code == 409, resp.text
    assert "region_map_rule" in resp.json()["detail"]
    cleanup.assert_not_called()
    db.delete.assert_not_called()


async def test_cleanup_table_dependents_two_tables_one_hierarchy():
    """Direct (non-mocked) unit of the shared helper: two tables of one source
    each contribute a level to the SAME hierarchy. Only after the SECOND
    table's level is dropped (leaving zero remaining) is the hierarchy stripped
    from personas and deleted — proving the per-table orphan re-check."""
    from src.api import _table_cleanup

    model_id = TEST_MODEL_ID
    t1, t2 = uuid.uuid4(), uuid.uuid4()
    c1, c2 = uuid.uuid4(), uuid.uuid4()
    hierarchy_id = uuid.uuid4()

    strip = AsyncMock()
    purge = AsyncMock()

    def _db_for(col_id, remaining_after):
        db = make_mock_db()
        db.execute = _execute_queue(
            _Res([]),               # FOR UPDATE lock on the table row
            _Res([]),               # assert_table_not_rls_mapping -> no rules
            _Res([col_id]),         # col_ids for this table
            _Res([]),               # uda_ids
            _Res([]),               # base measure ids
            _Res([]),               # dimension rows
            _Res([]),               # delete(Dimension) by col
            _Res([]),               # delete(Measure) by col
            _Res([hierarchy_id]),   # physical hierarchy-level hierarchy ids
            _Res([]),               # delete(HierarchyLevel)
            _Res(scalar_val=remaining_after),  # remaining level count for hid
        )
        return db

    with (
        patch("src.api.personas.strip_id_from_personas", strip),
        patch("src.api._scope.purge_entity_soft_references", purge),
    ):
        # First table: after dropping its level, ONE level remains (the second
        # table's) -> hierarchy NOT yet orphaned.
        await _table_cleanup.cleanup_table_dependents(
            _db_for(c1, remaining_after=1), model_id=model_id, table_id=t1
        )
        # Second table: after dropping its level, ZERO remain -> hierarchy is
        # stripped from personas and deleted.
        await _table_cleanup.cleanup_table_dependents(
            _db_for(c2, remaining_after=0), model_id=model_id, table_id=t2
        )

    stripped_hierarchy = [
        c for c in strip.call_args_list
        if c.kwargs.get("object_class") == "hierarchy"
        and c.kwargs.get("object_id") == hierarchy_id
    ]
    # Stripped exactly once — on the second (orphaning) table only.
    assert len(stripped_hierarchy) == 1


# --------------------------------------------------------------------------- #
# Bug-7795 — delete_target guards dependents + clears (not repoints) target_id
# --------------------------------------------------------------------------- #

TARGETS_PREFIX = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/targets"
)


async def test_delete_target_blocked_by_aggregate_dependents(client):
    target_id = uuid.uuid4()
    model = make_model()
    target = types.SimpleNamespace(id=target_id, model_id=TEST_MODEL_ID)

    db = make_mock_db()
    db.get = _get_by_class({Model: model, DataTarget: target})
    db.execute = _execute_queue(
        _Res([]),                   # FOR UPDATE lock select
        _Res(["agg_sales_daily"]),  # aggregate physical_table_names
        _Res([]),                   # pocket physical_table_names
    )

    with patch("src.api.targets.get_tenant_db", async_gen_from(db)):
        resp = await client.delete(f"{TARGETS_PREFIX}/{target_id}")

    assert resp.status_code == 409, resp.text
    assert "agg_sales_daily" in resp.json()["detail"]
    db.delete.assert_not_called()


async def test_delete_target_clears_active_target_without_repoint(client):
    target_id = uuid.uuid4()
    # The model's active target IS the one being deleted.
    model = make_model()
    model.target_id = target_id
    target = types.SimpleNamespace(id=target_id, model_id=TEST_MODEL_ID)

    db = make_mock_db()
    db.get = _get_by_class({Model: model, DataTarget: target})
    db.execute = _execute_queue(
        _Res([]),  # FOR UPDATE lock select
        _Res([]),  # no aggregate dependents
        _Res([]),  # no pocket dependents
    )

    with patch("src.api.targets.get_tenant_db", async_gen_from(db)):
        resp = await client.delete(f"{TARGETS_PREFIX}/{target_id}")

    assert resp.status_code == 204, resp.text
    # Bug-7795: target_id is CLEARED, never silently repointed to another target.
    assert model.target_id is None
    db.delete.assert_awaited_once_with(target)


# --------------------------------------------------------------------------- #
# Bug-7792 — physical-column delete guard detects hierarchy-level references
# --------------------------------------------------------------------------- #

TABLE_PREFIX = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"
    f"/tables/{{table_id}}"
)


async def test_delete_physical_column_blocked_by_hierarchy_level_key(client):
    table_id = uuid.uuid4()
    column_id = uuid.uuid4()
    model = make_model()
    table = types.SimpleNamespace(id=table_id, model_id=TEST_MODEL_ID)
    col = types.SimpleNamespace(id=column_id, model_table_id=table_id)

    from shared.db.models import ModelColumn

    async def _get(cls, _id):
        if cls is Model:
            return model
        if cls is ModelTable:
            return table
        if cls is ModelColumn:
            return col
        return None

    db = make_mock_db()
    db.get = AsyncMock(side_effect=_get)
    # Query order in delete_table_attribute (physical path):
    #   1 dimensions, 2 measures, 3 joins, 4 uda column refs,
    #   5 hierarchy-level KEY refs (this one has a hit), 6 level-attr refs.
    db.execute = _execute_queue(
        _Res([]),                                   # dimensions
        _Res([]),                                   # measures
        _Res([]),                                   # joins
        _Res([]),                                   # uda column refs
        _Res([("Geography", "City")]),              # hierarchy-level key ref
        _Res([]),                                   # level-attribute refs
    )

    url = TABLE_PREFIX.format(table_id=table_id)
    with patch("src.api.table_attributes.get_tenant_db", async_gen_from(db)):
        resp = await client.delete(
            f"{url}/attributes/{column_id}?kind=physical"
        )

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "hierarchy levels (key)" in detail
    assert "Geography.City" in detail
    db.delete.assert_not_called()


async def test_delete_physical_column_allowed_when_unreferenced(client):
    table_id = uuid.uuid4()
    column_id = uuid.uuid4()
    model = make_model()
    table = types.SimpleNamespace(id=table_id, model_id=TEST_MODEL_ID)
    col = types.SimpleNamespace(id=column_id, model_table_id=table_id)
    from shared.db.models import ModelColumn

    async def _get(cls, _id):
        if cls is Model:
            return model
        if cls is ModelTable:
            return table
        if cls is ModelColumn:
            return col
        return None

    db = make_mock_db()
    db.get = AsyncMock(side_effect=_get)
    db.execute = _execute_queue(
        _Res([]),  # dimensions
        _Res([]),  # measures
        _Res([]),  # joins
        _Res([]),  # uda column refs
        _Res([]),  # hierarchy-level key refs
        _Res([]),  # level-attribute refs
    )

    url = TABLE_PREFIX.format(table_id=table_id)
    with patch("src.api.table_attributes.get_tenant_db", async_gen_from(db)):
        resp = await client.delete(
            f"{url}/attributes/{column_id}?kind=physical"
        )

    assert resp.status_code == 204, resp.text
    db.delete.assert_awaited_once_with(col)
