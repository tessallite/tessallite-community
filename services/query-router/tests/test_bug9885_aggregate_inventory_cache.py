"""Bug-9885/Bug-9938 — reuse session-free aggregate inventory across requests.

``load_active_aggregates`` / ``load_inactive_aggregates`` return the model's
WHOLE aggregate inventory: every definition, every column, and the Measure
behind each column. The router asks for it on EVERY query, and an unrestricted
``MDSCHEMA_MEMBERS`` Discover issues one ``/discover/members`` request per
dimension (~110 on ``modely_technical``) — so one user action hydrated the same
thousands of ORM rows ~110 times. Measured at ~250 ms of Python per request,
~85% of the request's CPU, and the reason a cold unrestricted Discover cost
~45 s.

Aggregates are LIVE state, not deployed-snapshot state, so the reuse is NOT
time-bounded: every call re-reads a cheap identity and rehydrates the moment
anything that identity covers moves. These tests pin both halves — the reuse
and the invalidation — at the loader's own boundary, with the identity row as
the input, because that is exactly what the cache decides on.

Test escape: nothing asserted how many times the inventory was loaded, so a
per-request rehydration was invisible to every existing test; and no test
drove the same model through concurrent sessions, allowing live ORM rows from
one request to be reused by another.
Guard: this file.
Tier: T3 — a stale inventory is a wrong-numbers routing risk.
"""
from __future__ import annotations

import asyncio
import dataclasses
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from shared.db.models import AggregateDefinition

from src.semantic import binder
from src.semantic.binder import (
    invalidate_aggregate_inventory,
    load_active_aggregates,
    load_inactive_aggregates,
)

from result_fakes import ScalarResult

MODEL_ID = "11111111-1111-1111-1111-111111111111"
OTHER_MODEL_ID = "22222222-2222-2222-2222-222222222222"
THIRD_MODEL_ID = "33333333-3333-3333-3333-333333333333"


def _identity(
    *,
    definitions: str = "digest-definitions",
    column_count: str = "6",
    column_created: str = "2026-09-01T00:00:00",
    policy_count: str = "2",
    policy_updated: str = "2026-09-01T00:00:00",
    measure_count: str = "3",
    measure_updated: str = "2026-09-01T00:00:00",
    deployed_version_id: str = "v-1",
    deploy_epoch: str = "7",
) -> tuple:
    return (
        definitions,
        column_count, column_created,
        policy_count, policy_updated,
        measure_count, measure_updated,
        deployed_version_id, deploy_epoch,
    )


def _aggregate(status: str, name: str):
    """A definition shaped like the loaded ORM graph the matcher reads."""
    measure = SimpleNamespace(id=uuid.uuid4(), name=f"{name}_measure")
    column = SimpleNamespace(
        id=uuid.uuid4(), physical_col_name=f"{name}_col", measure=measure,
    )
    return SimpleNamespace(
        id=uuid.uuid4(), status=status, grain=[name], columns=[column],
        refresh_policy=SimpleNamespace(cron_expression="0 * * * *"),
        persona_id=None,
    )


class FakeResult:
    """Bug-9012: ``scalars()`` returns a distinct ``ScalarResult`` projection,
    never the receiver, so a caller that mistakenly calls a Result-only
    accessor after ``scalars()`` cannot pass here by accident."""

    def __init__(self, row=None, objects=None):
        self._row = row
        self._objects = objects

    def one(self):
        return self._row

    def scalars(self):
        return ScalarResult(self._objects or [])


class FakeSession:
    """Records what the loader asked the database for.

    ``identity`` is the row the identity probe returns; assign a new one to
    simulate a change to the model's aggregates, measures, or deploy pointer.
    """

    def __init__(self, identity: tuple, active: list, inactive: list):
        self.identity = identity
        self.active = active
        self.inactive = inactive
        self.identity_queries = 0
        self.hydration_queries = 0
        self.info: dict = {}

    async def execute(self, stmt):
        entities = [d.get("entity") for d in stmt.column_descriptions]
        if AggregateDefinition not in entities:
            self.identity_queries += 1
            return FakeResult(row=self.identity)
        self.hydration_queries += 1
        if "status != " in str(stmt):
            return FakeResult(objects=self.inactive)
        return FakeResult(objects=self.active)


class BlockingHydrationSession(FakeSession):
    """Pause one hydration so a model-scoped deploy eviction can interleave."""

    def __init__(self, identity: tuple, active: list, inactive: list):
        super().__init__(identity, active, inactive)
        self.hydration_started = asyncio.Event()
        self.release_hydration = asyncio.Event()
        self._block_once = True

    async def execute(self, stmt):
        entities = [d.get("entity") for d in stmt.column_descriptions]
        if AggregateDefinition in entities and self._block_once:
            self._block_once = False
            self.hydration_started.set()
            await self.release_hydration.wait()
        return await super().execute(stmt)

@pytest.fixture(autouse=True)
def _clean_cache():
    invalidate_aggregate_inventory()
    yield
    invalidate_aggregate_inventory()


def _session(identity=None):
    return FakeSession(
        identity or _identity(),
        [_aggregate("active", "sales")],
        [_aggregate("retired", "legacy")],
    )


@pytest.mark.asyncio
async def test_bug9885_second_load_reuses_the_hydrated_inventory():
    """The Discover fan-out's 2nd..Nth request must not rehydrate the model."""
    db = _session()

    first_active = await load_active_aggregates(MODEL_ID, db)
    first_inactive = await load_inactive_aggregates(MODEL_ID, db)
    assert db.hydration_queries == 2  # active + inactive, once

    second_active = await load_active_aggregates(MODEL_ID, db)
    second_inactive = await load_inactive_aggregates(MODEL_ID, db)

    assert db.hydration_queries == 2, "inventory was hydrated again"
    # ... and the reuse is not a cheaper, different answer.
    assert second_active == first_active
    assert second_inactive == first_inactive
    assert [c.measure.name for a in second_active for c in a.columns] == [
        c.measure.name for a in first_active for c in a.columns
    ]
    assert second_active[0].refresh_policy.cron_expression == "0 * * * *"


@pytest.mark.asyncio
async def test_bug9885_caller_cannot_corrupt_the_cached_inventory():
    """Each caller gets its own list; mutating it must not poison the cache."""
    db = _session()
    first = await load_active_aggregates(MODEL_ID, db)
    first.clear()
    assert len(await load_active_aggregates(MODEL_ID, db)) == 1


@pytest.mark.asyncio
async def test_bug9938_cached_definitions_are_session_free_value_objects():
    """The process cache uses frozen values; this corrects the interim design.

    The earlier session-local test pinned Bug-9938's isolation fix but also
    pinned the Bug-9885 regression. The contract is both cross-request reuse
    and no ORM graph crossing a session, so this assertion is a design
    correction, not a weakening of the guard.
    """
    db = _session()
    active = await load_active_aggregates(MODEL_ID, db)
    inactive = await load_inactive_aggregates(MODEL_ID, db)

    cached = binder._AGG_INVENTORY_CACHE[MODEL_ID]
    assert cached[2] == tuple(active)
    assert cached[3] == tuple(inactive)
    assert not isinstance(cached[2][0], AggregateDefinition)
    assert isinstance(cached[2][0].columns, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cached[2][0].status = "inactive"
    assert await load_active_aggregates(MODEL_ID, db) == active


@pytest.mark.parametrize(
    "changed",
    [
        pytest.param({"definitions": "changed"}, id="definition_added_or_edited"),
        pytest.param({"column_count": "7"}, id="aggregate_column_added"),
        pytest.param({"column_created": "2026-09-02T00:00:00"}, id="columns_rebuilt"),
        pytest.param({"policy_count": "1"}, id="refresh_policy_removed"),
        pytest.param({"policy_updated": "2026-09-02T00:00:00"}, id="cron_edited"),
        pytest.param({"measure_count": "4"}, id="measure_added"),
        pytest.param({"measure_updated": "2026-09-02T00:00:00"}, id="measure_renamed"),
        pytest.param({"deployed_version_id": "v-2"}, id="deployed_version_changed"),
        pytest.param({"deploy_epoch": "8"}, id="deploy_epoch_bumped"),
    ],
)
@pytest.mark.asyncio
async def test_bug9885_inventory_change_rehydrates(changed):
    """Anything that can change what may serve must miss the cache.

    ``deploy_epoch``/``deployed_version_id`` cover deploy and revert (a revert
    to the same version bumps only the epoch), the rest cover the live
    aggregate lifecycle.
    """
    db = _session()
    await load_active_aggregates(MODEL_ID, db)
    assert db.hydration_queries == 2  # active + inactive, once

    db.identity = _identity(**changed)
    db.active = [_aggregate("active", "rebuilt")]
    refreshed = await load_active_aggregates(MODEL_ID, db)

    assert db.hydration_queries == 4, "stale inventory served after a change"
    assert refreshed[0].grain == ("rebuilt",)


@pytest.mark.asyncio
async def test_bug9885_identity_ignores_the_routers_own_hit_bookkeeping():
    """The router credits a hit/miss on aggregate_definitions after routing.

    Those writes bump ``updated_at`` and the EMA counters without changing what
    may serve. An identity that watched them rehydrated the whole inventory
    mid-Discover (measured: 16 times across 123 dimensions), which is most of
    the cost this cache exists to remove. Everything that DOES decide
    servability must still be watched — this asserts both halves against the
    SQL actually issued.
    """
    captured: list = []

    class Capturing(FakeSession):
        async def execute(self, stmt):
            captured.append(str(stmt.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )))
            return await super().execute(stmt)

    await load_active_aggregates(MODEL_ID, Capturing(_identity(), [], []))
    identity_sql = captured[0]

    for bookkeeping in ("hit_count", "estimated_hit_rate"):
        assert bookkeeping not in identity_sql
    assert "aggregate_definitions.updated_at" not in identity_sql
    # ... while everything that decides whether an aggregate may serve is read.
    for observed in (
        "aggregate_definitions.status",
        "aggregate_definitions.grain",
        "aggregate_definitions.last_refreshed_at",
        "aggregate_definitions.built_for_version_id",
        "aggregate_definitions.built_for_epoch",
        "aggregate_definitions.persona_id",
        "aggregate_columns.created_at",
        "aggregate_refresh_policies.updated_at",
        "measures.updated_at",
        "models.deployed_version_id",
        "models.deploy_epoch",
    ):
        assert observed in identity_sql, observed


@pytest.mark.asyncio
async def test_bug9885_identity_is_read_on_every_load():
    """The reuse is not time-bounded, so the identity is never assumed."""
    db = _session()
    for _ in range(4):
        await load_active_aggregates(MODEL_ID, db)
    assert db.identity_queries == 4


@pytest.mark.asyncio
async def test_bug9885_inventory_is_scoped_per_model():
    """One model's inventory can never be served for another."""
    db_a = _session()
    db_b = FakeSession(
        _identity(), [_aggregate("active", "other_model")], [],
    )
    a = await load_active_aggregates(MODEL_ID, db_a)
    b = await load_active_aggregates(OTHER_MODEL_ID, db_b)
    assert a[0].grain == ("sales",)
    assert b[0].grain == ("other_model",)
    assert db_b.hydration_queries == 2


@pytest.mark.asyncio
async def test_bug9938_concurrent_sessions_never_share_inventory_rows():
    """Concurrent requests share only frozen values, never ORM inventory rows."""
    db_a = _session()
    db_b = FakeSession(
        _identity(), [_aggregate("active", "other_session")], [],
    )

    await load_active_aggregates(MODEL_ID, db_a)
    active_a, active_b = await asyncio.gather(
        load_active_aggregates(MODEL_ID, db_a),
        load_active_aggregates(MODEL_ID, db_b),
    )

    assert active_a[0].grain == ("sales",)
    assert active_b[0].grain == ("sales",)
    assert active_a[0] is active_b[0]
    assert not isinstance(active_b[0], AggregateDefinition)
    assert db_b.hydration_queries == 0


@pytest.mark.asyncio
async def test_bug9885_inventory_carries_no_principal_or_persona_scope():
    """The inventory is the model's whole aggregate set, for every caller.

    Persona and row-security scoping is the matcher's job (a persona-scoped
    aggregate is selected by ``persona_id`` at match time), so the cache key
    must stay model-only — keying it on the principal would both defeat the
    reuse and invent a second, weaker security boundary.
    """
    persona_scoped = _aggregate("active", "persona_only")
    persona_scoped.persona_id = uuid.uuid4()
    db = FakeSession(_identity(), [_aggregate("active", "global"), persona_scoped], [])

    loaded = await load_active_aggregates(MODEL_ID, db)

    assert {a.grain[0] for a in loaded} == {"global", "persona_only"}
    assert set(binder._AGG_INVENTORY_CACHE) == {MODEL_ID}


@pytest.mark.asyncio
async def test_bug9885_explicit_invalidation_forces_a_rehydrate():
    """``evict_model_cache`` drops the entry on the replica it reaches."""
    db = _session()
    await load_active_aggregates(MODEL_ID, db)
    invalidate_aggregate_inventory(MODEL_ID)
    await load_active_aggregates(MODEL_ID, db)
    assert db.hydration_queries == 4


@pytest.mark.asyncio
async def test_bug9942_model_eviction_cannot_be_undone_by_an_inflight_load():
    """A deploy eviction remains effective across an in-flight hydration."""
    db = BlockingHydrationSession(
        _identity(),
        [_aggregate("active", "before_deploy")],
        [_aggregate("retired", "legacy")],
    )
    load = asyncio.create_task(load_active_aggregates(MODEL_ID, db))
    await db.hydration_started.wait()

    invalidate_aggregate_inventory(MODEL_ID)
    db.release_hydration.set()
    await load

    assert MODEL_ID not in binder._AGG_INVENTORY_CACHE
    await load_active_aggregates(MODEL_ID, db)
    assert db.hydration_queries == 4, (
        "the post-deploy request reused an in-flight pre-eviction inventory"
    )


@pytest.mark.asyncio
async def test_bug9942_inventory_cache_evicts_at_its_model_capacity(monkeypatch):
    """The process-local inventory stays bounded when models exceed capacity."""
    monkeypatch.setattr(binder, "_AGG_INVENTORY_MAX_MODELS", 2)

    await load_active_aggregates(MODEL_ID, _session())
    await load_active_aggregates(OTHER_MODEL_ID, _session())
    await load_active_aggregates(THIRD_MODEL_ID, _session())

    assert len(binder._AGG_INVENTORY_CACHE) == 2
    assert MODEL_ID not in binder._AGG_INVENTORY_CACHE
    assert {OTHER_MODEL_ID, THIRD_MODEL_ID} == set(binder._AGG_INVENTORY_CACHE)


@pytest.mark.asyncio
async def test_bug9938_inventory_cache_is_global_but_session_free():
    """Cross-request reuse is safe because the cached graph is immutable data.

    This replaces the interim session-local assertion. Requiring separate
    per-session inventories would preserve the Bug-9885 hydration regression;
    sharing a frozen value object is the intended design correction.
    """
    db_a = _session()
    db_b = _session()

    active_a = await load_active_aggregates(MODEL_ID, db_a)
    active_b = await load_active_aggregates(MODEL_ID, db_b)

    assert active_a[0] is active_b[0]
    assert not isinstance(active_b[0], AggregateDefinition)
    assert db_a.hydration_queries == 2
    assert db_b.hydration_queries == 0
