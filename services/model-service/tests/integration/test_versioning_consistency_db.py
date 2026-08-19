"""Real-async DB guards for the two model-versioning correctness defects.

These prove behaviour a mocked session cannot: snapshot isolation (Bug-7980)
and atomic complete revert of named-set definition + governance (Bug-7982).
Skipped unless ``TESSALLITE_VERSIONING_DB_URL`` (or the importer-harness URL)
points at a reachable Postgres.

Run:
    cd tessallite/services/model-service
    TESSALLITE_VERSIONING_DB_URL=postgresql+asyncpg://user:pw@localhost:5432/db \
      pytest tests/integration/test_versioning_consistency_db.py -v
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from shared.db.models import (
    KPI,
    KPIUsage,
    KPIVersion,
    Model,
    NamedSet,
    NamedSetUsage,
    NamedSetVersion,
    Project,
    TenantBase,
)
from shared.model_snapshot.rehydrator import rehydrate_into_live
from src.api._model_lock import (
    acquire_model_definition_lock,
    model_advisory_lock_key,
)

pytestmark = [pytest.mark.integration]

_DB_URL = os.environ.get("TESSALLITE_VERSIONING_DB_URL") or os.environ.get(
    "TESSALLITE_IMPORTER_REHYDRATION_DB_URL"
)


def _mk_engine(schema: str):
    engine = create_async_engine(_DB_URL, future=True)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_search_path(dbapi_connection, _record):  # noqa: ANN001
        cur = dbapi_connection.cursor()
        cur.execute(f'SET search_path TO "{schema}"')
        cur.close()

    return engine


@asynccontextmanager
async def _isolated_schema() -> AsyncIterator[tuple[async_sessionmaker, str]]:
    schema = f"versioning_consistency_{uuid.uuid4().hex}"
    boot = create_async_engine(_DB_URL, future=True)
    async with boot.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'SET search_path TO "{schema}"'))
        await conn.run_sync(TenantBase.metadata.create_all)
    await boot.dispose()

    engine = _mk_engine(schema)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory, schema
    finally:
        drop = create_async_engine(_DB_URL, future=True)
        async with drop.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await drop.dispose()
        await engine.dispose()


async def _seed_model(session: AsyncSession) -> uuid.UUID:
    project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    session.add(
        Project(id=project_id, slug=f"p-{project_id.hex[:8]}", display_name="P")
    )
    session.add(
        Model(
            id=model_id,
            project_id=project_id,
            slug=f"m-{model_id.hex[:8]}",
            display_name="M",
            seed=uuid.uuid4().hex,
        )
    )
    await session.flush()
    return model_id


# ---------------------------------------------------------------------------
# Bug-7980 — snapshot isolation blocks partial observation of a concurrent write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_repeatable_read_snapshot_does_not_observe_concurrent_commit():
    """A REPEATABLE READ transaction (the isolation Save's snapshot uses) must
    NOT observe a row a concurrent session commits after the snapshot's first
    read. This is the DB-level guarantee that prevents a Save from stitching
    together pre- and post-write state into a mixed/partial version."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as setup:
            model_id = await _seed_model(setup)
            setup.add(
                NamedSet(
                    id=uuid.uuid4(), model_id=model_id, name="A",
                    expression="{[X]}", certification_status="draft",
                )
            )
            await setup.commit()

        # Snapshot session: establish REPEATABLE READ, take the first read.
        async with factory() as snap:
            await snap.connection(
                execution_options={"isolation_level": "REPEATABLE READ"}
            )
            first = (
                await snap.execute(
                    select(NamedSet.name).where(NamedSet.model_id == model_id)
                )
            ).all()
            assert {r[0] for r in first} == {"A"}

            # Concurrent writer commits a NEW named set on another connection.
            async with factory() as writer:
                writer.add(
                    NamedSet(
                        id=uuid.uuid4(), model_id=model_id, name="B",
                        expression="{[Y]}", certification_status="draft",
                    )
                )
                await writer.commit()

            # The snapshot must still see the point-in-time it started with.
            second = (
                await snap.execute(
                    select(NamedSet.name).where(NamedSet.model_id == model_id)
                )
            ).all()
            assert {r[0] for r in second} == {"A"}, (
                "REPEATABLE READ snapshot observed a concurrent commit — "
                "Save could produce a mixed-time version"
            )


# ---------------------------------------------------------------------------
# Bug-7982 — revert restores named-set DEFINITION + preserves GOVERNANCE, atomically
# ---------------------------------------------------------------------------


def _snapshot_with_named_set(model_id: uuid.UUID, ns_id: uuid.UUID) -> dict:
    """A minimal reverted-to snapshot carrying one named set at its v-old
    definition + v-old governance."""
    return {
        "schema_version": 4,
        "model": {"id": str(model_id)},
        "named_sets": [
            {
                "id": str(ns_id),
                "model_id": str(model_id),
                "name": "TopCustomers",
                "expression": "TOPCOUNT([Customer], 10)",  # OLD definition
                "scope": 1,
                "certification_status": "certified",  # OLD governance (snapshot)
                "owner_user_id": "old-owner",
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_revert_restores_named_set_definition_but_preserves_governance():
    """Revert must restore the named-set DEFINITION (expression) from the
    snapshot while PRESERVING the live GOVERNANCE (certification/owner) — the
    exact split-brain Bug-7982 forbids."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            ns_id = uuid.uuid4()
            # Live state = NEWER definition + NEWER governance (post-snapshot edits).
            s.add(
                NamedSet(
                    id=ns_id, model_id=model_id, name="TopCustomers",
                    expression="TOPCOUNT([Customer], 25)",  # NEW definition
                    scope=1,
                    certification_status="deprecated",  # NEW live governance
                    owner_user_id="new-owner",
                )
            )
            await s.commit()

        snapshot = _snapshot_with_named_set(model_id, ns_id)
        async with factory() as s:
            await rehydrate_into_live(
                model_id, snapshot, s,
                preserve_aggregates=True, preserve_pockets=True,
                restore_governance=False,
                drop_orphan_aggregates=False, actor="test",
            )
            await s.commit()

        async with factory() as s:
            ns = (
                await s.execute(select(NamedSet).where(NamedSet.id == ns_id))
            ).scalar_one()
            # DEFINITION restored from the snapshot.
            assert ns.expression == "TOPCOUNT([Customer], 10)"
            # GOVERNANCE preserved from live (NOT rolled back to the snapshot).
            assert ns.certification_status == "deprecated"
            assert ns.owner_user_id == "new-owner"


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_revert_named_sets_is_atomic_on_failure():
    """A failure mid-revert must leave NO split state: the whole rehydrate is
    one transaction, so on rollback the live named set keeps its ORIGINAL
    definition and governance — never a half-reverted mix."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            ns_id = uuid.uuid4()
            s.add(
                NamedSet(
                    id=ns_id, model_id=model_id, name="TopCustomers",
                    expression="TOPCOUNT([Customer], 25)",  # live/original
                    scope=1, certification_status="deprecated",
                    owner_user_id="new-owner",
                )
            )
            await s.commit()

        snapshot = _snapshot_with_named_set(model_id, ns_id)
        async with factory() as s:
            await rehydrate_into_live(
                model_id, snapshot, s,
                preserve_aggregates=True, preserve_pockets=True,
                restore_governance=False,
                drop_orphan_aggregates=False, actor="test",
            )
            # Simulate a failure AFTER the named-set rewrite but BEFORE commit.
            await s.rollback()

        async with factory() as s:
            ns = (
                await s.execute(select(NamedSet).where(NamedSet.id == ns_id))
            ).scalar_one()
            # Original state fully intact — the aborted revert left no mix.
            assert ns.expression == "TOPCOUNT([Customer], 25)"
            assert ns.certification_status == "deprecated"
            assert ns.owner_user_id == "new-owner"


# ---------------------------------------------------------------------------
# Bug-7982 #1 — definition/governance writers share the revert advisory lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_model_definition_lock_is_mutually_exclusive():
    """Two sessions on the SAME per-model advisory key must exclude each other,
    while different models' keys do not contend. This is the primitive that
    stops a concurrent named-set governance write from committing between a
    revert's governance capture and its commit (which would be silently lost).
    Uses ``pg_try_advisory_xact_lock`` (non-blocking) for a deterministic proof;
    the key is a pure hash of the model UUID, so no rows are needed."""
    async with _isolated_schema() as (factory, _schema):
        model_id = uuid.uuid4()
        other_model_id = uuid.uuid4()

        async with factory() as holder:
            # Holder acquires the lock and keeps its transaction open.
            await acquire_model_definition_lock(holder, model_id)

            async with factory() as contender:
                got_same = (
                    await contender.execute(
                        text("SELECT pg_try_advisory_xact_lock(:k)"),
                        {"k": model_advisory_lock_key(model_id)},
                    )
                ).scalar()
                got_other = (
                    await contender.execute(
                        text("SELECT pg_try_advisory_xact_lock(:k)"),
                        {"k": model_advisory_lock_key(other_model_id)},
                    )
                ).scalar()
                # Same model → blocked by the holder; different model → free.
                assert got_same is False, (
                    "a writer could acquire the lock a revert already holds — "
                    "a concurrent governance change would not serialize"
                )
                assert got_other is True
                await contender.rollback()

            await holder.rollback()


# ---------------------------------------------------------------------------
# Bug-7982 #2 — revert preserves named-set version history + usage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_revert_preserves_named_set_history_and_usage():
    """A model revert must NOT irreversibly wipe a surviving named set's edit
    history (NamedSetVersion) or Excel usage telemetry (NamedSetUsage) via the
    parent-delete cascade."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            ns_id = uuid.uuid4()
            s.add(
                NamedSet(
                    id=ns_id, model_id=model_id, name="Keep",
                    expression="{[X]}", scope=1,
                    certification_status="draft",
                )
            )
            await s.flush()
            s.add(
                NamedSetVersion(
                    id=uuid.uuid4(), named_set_id=ns_id, version_number=1,
                    change_summary="Created", snapshot={"expression": "{[X]}"},
                )
            )
            s.add(
                NamedSetUsage(
                    id=uuid.uuid4(), named_set_id=ns_id, usage_type="cell",
                    workbook_id="wb1",
                )
            )
            await s.commit()

        # Reverted-to snapshot carries the same named set (same id).
        snapshot = {
            "schema_version": 4,
            "model": {"id": str(model_id)},
            "named_sets": [{
                "id": str(ns_id), "model_id": str(model_id), "name": "Keep",
                "expression": "{[X]}", "scope": 1,
                "certification_status": "draft",
            }],
        }
        async with factory() as s:
            await rehydrate_into_live(
                model_id, snapshot, s,
                preserve_aggregates=True, preserve_pockets=True,
                restore_governance=False,
                drop_orphan_aggregates=False, actor="test",
            )
            await s.commit()

        async with factory() as s:
            vcount = (
                await s.execute(
                    select(NamedSetVersion).where(
                        NamedSetVersion.named_set_id == ns_id
                    )
                )
            ).scalars().all()
            ucount = (
                await s.execute(
                    select(NamedSetUsage).where(
                        NamedSetUsage.named_set_id == ns_id
                    )
                )
            ).scalars().all()
            assert len(vcount) == 1, "revert wiped named-set version history"
            assert len(ucount) == 1, "revert wiped named-set usage telemetry"


# ---------------------------------------------------------------------------
# Bug-7982 #3 — cyclic replacement graph round-trips through rehydration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_cyclic_replacement_round_trips_through_rehydration():
    """A valid two-cycle replacement graph (A<->B) must survive rehydration on
    the import path (restore_governance=True) instead of being silently erased."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            await s.commit()

        a, b = uuid.uuid4(), uuid.uuid4()
        snapshot = {
            "schema_version": 4,
            "model": {"id": str(model_id)},
            "named_sets": [
                {"id": str(a), "model_id": str(model_id), "name": "A",
                 "expression": "{[X]}", "scope": 1,
                 "certification_status": "deprecated", "replacement_id": str(b)},
                {"id": str(b), "model_id": str(model_id), "name": "B",
                 "expression": "{[Y]}", "scope": 1,
                 "certification_status": "deprecated", "replacement_id": str(a)},
            ],
        }
        async with factory() as s:
            await rehydrate_into_live(
                model_id, snapshot, s,
                drop_orphan_aggregates=False, actor="test",
            )
            await s.commit()

        async with factory() as s:
            rows = {
                r.id: r.replacement_id
                for r in (
                    await s.execute(
                        select(NamedSet).where(NamedSet.model_id == model_id)
                    )
                ).scalars().all()
            }
            assert rows[a] == b, "A->B replacement erased on rehydration"
            assert rows[b] == a, "B->A replacement erased on rehydration"


# ---------------------------------------------------------------------------
# Bug-7982 (opus5 findings 2 & 3) — the SAME fixes generalised to KPIs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_revert_preserves_all_kpi_cascade_children():
    """A model revert must NOT wipe ANY of a surviving KPI's cascade children:
    edit history (KPIVersion), usage telemetry (KPIUsage), snapshot history
    (KPISnapshot — composite normalisation reads it), or the $KPIs serving cache
    (KPILatest — the gateway serves $KPIs exclusively from it)."""
    from shared.db.models import KPILatest, KPISnapshot

    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            kpi_id = uuid.uuid4()
            s.add(KPI(id=kpi_id, model_id=model_id, name="Revenue"))
            await s.flush()
            s.add(
                KPIVersion(
                    id=uuid.uuid4(), kpi_id=kpi_id, version_number=1,
                    change_summary="Created", snapshot={"name": "Revenue"},
                )
            )
            s.add(
                KPIUsage(
                    id=uuid.uuid4(), kpi_id=kpi_id, usage_type="cell",
                    workbook_id="wb1",
                )
            )
            s.add(KPISnapshot(id=uuid.uuid4(), kpi_id=kpi_id, value=42.0))
            s.add(
                KPILatest(
                    id=uuid.uuid4(), model_id=model_id, kpi_id=kpi_id,
                    kpi_name="Revenue", value=42.0,
                )
            )
            await s.commit()

        snapshot = {
            "schema_version": 4,
            "model": {"id": str(model_id)},
            "kpis": [{"id": str(kpi_id), "model_id": str(model_id), "name": "Revenue"}],
        }
        async with factory() as s:
            await rehydrate_into_live(
                model_id, snapshot, s,
                preserve_aggregates=True, preserve_pockets=True,
                restore_governance=False,
                drop_orphan_aggregates=False, actor="test",
            )
            await s.commit()

        async with factory() as s:
            async def _count(model_cls, col):
                return len((
                    await s.execute(select(model_cls).where(col == kpi_id))
                ).scalars().all())

            assert await _count(KPIVersion, KPIVersion.kpi_id) == 1, "wiped KPI version history"
            assert await _count(KPIUsage, KPIUsage.kpi_id) == 1, "wiped KPI usage telemetry"
            assert await _count(KPISnapshot, KPISnapshot.kpi_id) == 1, "wiped KPI snapshot history"
            assert await _count(KPILatest, KPILatest.kpi_id) == 1, "wiped $KPIs serving cache (KPILatest)"


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_cyclic_kpi_replacement_round_trips_through_rehydration():
    """A valid two-cycle KPI replacement graph (A<->B) must survive rehydration
    on the import path instead of being silently erased."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            await s.commit()

        a, b = uuid.uuid4(), uuid.uuid4()
        snapshot = {
            "schema_version": 4,
            "model": {"id": str(model_id)},
            "kpis": [
                {"id": str(a), "model_id": str(model_id), "name": "Margin",
                 "certification_status": "deprecated", "replacement_id": str(b)},
                {"id": str(b), "model_id": str(model_id), "name": "Margin v2",
                 "certification_status": "deprecated", "replacement_id": str(a)},
            ],
        }
        async with factory() as s:
            await rehydrate_into_live(
                model_id, snapshot, s,
                drop_orphan_aggregates=False, actor="test",
            )
            await s.commit()

        async with factory() as s:
            rows = {
                r.id: r.replacement_id
                for r in (
                    await s.execute(select(KPI).where(KPI.model_id == model_id))
                ).scalars().all()
            }
            assert rows[a] == b, "KPI A->B replacement erased on rehydration"
            assert rows[b] == a, "KPI B->A replacement erased on rehydration"


# ---------------------------------------------------------------------------
# Bug-7982 (Codex re-gate residual 2) — KPILatest deploy-epoch binding: a stale
# cached value is WITHHELD after a definition-changing revert (no wrong number)
# ---------------------------------------------------------------------------


def _served_kpi_values(rows):
    return {r[0].kpi_id: r[0].value for r in rows}


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_stale_kpi_latest_is_withheld_after_revert_bumps_epoch():
    """The $KPIs serve predicate must serve a kpi_latest value ONLY when it was
    evaluated for the model's CURRENT deploy epoch. A revert bumps deploy_epoch,
    so a value computed under the old definition (old epoch) must NOT be served —
    otherwise a reverted KPI serves the wrong number. Replicates the exact
    query-router serve predicate (routes.py $KPIs path)."""
    from decimal import Decimal

    from shared.db.models import KPI, KPILatest, Model as _M

    async def _serve(s, model_id):
        # EXACT predicate from query-router routes.py $KPIs serve path.
        res = await s.execute(
            select(KPILatest, KPI)
            .join(KPI, KPI.id == KPILatest.kpi_id)
            .join(_M, _M.id == KPI.model_id)
            .where(KPILatest.model_id == model_id)
            .where(KPI.is_deployed.is_(True))
            .where(_M.deployed_version_id.is_not(None))
            .where(KPILatest.evaluated_for_epoch == _M.deploy_epoch)
        )
        return _served_kpi_values(res.all())

    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            model = await s.get(_M, model_id)
            # Deployed model at epoch 5.
            deployed_v = uuid.uuid4()
            model.deployed_version_id = deployed_v
            model.deploy_epoch = 5
            kpi_id = uuid.uuid4()
            s.add(KPI(id=kpi_id, model_id=model_id, name="Revenue", is_deployed=True))
            await s.flush()
            # Cached value 100, evaluated for epoch 5 (the current deploy).
            s.add(KPILatest(
                id=uuid.uuid4(), model_id=model_id, kpi_id=kpi_id,
                kpi_name="Revenue", value=Decimal("100"),
                evaluated_for_version_id=deployed_v, evaluated_for_epoch=5,
            ))
            await s.commit()

        # Before any revert: the value is served (epoch matches).
        async with factory() as s:
            served = await _serve(s, model_id)
            assert served.get(kpi_id) == Decimal("100"), "fresh value should serve"

        # A definition-changing revert bumps deploy_epoch (Bug-7140) but the
        # kpi_latest row still carries the OLD epoch (5).
        async with factory() as s:
            model = await s.get(_M, model_id)
            model.deploy_epoch = 6
            await s.commit()

        # Now the stale value MUST be withheld (epoch 5 != current epoch 6) — the
        # reverted KPI serves NO stale number until re-evaluation repopulates it.
        async with factory() as s:
            served = await _serve(s, model_id)
            assert kpi_id not in served, (
                "stale kpi_latest (old epoch) was served after a revert — "
                "mixed-version wrong number"
            )


# ---------------------------------------------------------------------------
# Bug-7982 completion round — wrong-number stamp-timing: KPILatest must be
# stamped with the epoch that was current when EVALUATION STARTED, not the
# epoch re-read fresh at WRITE time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_mid_evaluation_revert_does_not_serve_stale_value():
    """A revert can commit and bump ``deploy_epoch`` WHILE a KPI evaluation
    (evaluate-batch / the scheduler sweep) is in flight. The value the
    evaluation computed was evaluated under the OLD definition, so the
    ``kpi_latest`` write MUST be stamped with the epoch that was current when
    evaluation STARTED — captured once, up front, and threaded through
    unchanged (see ``kpis.py`` ``evaluate_batch`` / ``sweep.py``
    ``_snapshot_tenant``) — never re-derived by re-reading ``Model.deploy_epoch``
    at write time, which could already observe the concurrent revert's new
    epoch and mislabel a stale value as current.

    This test calls the REAL production write path (``_upsert_kpi_latest_batch``)
    against a real Postgres row, simulating the exact race: capture the
    epoch/version (as evaluate_batch does at the top of the request), THEN let
    a revert commit and bump the epoch (simulating the revert landing while the
    evaluation HTTP call / measure resolution is still running), THEN perform
    the write using the epoch captured BEFORE the revert. The $KPIs serve
    predicate must withhold the resulting row.

    Mutation check: reverting the stamp-timing fix (making
    ``_upsert_kpi_latest_batch`` re-read ``Model.deploy_epoch`` fresh from the DB
    instead of using the caller-supplied ``eval_epoch``) makes this test FAIL —
    the late fresh read observes the post-revert epoch (6), which then MATCHES
    the model's current epoch, so the stale (epoch-5) value would be wrongly
    SERVED instead of withheld.
    """
    import types as _types
    from decimal import Decimal

    from shared.db.models import KPI, KPILatest, Model as _M
    from shared.schemas.pydantic_models import KPIEvaluateResponse
    from src.api.kpi_latest import _upsert_kpi_latest_batch

    async def _serve(s, model_id):
        # Same predicate as the query-router $KPIs serve path / the residual-2
        # test above.
        res = await s.execute(
            select(KPILatest, KPI)
            .join(KPI, KPI.id == KPILatest.kpi_id)
            .join(_M, _M.id == KPI.model_id)
            .where(KPILatest.model_id == model_id)
            .where(KPI.is_deployed.is_(True))
            .where(_M.deployed_version_id.is_not(None))
            .where(KPILatest.evaluated_for_epoch == _M.deploy_epoch)
        )
        return _served_kpi_values(res.all())

    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            model = await s.get(_M, model_id)
            deployed_v = uuid.uuid4()
            model.deployed_version_id = deployed_v
            model.deploy_epoch = 5
            kpi_id = uuid.uuid4()
            s.add(KPI(id=kpi_id, model_id=model_id, name="Revenue", is_deployed=True))
            await s.commit()

        # --- "evaluate_batch" starts: capture the epoch/version binding up
        # front, exactly as the real handler does, BEFORE anything else runs.
        async with factory() as s:
            model = await s.get(_M, model_id)
            eval_version_id_at_start = model.deployed_version_id
            eval_epoch_at_start = model.deploy_epoch
        assert eval_epoch_at_start == 5

        # --- Meanwhile, a revert commits and bumps the epoch WHILE the
        # evaluation above is (conceptually) still running its measure
        # resolution / query-router calls.
        async with factory() as s:
            model = await s.get(_M, model_id)
            model.deploy_epoch = 6
            await s.commit()

        # --- The evaluation finishes and writes its result, using the epoch it
        # captured BEFORE the revert — the real _upsert_kpi_latest_batch call
        # evaluate_batch makes at the end of the request.
        async with factory() as s:
            kpi_objs = {kpi_id: _types.SimpleNamespace(name="Revenue")}
            result_map = {
                kpi_id: KPIEvaluateResponse(
                    kpi_id=kpi_id, value=100.0, target=None, status=0,
                    status_label=None, trend_pct=None, formatted_value="100",
                )
            }
            await _upsert_kpi_latest_batch(
                s, model_id, kpi_objs, result_map,
                eval_version_id=eval_version_id_at_start,
                eval_epoch=eval_epoch_at_start,
            )

        # Sanity: the row landed, stamped with the STALE (pre-revert) epoch —
        # proves the withhold below is the epoch gate, not a missing row.
        async with factory() as s:
            row = (
                await s.execute(select(KPILatest).where(KPILatest.kpi_id == kpi_id))
            ).scalar_one()
            assert row.evaluated_for_epoch == 5
            assert float(row.value) == 100.0

        # The $KPIs serve predicate must WITHHOLD this value: it was computed
        # under epoch 5 (before the revert), but the model is now at epoch 6.
        async with factory() as s:
            served = await _serve(s, model_id)
            assert kpi_id not in served, (
                "a value computed BEFORE a mid-flight revert was served after "
                "the revert bumped deploy_epoch — wrong-number stamp-timing bug"
            )


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_slow_stale_evaluation_cannot_clobber_a_fresher_published_value():
    """opus5 completion-round finding 2.5 (epoch monotonicity): capturing the
    epoch at evaluation START (residual to the stamp-timing fix above) opens a
    NEW availability race against the post-deploy re-eval trigger
    (``kpi_reeval_trigger.py``): a SLOW evaluation started under the OLD epoch
    can still be in flight when a deploy bumps the epoch and the trigger's
    FAST re-evaluation already published a fresh, servable row under the NEW
    epoch. If the slow write then applies unconditionally, it overwrites the
    fresh row with a stale epoch — ``$KPIs`` regresses from serving a correct
    number to serving NOTHING until the next hourly sweep.

    ``_upsert_kpi_latest_batch`` must refuse to regress a published row to an
    OLDER epoch: a write whose ``eval_epoch`` is older than what is already
    published is suppressed (the newer row survives untouched); a write whose
    ``eval_epoch`` is the same or newer still applies normally.

    Mutation check: dropping the ``where=`` monotonicity clause on the
    ``on_conflict_do_update`` makes the "stale write suppressed" assertion
    below fail — the stale write would unconditionally overwrite the fresh
    row instead.
    """
    import types

    from shared.db.models import KPI, KPILatest, Model as _M
    from shared.schemas.pydantic_models import KPIEvaluateResponse
    from src.api.kpi_latest import _upsert_kpi_latest_batch

    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            model = await s.get(_M, model_id)
            deployed_v = uuid.uuid4()
            model.deployed_version_id = deployed_v
            model.deploy_epoch = 6
            kpi_id = uuid.uuid4()
            s.add(KPI(id=kpi_id, model_id=model_id, name="Revenue", is_deployed=True))
            await s.commit()

        # The post-deploy trigger's FAST re-evaluation already published a
        # fresh, servable value under the CURRENT epoch (6).
        async with factory() as s:
            kpi_objs = {kpi_id: types.SimpleNamespace(name="Revenue")}
            fresh = {
                kpi_id: KPIEvaluateResponse(
                    kpi_id=kpi_id, value=200.0, target=None, status=0,
                    status_label=None, trend_pct=None, formatted_value="200",
                )
            }
            await _upsert_kpi_latest_batch(
                s, model_id, kpi_objs, fresh,
                eval_version_id=deployed_v, eval_epoch=6,
            )

        # A SLOW evaluation that started back under epoch 5 (before the
        # deploy) now finishes and tries to write its stale result.
        async with factory() as s:
            kpi_objs = {kpi_id: types.SimpleNamespace(name="Revenue")}
            stale = {
                kpi_id: KPIEvaluateResponse(
                    kpi_id=kpi_id, value=100.0, target=None, status=0,
                    status_label=None, trend_pct=None, formatted_value="100",
                )
            }
            await _upsert_kpi_latest_batch(
                s, model_id, kpi_objs, stale,
                eval_version_id=uuid.uuid4(), eval_epoch=5,
            )

        # The fresh (epoch 6) row must SURVIVE untouched — the stale (epoch 5)
        # write must have been suppressed, not applied.
        async with factory() as s:
            row = (
                await s.execute(select(KPILatest).where(KPILatest.kpi_id == kpi_id))
            ).scalar_one()
            assert row.evaluated_for_epoch == 6, (
                "a stale (older-epoch) evaluation clobbered a fresher "
                "published row — the epoch monotonicity guard did not fire"
            )
            assert float(row.value) == 200.0, (
                "the fresh published value was overwritten by a stale write"
            )

        # A write at the SAME-or-NEWER epoch must still apply normally.
        async with factory() as s:
            kpi_objs = {kpi_id: types.SimpleNamespace(name="Revenue")}
            newer = {
                kpi_id: KPIEvaluateResponse(
                    kpi_id=kpi_id, value=300.0, target=None, status=0,
                    status_label=None, trend_pct=None, formatted_value="300",
                )
            }
            await _upsert_kpi_latest_batch(
                s, model_id, kpi_objs, newer,
                eval_version_id=uuid.uuid4(), eval_epoch=6,
            )

        async with factory() as s:
            row = (
                await s.execute(select(KPILatest).where(KPILatest.kpi_id == kpi_id))
            ).scalar_one()
            assert row.evaluated_for_epoch == 6
            assert float(row.value) == 300.0, (
                "a same-epoch write must still be applied normally, not "
                "over-suppressed by the monotonicity guard"
            )


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_revert_clears_a_field_added_after_the_snapshot():
    """opus5 R3 finding 1: an in-place revert must restore a definition column to
    NULL when the reverted-to snapshot has it NULL — otherwise a field ADDED in a
    later version survives the revert (a stale/wrong governed value on $KPIs)."""
    from decimal import Decimal

    from shared.db.models import KPI

    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            kpi_id = uuid.uuid4()
            # Live (v2) state: a target was ADDED after the snapshot.
            s.add(KPI(
                id=kpi_id, model_id=model_id, name="Profit",
                description="v2 note", target_type="static",
                target_value=Decimal("1000"),
            ))
            await s.commit()

        # Reverted-to (v1) snapshot: full row but target_type / target_value /
        # description are absent (were NULL at v1). A complete serialiser snapshot
        # carries all NOT-NULL columns; here we give the NOT-NULL ones and omit
        # the nullable v2 additions.
        snapshot = {
            "schema_version": 4,
            "model": {"id": str(model_id)},
            "kpis": [{
                "id": str(kpi_id), "model_id": str(model_id), "name": "Profit",
                # nullable v2 fields intentionally absent -> must revert to NULL
            }],
        }
        async with factory() as s:
            await rehydrate_into_live(
                model_id, snapshot, s,
                preserve_aggregates=True, preserve_pockets=True,
                restore_governance=False,
                drop_orphan_aggregates=False, actor="test",
            )
            await s.commit()

        async with factory() as s:
            kpi = (await s.execute(select(KPI).where(KPI.id == kpi_id))).scalar_one()
            assert kpi.target_type is None, "revert did not clear target_type"
            assert kpi.target_value is None, "revert did not clear target_value"
            assert kpi.description is None, "revert did not clear description"
            # NOT-NULL defaults are untouched (never NULLed).
            assert kpi.direction is not None


# ---------------------------------------------------------------------------
# Bug-7982 completion round item 4 — bounded lock-wait timeout (opus5 finding
# 4.7: this live-DB test belongs in tests/integration/ per the CLAUDE.md test
# taxonomy, not bundled under a blanket ``pytest.mark.unit`` in a mock-based
# unit test file; moved here from test_model_lock_timeout.py, which keeps only
# its pure-mock unit tests).
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lock_test_engine():
    """A dedicated engine for the lock-timeout race test.

    NullPool: two DISTINCT physical connections for the holder/waiter
    sessions, so the elapsed-time assertion below cannot be confused by a
    pool-checkout wait (this test's own history: a pydantic-settings
    env-caching quirk once produced an elapsed time close to the DEFAULT
    30s pool_timeout, which looked identical to "the fix isn't working"
    until traced to the settings cache, not pooling — NullPool removes that
    ambiguity entirely regardless of cause).
    """
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(_DB_URL, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_second_writer_fails_fast_instead_of_hanging(monkeypatch):
    """A holder keeps the lock open (simulating calendar.py's DDL-in-flight
    window); a second writer configured with a short lock_timeout must fail
    fast with HTTP 503 rather than hang until the holder's transaction ends.

    Mutation check: removing the lock_timeout (reverting to the bare
    ``pg_advisory_xact_lock`` call with no bound) makes the second writer BLOCK
    until the holder's transaction ends instead of failing fast — this test
    would then hang / time out instead of completing quickly.

    Patches ``get_settings`` directly (rather than the
    ``MODEL_DEFINITION_LOCK_TIMEOUT_SECONDS`` env var) — pydantic-settings can
    cache its environment read across ``Settings()`` instantiations within a
    process, so an env-var monkeypatch set after other tests have already
    triggered a ``Settings()`` build is not reliably observed; patching the
    accessor the implementation actually calls is deterministic.

    Bug-7982 R7: the target is ``shared.db.model_lock`` — R6 moved the
    implementation there and left ``src.api._model_lock`` a pure re-export shim
    with no ``get_settings`` attribute, so this test had been raising
    AttributeError at setup ever since and never reached the contention it
    claims to prove. It was not caught because that round ran only its own new
    test file, not the whole integration directory.
    """
    import time
    import types

    from fastapi import HTTPException
    from shared.db import model_lock as _model_lock_impl

    monkeypatch.setattr(
        _model_lock_impl,
        "get_settings",
        lambda: types.SimpleNamespace(MODEL_DEFINITION_LOCK_TIMEOUT_SECONDS=1),
    )

    model_id = uuid.uuid4()

    async with _lock_test_engine() as engine:
        factory = async_sessionmaker(engine, expire_on_commit=False)

        # Holder: acquires the lock and keeps the transaction open (never
        # commits/rolls back during the test), simulating a slow DDL call.
        holder = factory()
        await acquire_model_definition_lock(holder, model_id)

        try:
            # Second writer: same model, short timeout — must fail fast.
            waiter = factory()
            start = time.monotonic()
            with pytest.raises(HTTPException) as exc_info:
                await acquire_model_definition_lock(waiter, model_id)
            elapsed = time.monotonic() - start

            assert exc_info.value.status_code == 503
            # Failed fast (well under a lazy/hanging wait) — bounded by the
            # 1s configured timeout plus reasonable scheduling slack.
            assert elapsed < 10.0
            await waiter.rollback()
            await waiter.close()
        finally:
            await holder.rollback()
            await holder.close()
