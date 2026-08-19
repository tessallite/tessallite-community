"""Real-async DB guard for migration 0194's I/O half.

``shared/semantic/tests/test_join_orientation_backfill.py`` proves the POLICY
(which token becomes what, and that the patched snapshot agrees with the live
row). It cannot prove the part that actually ships: that the migration writes
BOTH sides in one transaction, bumps the epoch, stales the artifacts, and is a
no-op on a second run. That half was previously covered only by a disposable
live probe, which is exactly the kind of coverage CLAUDE.md requires to be
promoted rather than described.

The migration body is executed through Alembic's own ``Operations`` context
against an isolated schema built from ``TenantBase.metadata`` — the current
ORM shape, which is what a migrated tenant ends up with — rather than by
replaying 194 revisions, so the test costs one schema create.

Skipped unless ``TESSALLITE_VERSIONING_DB_URL`` (or the importer-harness URL)
points at a reachable Postgres.

Run:
    cd tessallite/services/model-service
    TESSALLITE_VERSIONING_DB_URL=postgresql+asyncpg://user:pw@localhost:5432/db \
      pytest tests/integration/test_migration_0194_join_orientation_db.py -v
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from shared.db.models import TenantBase
from shared.semantic.join_keyword import edge_cardinality, is_orientation_declared

pytestmark = [pytest.mark.integration]

_DB_URL = os.environ.get("TESSALLITE_VERSIONING_DB_URL") or os.environ.get(
    "TESSALLITE_IMPORTER_REHYDRATION_DB_URL"
)

_MIGRATION = (
    Path(__file__).resolve().parents[4]
    / "shared"
    / "db"
    / "migrations"
    / "versions"
    / "0194_join_orientation_backfill_deployed_snapshot_repair.py"
)


def _load_migration():
    """Load the revision module the way Alembic does — see
    ``tests/unit/test_migration_modules_load_like_alembic.py`` for why the
    absence of a ``sys.modules`` entry is load-bearing."""
    spec = importlib.util.spec_from_file_location("m0194_probe", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_upgrade(connection) -> None:
    """Execute ``upgrade()`` inside a real Alembic operations context."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = _load_migration()
    ctx = MigrationContext.configure(connection)
    with Operations.context(ctx):
        module.upgrade()


def _mk_engine(schema: str):
    engine = create_async_engine(_DB_URL, future=True)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_search_path(dbapi_connection, _record):  # noqa: ANN001
        cur = dbapi_connection.cursor()
        cur.execute(f'SET search_path TO "{schema}"')
        cur.close()

    return engine


@asynccontextmanager
async def _isolated_schema() -> AsyncIterator[tuple[async_sessionmaker, object]]:
    schema = f"m0194_{uuid.uuid4().hex}"
    boot = create_async_engine(_DB_URL, future=True)
    async with boot.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'SET search_path TO "{schema}"'))
        await conn.run_sync(TenantBase.metadata.create_all)
    await boot.dispose()

    engine = _mk_engine(schema)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory, engine
    finally:
        drop = create_async_engine(_DB_URL, future=True)
        async with drop.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await drop.dispose()
        await engine.dispose()


class _Fixture:
    """Ids of one seeded model whose single join carries a legacy token."""

    def __init__(self) -> None:
        self.project_id = uuid.uuid4()
        self.connection_id = uuid.uuid4()
        self.model_id = uuid.uuid4()
        self.source_id = uuid.uuid4()
        self.target_id = uuid.uuid4()
        self.left_table_id = uuid.uuid4()
        self.right_table_id = uuid.uuid4()
        self.left_column_id = uuid.uuid4()
        self.right_column_id = uuid.uuid4()
        self.join_id = uuid.uuid4()
        self.version_id = uuid.uuid4()
        self.aggregate_id = uuid.uuid4()


async def _seed(session: AsyncSession, fx: _Fixture, *, epoch: int = 3) -> None:
    """One model, one legacy-token join, one deployed version, one artifact.

    Built through the ORM so the FK chain the migration relies on
    (project -> connection -> source/target -> tables -> columns -> join) is
    real, not simulated.
    """
    from shared.db.models import (
        AggregateDefinition,
        DataSource,
        DataTarget,
        Join,
        Model,
        ModelColumn,
        ModelTable,
        ModelVersion,
        Project,
        ProjectConnection,
    )

    session.add(
        Project(
            id=fx.project_id, slug=f"p-{fx.project_id.hex[:8]}", display_name="P"
        )
    )
    await session.flush()
    session.add(
        ProjectConnection(
            id=fx.connection_id,
            project_id=fx.project_id,
            display_name="C",
            connection_type="postgresql",
            encrypted_credentials=b"x",
            config={},
        )
    )
    session.add(
        Model(
            id=fx.model_id,
            project_id=fx.project_id,
            slug=f"m-{fx.model_id.hex[:8]}",
            display_name="M",
            seed=uuid.uuid4().hex,
        )
    )
    await session.flush()
    session.add(
        DataSource(
            id=fx.source_id,
            model_id=fx.model_id,
            project_connection_id=fx.connection_id,
            source_type="postgresql",
            display_name="S",
            config={},
        )
    )
    session.add(
        DataTarget(
            id=fx.target_id,
            model_id=fx.model_id,
            project_connection_id=fx.connection_id,
            target_type="postgresql",
            display_name="T",
            config={},
        )
    )
    await session.flush()
    for tid, ttype, name in (
        (fx.left_table_id, "fact", "fact"),
        (fx.right_table_id, "dim_detail", "dim"),
    ):
        session.add(
            ModelTable(
                id=tid,
                model_id=fx.model_id,
                source_id=fx.source_id,
                table_type=ttype,
                physical_name=name,
                alias=name,
                display_name=name,
            )
        )
    await session.flush()
    for cid, tid in (
        (fx.left_column_id, fx.left_table_id),
        (fx.right_column_id, fx.right_table_id),
    ):
        session.add(
            ModelColumn(
                id=cid, model_table_id=tid, column_name="k", data_type="integer"
            )
        )
    await session.flush()
    session.add(
        Join(
            id=fx.join_id,
            model_id=fx.model_id,
            left_table_id=fx.left_table_id,
            right_table_id=fx.right_table_id,
            left_column_id=fx.left_column_id,
            right_column_id=fx.right_column_id,
            # The legacy conflation this migration exists to close, with the
            # fan-out 0191 already split out of it on the LIVE row.
            join_type="many_to_one",
            cardinality="many_to_one",
        )
    )
    session.add(
        ModelVersion(
            id=fx.version_id,
            model_id=fx.model_id,
            version_number=7,
            created_by="modeller@example.test",
            summary="modeller save",
            snapshot_json={
                "measures": [{"name": "amount"}],
                "joins": [
                    {
                        "id": str(fx.join_id),
                        "model_id": str(fx.model_id),
                        "left_table_id": str(fx.left_table_id),
                        "right_table_id": str(fx.right_table_id),
                        "left_column_id": str(fx.left_column_id),
                        "right_column_id": str(fx.right_column_id),
                        "join_type": "many_to_one",
                    }
                ],
            },
        )
    )
    await session.flush()
    # An artifact that IS compatible with the current pointer, so staling it is
    # a real state change rather than a row that was already refused.
    session.add(
        AggregateDefinition(
            id=fx.aggregate_id,
            model_id=fx.model_id,
            target_id=fx.target_id,
            physical_table_name="agg_probe",
            status="active",
            is_stale=False,
            grain=[],
            built_for_version_id=fx.version_id,
            built_for_epoch=epoch,
        )
    )
    await session.execute(
        text(
            "UPDATE models SET deployed_version_id = :v, deploy_epoch = :e, "
            "predictive_built_for_version_id = :v WHERE id = :m"
        ),
        {"v": fx.version_id, "e": epoch, "m": fx.model_id},
    )
    await session.commit()


async def _state(session: AsyncSession, fx: _Fixture) -> dict:
    row = (
        await session.execute(
            text(
                "SELECT j.join_type AS live_token, j.cardinality, "
                "       m.deploy_epoch, m.deployed_version_id, "
                "       m.predictive_built_for_version_id, "
                "       v.version_number, v.summary, "
                "       v.snapshot_json -> 'joins' -> 0 ->> 'join_type' AS snap_token, "
                "       a.is_stale, "
                "       (SELECT count(*) FROM model_versions WHERE model_id = :m) "
                "         AS version_count "
                "FROM joins j "
                "JOIN models m ON m.id = j.model_id "
                "JOIN model_versions v ON v.id = m.deployed_version_id "
                "JOIN aggregate_definitions a ON a.model_id = m.id "
                "WHERE j.id = :j"
            ),
            {"j": fx.join_id, "m": fx.model_id},
        )
    ).mappings().one()
    return dict(row)


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_0194_rewrites_live_and_deployed_snapshot_in_lockstep():
    """The whole thesis of the lane in one assertion set.

    Rewriting the live token ALONE would make ``definition_closure`` refuse
    every aggregate and pocket refresh on the model (that is why 0191 stopped
    short). The migration must move both sides together, bump the epoch so the
    now-incompatible artifact stops serving and rebuilds, and leave the
    modeller's version numbering untouched.
    """
    async with _isolated_schema() as (factory, engine):
        fx = _Fixture()
        async with factory() as s:
            await _seed(s, fx)
            before = await _state(s, fx)
        assert before["live_token"] == "many_to_one"
        assert before["snap_token"] == "many_to_one"
        assert before["is_stale"] is False

        async with engine.begin() as conn:
            await conn.run_sync(_run_upgrade)

        async with factory() as s:
            after = await _state(s, fx)

        # Both sides moved, together.
        assert after["live_token"] == "left"
        assert after["snap_token"] == "left"
        # ...and the fan-out a modeller/0191 already declared on the LIVE row
        # is preserved, not clobbered by the orientation rewrite.
        assert before["cardinality"] == "many_to_one"
        assert after["cardinality"] == "many_to_one"

        # The epoch bump is what makes the artifact incompatible and rebuildable.
        assert after["deploy_epoch"] == before["deploy_epoch"] + 1
        assert after["is_stale"] is True
        assert after["predictive_built_for_version_id"] is None

        # The modeller's numbering space is untouched: no appended version, no
        # moved pointer. A system-authored row would hijack deploy-latest and
        # silence the saved-but-not-deployed banner.
        assert after["version_count"] == before["version_count"] == 1
        assert after["deployed_version_id"] == before["deployed_version_id"]
        assert after["version_number"] == 7
        # The in-place correction is disclosed on the version it changed.
        assert "migration 0194" in (after["summary"] or "")

        async with factory() as s:
            audit = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM audit_events "
                        "WHERE action = 'model.deploy' AND target_id = :m"
                    ),
                    {"m": fx.model_id},
                )
            ).scalar_one()
        assert audit == 1, "the correction must be visible on the audit timeline"


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_0194_is_a_no_op_on_a_second_run():
    """Idempotency is what makes a repair migration safe to re-run after a
    partial deploy. A second pass must not bump the epoch again (which would
    stale freshly rebuilt artifacts for nothing) or re-annotate the summary."""
    async with _isolated_schema() as (factory, engine):
        fx = _Fixture()
        async with factory() as s:
            await _seed(s, fx)

        async with engine.begin() as conn:
            await conn.run_sync(_run_upgrade)
        async with factory() as s:
            first = await _state(s, fx)

        async with engine.begin() as conn:
            await conn.run_sync(_run_upgrade)
        async with factory() as s:
            second = await _state(s, fx)

        assert second == first


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_0194_leaves_a_disagreeing_deployed_snapshot_alone():
    """When the snapshot already disagrees with live, the model has
    un-deployed join edits and the closure already refuses its refreshes.
    Overwriting the snapshot would change what the ROUTER binds — a different
    edit, not a correction — so only the live row moves and no epoch is burnt.
    """
    async with _isolated_schema() as (factory, engine):
        fx = _Fixture()
        async with factory() as s:
            await _seed(s, fx)
            await s.execute(
                text(
                    "UPDATE model_versions SET snapshot_json = "
                    "jsonb_set(snapshot_json, '{joins,0,join_type}', '\"inner\"') "
                    "WHERE id = :v"
                ),
                {"v": fx.version_id},
            )
            await s.commit()
            before = await _state(s, fx)
        assert before["snap_token"] == "inner"

        async with engine.begin() as conn:
            await conn.run_sync(_run_upgrade)

        async with factory() as s:
            after = await _state(s, fx)
        assert after["live_token"] == "left"
        assert after["snap_token"] == "inner"
        assert after["deploy_epoch"] == before["deploy_epoch"]
        assert after["is_stale"] is False


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_0194_keeps_the_deployed_snapshot_fan_out_readable():
    """A legacy token is the deployed snapshot's ONLY carrier of fan-out.

    ``0191`` backfilled ``joins.cardinality`` on LIVE rows only, so any snapshot
    still holding a cardinality token predates the split and has no
    ``cardinality`` key. If 0194 rewrites the token without re-encoding the
    fan-out, ``edge_cardinality`` on the SNAPSHOT — what the router, BOTH
    field-compatibility loaders and the drill path classifier bind to — becomes
    None, the many-to-many guard stops refusing a fanning path, and the pair is
    offered as compatible. Proven by execution: MANY_TO_MANY_UNSUPPORTED before,
    COMPATIBLE after.
    """
    async with _isolated_schema() as (factory, engine):
        fx = _Fixture()
        async with factory() as s:
            await _seed(s, fx)
            # A pre-0191 many-to-many edge exactly as it is stored: the token on
            # BOTH sides, cardinality backfilled live by 0191, and NO
            # cardinality key in the snapshot.
            await s.execute(
                text(
                    "UPDATE joins SET join_type = 'many_to_many', "
                    "cardinality = 'many_to_many' WHERE id = :j"
                ),
                {"j": fx.join_id},
            )
            await s.execute(
                text(
                    "UPDATE model_versions SET snapshot_json = jsonb_set("
                    "snapshot_json, '{joins,0,join_type}', '\"many_to_many\"') "
                    "WHERE id = :v"
                ),
                {"v": fx.version_id},
            )
            await s.commit()

        async with engine.begin() as conn:
            await conn.run_sync(_run_upgrade)

        async with factory() as s:
            snap_join = (
                await s.execute(
                    text(
                        "SELECT snapshot_json -> 'joins' -> 0 "
                        "FROM model_versions WHERE id = :v"
                    ),
                    {"v": fx.version_id},
                )
            ).scalar_one()

        assert is_orientation_declared(snap_join["join_type"])
        assert edge_cardinality(snap_join) == "many_to_many", (
            f"the deployed snapshot join lost its fan-out: {snap_join!r}. "
            f"field_compatibility's many-to-many guard now reads None and "
            f"offers the measure/dimension pair as compatible."
        )


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_0194_agrees_with_a_snapshot_that_carries_an_explicit_null_cardinality():
    """The post-0191 shape, through the real migration.

    A join that arrived AFTER 0191 with a legacy token (a Bug-8698 import, or a
    revert to a pre-0191 version) has a NULL live ``cardinality``, and its
    deployed snapshot — necessarily serialised after the row existed, therefore
    after 0191 — carries ``"cardinality": null``, because
    ``row_to_snapshot_dict`` emits every ORM column.

    0194 writes the fan-out onto the LIVE row in that case. If it does not also
    re-encode it into the snapshot, the two disagree on ``cardinality``,
    ``definition_closure`` refuses every aggregate/pocket refresh on the model
    (the exact unattended regression 0191 stopped short to avoid), and the
    snapshot's many-to-many fan-out is gone so field_compatibility's guard
    fails open into silent double-counting.
    """
    async with _isolated_schema() as (factory, engine):
        fx = _Fixture()
        async with factory() as s:
            await _seed(s, fx)
            await s.execute(
                text(
                    "UPDATE joins SET join_type = 'many_to_many', "
                    "cardinality = NULL WHERE id = :j"
                ),
                {"j": fx.join_id},
            )
            await s.execute(
                text(
                    "UPDATE model_versions SET snapshot_json = jsonb_set("
                    "jsonb_set(snapshot_json, '{joins,0,join_type}', "
                    "'\"many_to_many\"'), '{joins,0,cardinality}', 'null') "
                    "WHERE id = :v"
                ),
                {"v": fx.version_id},
            )
            await s.commit()

        async with engine.begin() as conn:
            await conn.run_sync(_run_upgrade)

        async with factory() as s:
            live_card, snap_join = (
                await s.execute(
                    text(
                        "SELECT j.cardinality, v.snapshot_json -> 'joins' -> 0 "
                        "FROM joins j "
                        "JOIN models m ON m.id = j.model_id "
                        "JOIN model_versions v ON v.id = m.deployed_version_id "
                        "WHERE j.id = :j"
                    ),
                    {"j": fx.join_id},
                )
            ).one()

        assert live_card == "many_to_many"
        assert edge_cardinality(snap_join) == "many_to_many", (
            f"live gained the fan-out and the deployed snapshot did not: "
            f"{snap_join!r}. definition_closure now reports 'cardinality "
            f"changed' and refuses every refresh on this model."
        )
        assert snap_join["cardinality"] == live_card


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_0194_keeps_the_fan_out_when_cardinality_holds_an_unrecognised_value():
    """The live half of "resolves no fan-out" != "IS NULL".

    ``rehydrator._insert_joins`` writes a bundle's ``cardinality`` verbatim —
    the only field on that insert with no coercion — so a non-null value the
    vocabulary rejects is a producible stored value. An ``IS NULL`` guard
    leaves it in place, and once ``join_type`` is a real orientation
    ``edge_cardinality`` has nothing left to fall back to: the many-to-many
    guard fails OPEN on BOTH sides and no drift is reported, because the two
    sides still agree on the junk value.
    """
    async with _isolated_schema() as (factory, engine):
        fx = _Fixture()
        async with factory() as s:
            await _seed(s, fx)
            await s.execute(
                text(
                    "UPDATE joins SET join_type = 'many_to_many', "
                    "cardinality = 'N:N' WHERE id = :j"
                ),
                {"j": fx.join_id},
            )
            await s.execute(
                text(
                    "UPDATE model_versions SET snapshot_json = jsonb_set("
                    "jsonb_set(snapshot_json, '{joins,0,join_type}', "
                    "'\"many_to_many\"'), '{joins,0,cardinality}', '\"N:N\"') "
                    "WHERE id = :v"
                ),
                {"v": fx.version_id},
            )
            await s.commit()

        async with engine.begin() as conn:
            await conn.run_sync(_run_upgrade)

        async with factory() as s:
            live = (
                await s.execute(
                    text("SELECT join_type, cardinality FROM joins WHERE id = :j"),
                    {"j": fx.join_id},
                )
            ).one()
            snap_join = (
                await s.execute(
                    text(
                        "SELECT snapshot_json -> 'joins' -> 0 "
                        "FROM model_versions WHERE id = :v"
                    ),
                    {"v": fx.version_id},
                )
            ).scalar_one()

        assert is_orientation_declared(live.join_type)
        assert (
            edge_cardinality(
                {"join_type": live.join_type, "cardinality": live.cardinality}
            )
            == "many_to_many"
        ), (
            f"the LIVE row lost its fan-out: join_type={live.join_type!r} "
            f"cardinality={live.cardinality!r}"
        )
        assert edge_cardinality(snap_join) == "many_to_many", (
            f"the deployed snapshot lost its fan-out: {snap_join!r}; "
            f"field_compatibility's many-to-many guard now reads None."
        )


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_0194_does_not_clobber_a_cardinality_written_after_its_read():
    """The overwrite decision must be evaluated against the ROW, not a stale read.

    ``upgrade()`` scouts the joins before ``_lock_models`` runs, and that lock's
    wait is deliberately unbounded, so a modeller PATCH landing in the window is
    invisible to any map bound from the scout. Before round 4 the statement said
    ``CASE WHEN cardinality IS NULL``, which Postgres evaluated against the row
    itself; widening the predicate to ``resolves_no_fan_out`` moved the decision
    into Python and would have lost that atomicity with it. The consequence
    would be a silent wrong number: the declared ``many_to_many`` becomes the
    legacy token's ``many_to_one``, ``field_compatibility`` stops refusing the
    fanning path, and live and deployed AGREE on the wrong value so no drift is
    reported.

    ``upgrade()`` now re-reads under the lock, and the UPDATE additionally pins
    ``cardinality`` in its WHERE clause. This drives ``_apply_live_backfill``
    directly with a deliberately stale map to prove the second guard alone
    refuses, since the first is unreachable from a test that holds no lock.
    """
    m = _load_migration()
    async with _isolated_schema() as (factory, engine):
        fx = _Fixture()
        async with factory() as s:
            await _seed(s, fx)
            # What the modeller's PATCH commits AFTER the scout read ran.
            await s.execute(
                text(
                    "UPDATE joins SET join_type = 'many_to_one', "
                    "cardinality = 'many_to_many' WHERE id = :j"
                ),
                {"j": fx.join_id},
            )
            await s.commit()

        stale = {str(fx.join_id): None}  # what the pre-lock scout saw

        async with engine.begin() as conn:
            await conn.run_sync(
                lambda c: m._apply_live_backfill(
                    c,
                    m.plan_backfills([(fx.join_id, fx.model_id, "many_to_one")]),
                    stale,
                )
            )

        async with factory() as s:
            live = (
                await s.execute(
                    text("SELECT join_type, cardinality FROM joins WHERE id = :j"),
                    {"j": fx.join_id},
                )
            ).one()

    assert (
        edge_cardinality(
            {"join_type": live.join_type, "cardinality": live.cardinality}
        )
        == "many_to_many"
    ), (
        "0194 overwrote a modeller-declared many_to_many from a stale read: "
        f"join_type={live.join_type!r} cardinality={live.cardinality!r}; "
        "field_compatibility's fan-out guard now fails open into double-counting."
    )


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_0194_reads_the_cardinality_it_rewrites_after_it_holds_the_lock():
    """Two real sessions, one real lock — the runtime proof of the two-pass read.

    Every other guard on this mechanism is static (an AST check that the read is
    bound after ``_lock_models``) or drives ``_apply_live_backfill`` directly
    with a hand-made stale map. Neither executes the thing the fix exists for:
    ``upgrade()`` scouts the joins BEFORE taking the advisory locks, whose wait
    is deliberately unbounded, so a modeller PATCH landing in that window is
    invisible to the scout. Here session A holds the model's lock, the migration
    starts and blocks inside ``_lock_models`` with its scout already taken, A
    then commits ``cardinality='many_to_many'`` and releases, and the migration
    proceeds.

    If it re-reads under the lock it sees the modeller's declaration and leaves
    it alone. If it trusts the scout it overwrites it with the legacy token's
    ``many_to_one`` — ``field_compatibility`` stops refusing the fanning path,
    the measure double-counts, and live and deployed AGREE on the wrong value so
    ``definition_closure`` reports nothing.
    """
    from shared.db.model_lock import model_advisory_lock_key

    async with _isolated_schema() as (factory, engine):
        fx = _Fixture()
        async with factory() as s:
            await _seed(s, fx)
            # The pre-migration state the scout will observe.
            await s.execute(
                text("UPDATE joins SET cardinality = NULL WHERE id = :j"),
                {"j": fx.join_id},
            )
            await s.commit()

        holder = await engine.connect()
        await holder.begin()
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": model_advisory_lock_key(fx.model_id)},
        )

        migration_done = asyncio.Event()

        async def _run_migration() -> None:
            async with engine.begin() as conn:
                await conn.run_sync(_run_upgrade)
            migration_done.set()

        task = asyncio.create_task(_run_migration())
        try:
            # The migration must be BLOCKED on the advisory lock, with its
            # scout read already taken. If it finished, it never contended and
            # the test would prove nothing.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(migration_done.wait()), timeout=3.0
                )

            # The modeller's PATCH lands inside the lock-wait window.
            await holder.execute(
                text(
                    "UPDATE joins SET cardinality = 'many_to_many' WHERE id = :j"
                ),
                {"j": fx.join_id},
            )
            await holder.commit()  # releases the advisory lock
            await asyncio.wait_for(task, timeout=60.0)
        finally:
            if not task.done():
                task.cancel()
            await holder.close()

        async with factory() as s:
            live = (
                await s.execute(
                    text("SELECT join_type, cardinality FROM joins WHERE id = :j"),
                    {"j": fx.join_id},
                )
            ).one()

        assert live.join_type == "left", "the orientation backfill still ran"
        assert live.cardinality == "many_to_many", (
            "0194 overwrote a cardinality the modeller declared while it waited "
            f"for the lock: cardinality={live.cardinality!r}. It must re-read "
            f"under the lock rather than trust its pre-lock scout."
        )
