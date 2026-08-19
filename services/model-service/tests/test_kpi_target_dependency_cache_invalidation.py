"""Regression coverage for target-only KPI cache dependency invalidation."""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

import pytest
from .result_fakes import FakeScalarResult

from src.kpi_cache import get_kpi_cache
from src.api.kpis import _dependent_kpi_ids

from .conftest import TEST_TENANT, async_gen_from, make_mock_db
from .test_kpi_composite_indicators import PREFIX, _kpi


class _Result:
    def __init__(self, items=(), single=None):
        self._items = list(items)
        self._single = single

    def scalars(self):
        return FakeScalarResult(self._items)

    def all(self):
        return list(self._items)

    def scalar_one_or_none(self):
        return self._single


def _target_only_chain():
    target = _kpi(name="Target Root", expression="literal(10)")
    consumer = _kpi(name="Target Consumer", expression="literal(20)")
    consumer.target_type = "expression"
    consumer.target_expression = 'kpi("Target Root")'
    transitive = _kpi(name="Target Transitive", expression="literal(30)")
    transitive.target_type = "expression"
    transitive.target_expression = 'kpi("Target Consumer")'
    unrelated = _kpi(name="Unrelated", expression="literal(40)")
    return target, consumer, transitive, unrelated


def _composite_ownership_chain():
    parent = _kpi(
        name="Ownership Composite", kpi_type="composite", expression="literal(0)",
    )
    child = _kpi(
        name="Ownership Child", expression="literal(50)",
        parent_kpi_id=parent.id, target_value=100.0,
    )
    consumer = _kpi(
        name="Ownership Consumer", expression='kpi("Ownership Composite")',
    )
    unrelated = _kpi(name="Ownership Unrelated", expression="literal(40)")
    return parent, child, consumer, unrelated


def _reassigned_ownership_chain():
    old_parent = _kpi(
        name="Old Composite", kpi_type="composite", expression="literal(0)",
    )
    new_parent = _kpi(
        name="New Composite", kpi_type="composite", expression="literal(0)",
    )
    child = _kpi(
        name="Moved Child", expression="literal(50)",
        parent_kpi_id=old_parent.id, target_value=100.0,
    )
    old_consumer = _kpi(name="Old Consumer", expression='kpi("Old Composite")')
    new_consumer = _kpi(name="New Consumer", expression='kpi("New Composite")')
    return old_parent, new_parent, child, old_consumer, new_consumer


def _warm_single_and_batch_entries(*kpis):
    """Warm the shared cache slots used by single and batch evaluation paths."""
    cache = get_kpi_cache()
    cache.clear()
    for kpi in kpis:
        cache.put(
            TEST_TENANT, kpi.model_id, kpi.id, "single",
            calc_agg_mode="automatic", definition_version="single-v1",
        )
        cache.put(
            TEST_TENANT, kpi.model_id, kpi.id, "batch",
            calc_agg_mode="aggregate_first", definition_version="batch-v1",
        )
    return cache


def _assert_entries_evicted(cache, *kpis):
    for kpi in kpis:
        assert cache.get(
            TEST_TENANT, kpi.model_id, kpi.id,
            calc_agg_mode="automatic", definition_version="single-v1",
        ) is None
        assert cache.get(
            TEST_TENANT, kpi.model_id, kpi.id,
            calc_agg_mode="aggregate_first", definition_version="batch-v1",
        ) is None


def _mutation_db(target, all_kpis, *, version=None):
    db = make_mock_db()
    db.get = AsyncMock(return_value=target)

    async def execute(stmt, *args, **kwargs):
        if "kpi_versions" in str(stmt):
            return _Result(single=version)
        return _Result(all_kpis)

    db.execute = AsyncMock(side_effect=execute)
    return db


def test_reverse_closure_is_independent_of_db_row_order():
    leaf = _kpi(name="C", expression="literal(1)")
    middle = _kpi(name="B", expression='kpi("C")')
    consumer = _kpi(name="A", expression='kpi("B")')
    expected = {leaf.id, middle.id, consumer.id}

    assert set(_dependent_kpi_ids([consumer, middle, leaf], leaf.id, {"C"})) == expected
    assert set(_dependent_kpi_ids([leaf, middle, consumer], leaf.id, {"C"})) == expected


@pytest.mark.asyncio
async def test_child_create_evicts_composite_owner_and_transitive_single_batch_entries(client):
    """Creating a child must invalidate a parent warmed before the child existed."""
    parent, child, consumer, unrelated = _composite_ownership_chain()
    cache = _warm_single_and_batch_entries(parent, consumer, unrelated)
    db = make_mock_db()
    db.get = AsyncMock(return_value=parent)
    db.execute = AsyncMock(
        return_value=_Result([parent, child, consumer, unrelated]),
    )

    async def refresh_created(created):
        for attr, value in vars(child).items():
            setattr(created, attr, value)

    db.refresh = AsyncMock(side_effect=refresh_created)

    try:
        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.acquire_model_definition_lock", new_callable=AsyncMock),
            patch("src.api.kpis._create_kpi_version", new_callable=AsyncMock),
            patch("src.api.kpis.audit", new_callable=AsyncMock),
            patch("src.api.kpis._validate_kpi_expression", new_callable=AsyncMock, return_value=None),
            patch("src.api.kpis._check_expression_cycles", new_callable=AsyncMock, return_value=[]),
        ):
            response = await client.post(
                PREFIX,
                json={
                    "name": child.name,
                    "expression": child.expression,
                    "parent_kpi_id": str(parent.id),
                    "target_type": "static",
                    "target_value": 100.0,
                    "weight": child.weight,
                },
            )

        assert response.status_code == 201
        _assert_entries_evicted(cache, parent, consumer)
        assert cache.get(
            TEST_TENANT, unrelated.model_id, unrelated.id,
            calc_agg_mode="aggregate_first", definition_version="batch-v1",
        ) == "batch"
    finally:
        cache.clear()


@pytest.mark.asyncio
async def test_update_evicts_target_only_transitive_single_and_batch_entries(client):
    target, consumer, transitive, unrelated = _target_only_chain()
    cache = _warm_single_and_batch_entries(consumer, transitive, unrelated)
    db = _mutation_db(target, [target, consumer, transitive, unrelated])

    try:
        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.acquire_model_definition_lock", new_callable=AsyncMock),
            patch("src.api.kpis._create_kpi_version", new_callable=AsyncMock),
            patch("src.api.kpis.audit", new_callable=AsyncMock),
            patch("src.api.kpis._validate_kpi_expression", new_callable=AsyncMock, return_value=None),
            patch("src.api.kpis._check_expression_cycles", new_callable=AsyncMock, return_value=[]),
        ):
            response = await client.patch(
                f"{PREFIX}/{target.id}",
                json={"target_expression": "literal(11)"},
            )

        assert response.status_code == 200
        _assert_entries_evicted(cache, consumer, transitive)
        assert cache.get(
            TEST_TENANT, unrelated.model_id, unrelated.id,
            calc_agg_mode="automatic", definition_version="single-v1",
        ) == "single"
    finally:
        cache.clear()


@pytest.mark.asyncio
async def test_delete_evicts_target_only_transitive_single_and_batch_entries(client):
    target, consumer, transitive, unrelated = _target_only_chain()
    cache = _warm_single_and_batch_entries(consumer, transitive, unrelated)
    db = _mutation_db(target, [target, consumer, transitive, unrelated])

    try:
        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.acquire_model_definition_lock", new_callable=AsyncMock),
            patch("src.api.kpis.audit", new_callable=AsyncMock),
            patch("src.api.kpis.purge_entity_soft_references", new_callable=AsyncMock),
        ):
            response = await client.delete(f"{PREFIX}/{target.id}")

        assert response.status_code == 204
        _assert_entries_evicted(cache, consumer, transitive)
        assert cache.get(
            TEST_TENANT, unrelated.model_id, unrelated.id,
            calc_agg_mode="aggregate_first", definition_version="batch-v1",
        ) == "batch"
    finally:
        cache.clear()


@pytest.mark.asyncio
async def test_revert_evicts_target_only_transitive_single_and_batch_entries(client):
    target, consumer, transitive, unrelated = _target_only_chain()
    cache = _warm_single_and_batch_entries(consumer, transitive, unrelated)
    version = types.SimpleNamespace(
        snapshot={"target_expression": "literal(11)"},
    )
    db = _mutation_db(
        target, [target, consumer, transitive, unrelated], version=version,
    )

    try:
        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.acquire_model_definition_lock", new_callable=AsyncMock),
            patch("src.api.kpis._create_kpi_version", new_callable=AsyncMock),
            patch("src.api.kpis.audit", new_callable=AsyncMock),
            patch("src.api.kpis._check_expression_cycles", new_callable=AsyncMock, return_value=[]),
        ):
            response = await client.post(
                f"{PREFIX}/{target.id}/versions/1/revert",
            )

        assert response.status_code == 200
        _assert_entries_evicted(cache, consumer, transitive)
        assert cache.get(
            TEST_TENANT, unrelated.model_id, unrelated.id,
            calc_agg_mode="automatic", definition_version="single-v1",
        ) == "single"
    finally:
        cache.clear()


@pytest.mark.asyncio
async def test_child_update_evicts_composite_owner_and_transitive_single_batch_entries(client):
    parent, child, consumer, unrelated = _composite_ownership_chain()
    cache = _warm_single_and_batch_entries(parent, consumer, unrelated)
    db = _mutation_db(child, [parent, child, consumer, unrelated])

    try:
        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.acquire_model_definition_lock", new_callable=AsyncMock),
            patch("src.api.kpis._create_kpi_version", new_callable=AsyncMock),
            patch("src.api.kpis.audit", new_callable=AsyncMock),
        ):
            response = await client.patch(
                f"{PREFIX}/{child.id}", json={"target_value": 75.0},
            )

        assert response.status_code == 200
        _assert_entries_evicted(cache, parent, consumer)
        assert cache.get(
            TEST_TENANT, unrelated.model_id, unrelated.id,
            calc_agg_mode="automatic", definition_version="single-v1",
        ) == "single"
    finally:
        cache.clear()


@pytest.mark.asyncio
async def test_child_delete_evicts_composite_owner_and_transitive_single_batch_entries(client):
    parent, child, consumer, unrelated = _composite_ownership_chain()
    cache = _warm_single_and_batch_entries(parent, consumer, unrelated)
    db = _mutation_db(child, [parent, child, consumer, unrelated])

    try:
        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.acquire_model_definition_lock", new_callable=AsyncMock),
            patch("src.api.kpis.audit", new_callable=AsyncMock),
            patch("src.api.kpis.purge_entity_soft_references", new_callable=AsyncMock),
        ):
            response = await client.delete(f"{PREFIX}/{child.id}")

        assert response.status_code == 204
        _assert_entries_evicted(cache, parent, consumer)
    finally:
        cache.clear()


@pytest.mark.asyncio
async def test_child_revert_evicts_composite_owner_and_transitive_single_batch_entries(client):
    parent, child, consumer, unrelated = _composite_ownership_chain()
    cache = _warm_single_and_batch_entries(parent, consumer, unrelated)
    db = _mutation_db(
        child, [parent, child, consumer, unrelated],
        version=types.SimpleNamespace(snapshot={"target_value": 75.0}),
    )

    try:
        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.acquire_model_definition_lock", new_callable=AsyncMock),
            patch("src.api.kpis._create_kpi_version", new_callable=AsyncMock),
            patch("src.api.kpis.audit", new_callable=AsyncMock),
            patch("src.api.kpis._check_expression_cycles", new_callable=AsyncMock, return_value=[]),
        ):
            response = await client.post(f"{PREFIX}/{child.id}/versions/1/revert")

        assert response.status_code == 200
        _assert_entries_evicted(cache, parent, consumer)
    finally:
        cache.clear()


@pytest.mark.asyncio
async def test_child_parent_reassignment_evicts_old_new_owners_and_consumers(client):
    old_parent, new_parent, child, old_consumer, new_consumer = _reassigned_ownership_chain()
    cache = _warm_single_and_batch_entries(
        old_parent, new_parent, old_consumer, new_consumer,
    )
    db = _mutation_db(child, [old_parent, new_parent, child, old_consumer, new_consumer])
    # update_kpi calls:
    #   1. db.get(KPI, kpi_id) -> child
    #   2. db.get(KPI, updates["parent_kpi_id"]) -> new_parent (validate parent)
    #   3. _would_cycle calls db.get(KPI, new_parent.id) -> new_parent (walk ancestor chain)
    db.get = AsyncMock(side_effect=[child, new_parent, new_parent])

    try:
        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.acquire_model_definition_lock", new_callable=AsyncMock),
            patch("src.api.kpis._create_kpi_version", new_callable=AsyncMock),
            patch("src.api.kpis.audit", new_callable=AsyncMock),
            patch("src.api.kpis._check_expression_cycles", new_callable=AsyncMock, return_value=[]),
        ):
            response = await client.patch(
                f"{PREFIX}/{child.id}", json={"parent_kpi_id": str(new_parent.id)},
            )

        assert response.status_code == 200
        _assert_entries_evicted(cache, old_parent, new_parent, old_consumer, new_consumer)
    finally:
        cache.clear()


@pytest.mark.asyncio
async def test_child_parent_revert_evicts_current_and_restored_owner_closures(client):
    old_parent, new_parent, child, old_consumer, new_consumer = _reassigned_ownership_chain()
    child.parent_kpi_id = new_parent.id
    cache = _warm_single_and_batch_entries(
        old_parent, new_parent, old_consumer, new_consumer,
    )
    db = _mutation_db(
        child, [old_parent, new_parent, child, old_consumer, new_consumer],
        version=types.SimpleNamespace(snapshot={"parent_kpi_id": str(old_parent.id)}),
    )
    # revert_kpi_version calls:
    #   1. db.get(KPI, kpi_id) -> child
    #   2. db.get(KPI, ref_id) -> old_parent (validate parent_kpi_id FK)
    #   3. _would_cycle calls db.get(KPI, old_parent.id) -> old_parent (walk ancestor chain)
    db.get = AsyncMock(side_effect=[child, old_parent, old_parent])

    try:
        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.acquire_model_definition_lock", new_callable=AsyncMock),
            patch("src.api.kpis._create_kpi_version", new_callable=AsyncMock),
            patch("src.api.kpis.audit", new_callable=AsyncMock),
            patch("src.api.kpis._check_expression_cycles", new_callable=AsyncMock, return_value=[]),
        ):
            response = await client.post(f"{PREFIX}/{child.id}/versions/1/revert")

        assert response.status_code == 200
        _assert_entries_evicted(cache, old_parent, new_parent, old_consumer, new_consumer)
    finally:
        cache.clear()
