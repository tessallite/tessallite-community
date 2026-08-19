"""Bug-7981/Bug-8251 join-graph publication coherence contracts.

These tests separate correctness from eager eviction.  The durable deployment
identity is ``(model_id, deployed_version_id, deploy_epoch)``.  A local cache
eviction can reclaim memory sooner, but a request that observes a newly
committed version/epoch must miss every entry created for the prior identity,
including an entry refilled between pre-commit eviction and commit.

Test escape: snapshot-cache tests covered epoch identity for semantic shapes,
but no test exercised the join-graph primitive's pre-commit refill ordering or
modelled two independent replica dictionaries.  Guard: these tests call the
production cache primitive and graph loader with old/new deployment identities.
Tier: T1 deployment producer/consumer contract.
"""
from __future__ import annotations

import time
import types
import uuid
from unittest.mock import patch

import pytest

from src.rewrite import join_graph_cache as cache_module
from src.rewrite.join_graph_cache import (
    _get_join_graph,
    _put_join_graph,
    invalidate_join_graph_cache,
)
from src.rewrite.table_resolution import _load_model_graph
from src.semantic import snapshot_resolver
from src.semantic.snapshot_resolver import (
    resolve_deployed_shape as _real_resolve_deployed_shape,
)


def _put_marker(key: tuple[str, str, int], marker: str) -> None:
    _put_join_graph(
        key,
        tables_by_id={"table": marker},
        joins=[marker],
        columns_by_id={"column": marker},
        uda_by_id={"uda": marker},
    )


def test_precommit_refill_cannot_satisfy_postcommit_epoch_key() -> None:
    """A refill after eager eviction remains reachable only by the old key."""
    model_id = str(uuid.uuid4())
    version_id = str(uuid.uuid4())
    old_key = (model_id, version_id, 7)
    committed_key = (model_id, version_id, 8)

    _put_marker(old_key, "before-eviction")
    invalidate_join_graph_cache(model_id)

    # A query that read the durable model row before commit may refill V7/E7.
    _put_marker(old_key, "precommit-refill")

    old_entry = _get_join_graph(old_key)
    assert old_entry is not None
    assert old_entry.tables_by_id == {"table": "precommit-refill"}
    assert _get_join_graph(committed_key) is None


def test_sibling_replica_rejects_old_graph_without_broadcast_eviction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only replica A is evicted; replica B still cannot hit with the new key."""
    model_id = str(uuid.uuid4())
    old_key = (model_id, str(uuid.uuid4()), 3)
    committed_key = (model_id, str(uuid.uuid4()), 4)
    replica_a: dict = {}
    replica_b: dict = {}

    monkeypatch.setattr(cache_module, "_join_graph_cache", replica_a)
    _put_marker(old_key, "replica-a-old")

    monkeypatch.setattr(cache_module, "_join_graph_cache", replica_b)
    _put_marker(old_key, "replica-b-old")

    # The model-service eviction request reaches replica A only.
    monkeypatch.setattr(cache_module, "_join_graph_cache", replica_a)
    invalidate_join_graph_cache(model_id)
    assert not replica_a

    # Replica B retains the old process-local entry, but a request observing
    # the committed deployment computes a different key and cannot consume it.
    monkeypatch.setattr(cache_module, "_join_graph_cache", replica_b)
    retained_old = _get_join_graph(old_key)
    assert retained_old is not None
    assert retained_old.tables_by_id == {"table": "replica-b-old"}
    assert _get_join_graph(committed_key) is None


class _VersionDB:
    def __init__(self, versions: dict[uuid.UUID, object]) -> None:
        self._versions = versions
        self.get_calls: list[uuid.UUID] = []

    async def get(self, _model_type: type, version_id: uuid.UUID) -> object | None:
        self.get_calls.append(version_id)
        return self._versions.get(version_id)


def _snapshot(table_id: uuid.UUID, column_id: uuid.UUID, name: str) -> dict:
    return {
        "tables": [
            {
                "id": str(table_id),
                "physical_name": name,
                "table_type": "fact",
            }
        ],
        "columns": [
            {
                "id": str(column_id),
                "model_table_id": str(table_id),
                "column_name": f"{name}_amount",
                "data_type": "numeric",
            }
        ],
        "joins": [],
        "user_defined_attributes": [],
    }


@pytest.mark.asyncio
async def test_graph_loader_rebuilds_from_new_committed_snapshot_identity() -> None:
    """The serving loader uses the selected immutable snapshot on a key miss."""
    model_id = uuid.uuid4()
    v1_id = uuid.uuid4()
    v2_id = uuid.uuid4()
    v1_table = uuid.uuid4()
    v2_table = uuid.uuid4()
    v1_column = uuid.uuid4()
    v2_column = uuid.uuid4()
    db = _VersionDB(
        {
            v1_id: types.SimpleNamespace(
                snapshot_json=_snapshot(v1_table, v1_column, "sales_v1")
            ),
            v2_id: types.SimpleNamespace(
                snapshot_json=_snapshot(v2_table, v2_column, "sales_v2")
            ),
        }
    )
    model = types.SimpleNamespace(
        id=model_id,
        deployed_version_id=v1_id,
        deploy_epoch=10,
    )
    bound_query = types.SimpleNamespace(model=model, deployed_shape=None)

    with patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        new=_real_resolve_deployed_shape,
    ):
        first_tables, _, first_columns, _ = await _load_model_graph(
            bound_query, db, set()
        )
    assert first_tables[v1_table].physical_name == "sales_v1"
    assert first_columns[v1_column].column_name == "sales_v1_amount"

    # Publish V2/E11. No explicit cache invalidation is performed here: the
    # deployment identity alone must make the V1/E10 graph unreachable.
    model.deployed_version_id = v2_id
    model.deploy_epoch = 11
    with patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        new=_real_resolve_deployed_shape,
    ):
        second_tables, _, second_columns, _ = await _load_model_graph(
            bound_query, db, set()
        )

    assert second_tables[v2_table].physical_name == "sales_v2"
    assert second_columns[v2_column].column_name == "sales_v2_amount"
    assert v1_table not in second_tables
    assert v1_column not in second_columns
    assert db.get_calls == [v1_id, v2_id]


class _DeletedVersionDB(_VersionDB):
    def __init__(self, versions: dict[uuid.UUID, object]) -> None:
        super().__init__(versions)
        self.live_graph_reads = 0

    async def execute(self, _statement: object) -> object:
        self.live_graph_reads += 1
        raise AssertionError(
            "a deployed in-flight request must not fall through to live graph rows"
        )


@pytest.mark.asyncio
async def test_backward_revert_uses_bound_snapshot_after_old_version_deleted() -> None:
    """Regression: backward revert cannot mix old semantics with the live graph.

    The binder resolves V2/E2 before the revert. The revert then deletes V2 and
    eager eviction removes the old join-graph entry. Source/raw rewriting must
    reuse the V2 shape cached during binding; it must not independently fetch
    the deleted version and query post-revert live V1 rows.
    """
    snapshot_resolver.invalidate()
    invalidate_join_graph_cache()
    model_id = uuid.uuid4()
    v2_id = uuid.uuid4()
    v2_table = uuid.uuid4()
    v2_column = uuid.uuid4()
    db = _DeletedVersionDB(
        {
            v2_id: types.SimpleNamespace(
                snapshot_json={
                    **_snapshot(v2_table, v2_column, "sales_v2"),
                    "measures": [
                        {
                            "id": str(uuid.uuid4()),
                            "name": "revenue",
                            "default_agg": "sum",
                            "measure_type": "standard",
                            "data_type": "numeric",
                            "source_column_id": str(v2_column),
                        }
                    ],
                }
            )
        }
    )
    model = types.SimpleNamespace(
        id=model_id,
        slug="modelx",
        deployed_version_id=v2_id,
        deploy_epoch=2,
    )
    bound_query = types.SimpleNamespace(
        model=model,
        deployed_shape=None,
        logical_query=types.SimpleNamespace(
            from_tables=["modelx"],
            raw_query="SELECT * FROM modelx",
            input_dialect="postgres",
        ),
    )

    # Binder-stage selection: cache the immutable V2/E2 semantic + graph shape.
    selected_shape = await _real_resolve_deployed_shape(model, db)
    assert selected_shape is not None
    assert db.get_calls == [v2_id]
    bound_query.deployed_shape = selected_shape

    # Backward revert commits V1 and deletes V2. The local eviction endpoint has
    # also removed any already-built V2 join graph, forcing the exact old-key
    # refill seam that previously fell through to live ORM rows.
    db._versions.clear()
    invalidate_join_graph_cache(model_id)
    snapshot_resolver.invalidate(model_id)

    with patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        new=_real_resolve_deployed_shape,
    ):
        tables, joins, columns, udas = await _load_model_graph(
            bound_query, db, set()
        )

    assert tables[v2_table].physical_name == "sales_v2"
    assert columns[v2_column].column_name == "sales_v2_amount"
    assert joins == []
    assert udas == {}
    assert db.get_calls == [v2_id], (
        "graph hydration must hit the binder-selected snapshot cache, not "
        "re-fetch the version row after revert deleted it"
    )
    assert db.live_graph_reads == 0

    # Traverse a real source-SQL wrapper consumer. It must forward the same
    # request shape rather than reconstructing `_BoundLike(model)` without it.
    from src.rewrite.source_sql import _substitute_table_names

    invalidate_join_graph_cache(model_id)
    substituted = await _substitute_table_names(bound_query, db)
    assert substituted is not None
    assert "sales_v2" in substituted
    assert "modelx" not in substituted.lower()
    cached_v2 = _get_join_graph((str(model_id), str(v2_id), 2))
    assert cached_v2 is not None
    assert {table.physical_name for table in cached_v2.tables_by_id.values()} == {
        "sales_v2"
    }
    assert db.live_graph_reads == 0

    # A helper that does NOT thread the request shape still stays coherent:
    # ``resolve_deployed_shape`` is request-pinned, so the SAME immutable V2
    # shape is returned even though the process cache was cleared and the V2
    # version row is gone. This is the structural property that replaced
    # per-call-site shape threading (Bug-7981 round 4) — it must never read
    # live rows and must never publish live rows under the V2/E2 key.
    from src.rewrite.snapshot_graph_resolvers import resolve_all_tables

    invalidate_join_graph_cache(model_id)
    snapshot_resolver.invalidate(model_id)
    with patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        new=_real_resolve_deployed_shape,
    ):
        without_request_shape = await resolve_all_tables(model, db)
    assert [t.physical_name for t in without_request_shape] == ["sales_v2"]
    cached_after_helper = _get_join_graph((str(model_id), str(v2_id), 2))
    assert cached_after_helper is not None
    assert {
        t.physical_name for t in cached_after_helper.tables_by_id.values()
    } == {"sales_v2"}
    assert db.live_graph_reads == 0

    # A genuinely NEW request (no pin) for a deployment whose version row is
    # gone must still fail closed rather than reading post-revert live rows.
    from src.ir.logical_query import DeployedSnapshotUnavailableError

    invalidate_join_graph_cache(model_id)
    snapshot_resolver.invalidate(model_id)
    snapshot_resolver.reset_request_pins()
    with patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        new=_real_resolve_deployed_shape,
    ):
        with pytest.raises(DeployedSnapshotUnavailableError):
            await resolve_all_tables(model, db)
    assert _get_join_graph((str(model_id), str(v2_id), 2)) is None
    assert db.live_graph_reads == 0


# ---------------------------------------------------------------------------
# Bug-7981 round 4 - the request-scoped deployment pin is the structural fix.
#
# Rounds 1-3 threaded ``BoundQuery.deployed_shape`` through one more consumer
# each time and each review round found the next unthreaded consumer. The pin
# makes ``resolve_deployed_shape`` idempotent within a request, so EVERY
# consumer -- including ones nobody enumerated -- gets the identical immutable
# shape. These tests assert that property directly, not one call site at a time.
# ---------------------------------------------------------------------------


def _deployed_model_and_db(*, epoch: int = 2, with_tables: bool = True):
    """A deployed model whose V/E snapshot can be deleted mid-request."""
    model_id = uuid.uuid4()
    version_id = uuid.uuid4()
    table_id = uuid.uuid4()
    column_id = uuid.uuid4()
    snapshot = (
        _snapshot(table_id, column_id, "sales_v2")
        if with_tables
        else {"tables": [], "columns": [], "joins": [],
              "user_defined_attributes": []}
    )
    snapshot["measures"] = [
        {
            "id": str(uuid.uuid4()),
            "name": "revenue",
            "default_agg": "sum",
            "measure_type": "standard",
            "data_type": "numeric",
            "source_column_id": str(column_id),
        }
    ]
    db = _DeletedVersionDB(
        {version_id: types.SimpleNamespace(snapshot_json=snapshot)}
    )
    model = types.SimpleNamespace(
        id=model_id, slug="modelx",
        deployed_version_id=version_id, deploy_epoch=epoch,
    )
    return model, db, version_id, table_id, column_id


@pytest.mark.asyncio
async def test_unthreaded_consumer_gets_the_pinned_shape_after_revert() -> None:
    """The core Bug-7981 property: a consumer that never receives the shape
    still resolves the request's IDENTICAL immutable shape after the process
    cache is evicted and the version row is deleted."""
    snapshot_resolver.invalidate()
    snapshot_resolver.reset_request_pins()
    model, db, vid, _tid, _cid = _deployed_model_and_db()

    # Binder-stage resolution pins the shape for the rest of this request.
    bound_shape = await _real_resolve_deployed_shape(model, db)
    assert bound_shape is not None

    # Concurrent backward revert: version row deleted, process cache evicted.
    db._versions.clear()
    snapshot_resolver.invalidate(model.id)

    later = await _real_resolve_deployed_shape(model, db)
    assert later is bound_shape, (
        "an unthreaded consumer must get the SAME shape object, not None"
    )
    assert db.get_calls == [vid], "no second version-row fetch may occur"


@pytest.mark.asyncio
async def test_calc_dependency_resolution_survives_revert_within_request() -> None:
    """Bug-7981 round 3 (HIGH): calc-measure dependency resolution used to
    re-resolve the deployment independently, so an already-bound request lost
    its dependencies after a backward revert. It now consumes the request pin.

    Test escape: no test drove ``resolve_calc_dependency_measures`` after a
    mid-request version deletion. Guard: this test. Tier: T2.
    """
    snapshot_resolver.invalidate()
    snapshot_resolver.reset_request_pins()
    model, db, _vid, _tid, _cid = _deployed_model_and_db()

    assert await _real_resolve_deployed_shape(model, db) is not None

    db._versions.clear()
    snapshot_resolver.invalidate(model.id)

    with patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        new=_real_resolve_deployed_shape,
    ):
        resolved = await snapshot_resolver.resolve_calc_dependency_measures(
            model, db, {"revenue"},
        )
        assert set(resolved) == {"revenue"}
        assert db.live_graph_reads == 0

        # A NEW request (no pin) must fail closed instead of reading live rows.
        snapshot_resolver.reset_request_pins()
        assert await snapshot_resolver.resolve_calc_dependency_measures(
            model, db, {"revenue"},
        ) == {}


@pytest.mark.asyncio
async def test_deployed_shape_without_tables_fails_closed_and_caches_nothing() -> None:
    """Bug-7981 round 3 (CRITICAL): a deployed shape with semantic members but
    NO snapshot tables used to fall through to live ORM rows and publish those
    mutable draft rows under the deployed version/epoch cache key.

    Test escape: the fall-through was only reachable with a semantic-only
    snapshot, which no test constructed. Guard: this test. Tier: T2.
    """
    from src.ir.logical_query import DeployedSnapshotUnavailableError

    snapshot_resolver.invalidate()
    snapshot_resolver.reset_request_pins()
    invalidate_join_graph_cache()
    model, db, vid, _tid, _cid = _deployed_model_and_db(with_tables=False)

    shape = await _real_resolve_deployed_shape(model, db)
    assert shape is not None and not shape.tables_by_id
    bound_query = types.SimpleNamespace(model=model, deployed_shape=shape)

    with pytest.raises(DeployedSnapshotUnavailableError):
        await _load_model_graph(bound_query, db, set())

    assert db.live_graph_reads == 0, "no live ORM read may occur"
    assert _get_join_graph((str(model.id), str(vid), 2)) is None, (
        "a blocked deployed request must not publish a cache entry"
    )


@pytest.mark.asyncio
async def test_request_pins_do_not_leak_between_concurrent_requests() -> None:
    """Two requests run in separate asyncio Tasks -- the same mechanism ASGI
    uses per HTTP request -- must not share pins."""
    import asyncio

    snapshot_resolver.invalidate()
    snapshot_resolver.reset_request_pins()
    model, db, vid, _tid, _cid = _deployed_model_and_db()
    key = (str(model.id), str(vid), 2)

    async def _request_a():
        shape = await _real_resolve_deployed_shape(model, db)
        assert snapshot_resolver._pinned_shape(key) is shape
        return shape

    async def _request_b():
        # Independent Task -> independent context -> no inherited pin.
        return snapshot_resolver._pinned_shape(key)

    shape_a = await asyncio.create_task(_request_a())
    assert shape_a is not None
    assert await asyncio.create_task(_request_b()) is None
    assert snapshot_resolver._pinned_shape(key) is None, (
        "a pin set inside a request Task must not escape into the parent"
    )


@pytest.mark.asyncio
async def test_request_pin_registry_is_bounded() -> None:
    """Bug-8503 discipline: the pin registry cannot grow without limit even if
    a context is reused across many logical requests."""
    snapshot_resolver.reset_request_pins()
    for _ in range(snapshot_resolver._MAX_REQUEST_PINS * 3):
        model, db, _vid, _tid, _cid = _deployed_model_and_db()
        assert await _real_resolve_deployed_shape(model, db) is not None
    pins = snapshot_resolver._REQUEST_PINS.get()
    assert pins is not None
    assert len(pins) <= snapshot_resolver._MAX_REQUEST_PINS


# ---------------------------------------------------------------------------
# Bug-8503 / Bug-8251 - join-graph cache retention and per-replica self-healing
# ---------------------------------------------------------------------------


def test_join_graph_cache_enforces_a_hard_size_bound() -> None:
    """Bug-8503: superseded keys are never looked up again, so lookup-time
    expiry alone leaked a full graph per deploy. The cache is now bounded."""
    invalidate_join_graph_cache()
    limit = cache_module._JOIN_GRAPH_MAX_ENTRIES
    for i in range(limit + 40):
        _put_marker((str(uuid.uuid4()), str(uuid.uuid4()), i), f"entry-{i}")
    assert len(cache_module._join_graph_cache) <= limit


def test_join_graph_cache_sweeps_expired_entries_on_insert() -> None:
    """Bug-8503: an expired entry for a key nobody looks up again is reclaimed
    by the next insert, not left resident until process restart."""
    invalidate_join_graph_cache()
    dead_key = (str(uuid.uuid4()), str(uuid.uuid4()), 1)
    _put_marker(dead_key, "dead")
    cache_module._join_graph_cache[dead_key].expires_at = time.monotonic() - 1

    _put_marker((str(uuid.uuid4()), str(uuid.uuid4()), 1), "live")
    assert dead_key not in cache_module._join_graph_cache


def test_new_deployment_evicts_this_replicas_superseded_entries() -> None:
    """Bug-8251: without a cross-replica bus, each replica self-heals on its
    first post-deploy insert instead of retaining the stale graph until TTL."""
    invalidate_join_graph_cache()
    model_id = str(uuid.uuid4())
    old_key = (model_id, str(uuid.uuid4()), 4)
    new_key = (model_id, str(uuid.uuid4()), 5)
    other_model_key = (str(uuid.uuid4()), str(uuid.uuid4()), 4)

    _put_marker(old_key, "old")
    _put_marker(other_model_key, "unrelated")
    _put_marker(new_key, "new")

    assert _get_join_graph(old_key) is None, "superseded entry must be dropped"
    assert _get_join_graph(new_key) is not None
    assert _get_join_graph(other_model_key) is not None, (
        "another model entry must not be touched"
    )


def test_late_lower_epoch_insert_does_not_evict_the_newer_entry() -> None:
    """Anti-thrash: an in-flight pre-commit request inserting its own older key
    must not evict the committed newer entry (which would make every request
    during the deploy window rebuild)."""
    invalidate_join_graph_cache()
    model_id = str(uuid.uuid4())
    old_key = (model_id, str(uuid.uuid4()), 4)
    new_key = (model_id, str(uuid.uuid4()), 5)

    _put_marker(new_key, "new")
    _put_marker(old_key, "late-old")

    assert _get_join_graph(new_key) is not None
    assert _get_join_graph(old_key) is not None


@pytest.mark.asyncio
async def test_select_star_fallback_does_not_swallow_blocked_deployment() -> None:
    """Bug-7981 round 4 follow-up: ``rewrite_for_source``'s SELECT * and
    no-columns fallbacks wrap the graph resolvers in ``except Exception``,
    which swallowed ``DeployedSnapshotUnavailableError`` into a silent raw
    passthrough (semantic table names sent to the source). A blocked
    deployment must stay loud on EVERY rewrite path, not only the
    explicit-projection path.

    Test escape: the R4 fail-closed test drove ``_load_model_graph``
    directly, not the ``rewrite_for_source`` fallback paths that wrap it in
    ``except Exception``. Guard: this test. Tier: T1.
    """
    from src.ir.logical_query import DeployedSnapshotUnavailableError
    from src.rewrite.source_sql import rewrite_for_source

    snapshot_resolver.invalidate()
    snapshot_resolver.reset_request_pins()
    invalidate_join_graph_cache()
    model, db, _vid, _tid, _cid = _deployed_model_and_db(with_tables=False)

    shape = await _real_resolve_deployed_shape(model, db)
    assert shape is not None and not shape.tables_by_id

    bound_query = types.SimpleNamespace(
        model=model,
        deployed_shape=shape,
        has_passthrough_expressions=False,
        persona_narrowed_star=False,
        resolved_measures=[],
        resolved_dimensions=[],
        logical_query=types.SimpleNamespace(
            from_tables=["modelx"],
            raw_query="SELECT * FROM modelx",
            input_dialect="postgres",
            select_star=True,
            limit=None,
            offset=None,
            select_expressions=[],
        ),
    )
    with pytest.raises(DeployedSnapshotUnavailableError):
        await rewrite_for_source(bound_query, db, target_dialect="postgres")
    assert db.live_graph_reads == 0


@pytest.mark.asyncio
async def test_no_columns_path_does_not_swallow_blocked_deployment() -> None:
    """Companion to the SELECT * guard: the ``COUNT(*)`` / no-projected-column
    path resolves its base table through its own ``except Exception`` wrapper
    in ``_build_no_columns_sql``. That wrapper must also let a blocked
    deployment through instead of returning the raw semantic SQL.

    Test escape: the SELECT * guard covers ``_substitute_table_names`` only;
    ``_build_no_columns_sql`` is a separate swallowing site reached BEFORE
    ``_load_model_graph`` on the no-column path. Guard: this test. Tier: T1.
    """
    from src.ir.logical_query import DeployedSnapshotUnavailableError
    from src.rewrite.source_sql import _build_source_sql

    snapshot_resolver.invalidate()
    snapshot_resolver.reset_request_pins()
    invalidate_join_graph_cache()
    model, db, _vid, _tid, _cid = _deployed_model_and_db(with_tables=False)

    shape = await _real_resolve_deployed_shape(model, db)
    assert shape is not None and not shape.tables_by_id

    bound_query = types.SimpleNamespace(
        model=model,
        deployed_shape=shape,
        has_passthrough_expressions=False,
        persona_narrowed_star=False,
        resolved_measures=[],
        resolved_dimensions=[],
        resolved_filters=[],
        resolved_dimensions_by_name={},
        dim_type_by_name={},
        logical_query=types.SimpleNamespace(
            from_tables=["modelx"],
            raw_query="SELECT COUNT(*) FROM modelx",
            input_dialect="postgres",
            select_star=False,
            has_distinct=False,
            limit=None,
            offset=None,
            grain=[],
            order_by=[],
            filters=[],
            select_expressions=[],
            having_raw=None,
            has_unresolvable_order=False,
        ),
    )
    with pytest.raises(DeployedSnapshotUnavailableError):
        await _build_source_sql(bound_query, db, target_dialect="postgres")
    assert db.live_graph_reads == 0
