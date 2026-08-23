"""Real-Postgres proof that ``delete_model_cascade`` serialises on the per-model
advisory lock and agrees with migration ``0194``'s lock-then-write order.

The defect these pin (both halves live on main before this lane):

  1. ``shared/model_snapshot/cascade_delete.py::delete_model_cascade`` took NO
     per-model advisory lock, so a model DELETE, a project DELETE, and a
     project import in ``replace`` mode could all run right through a concurrent
     Save / revert / tenant migration that held the lock on the same model.
  2. It reached ``joins`` (and every other snapshot-owned table) BEFORE the
     ``models`` row and before any lock. ``0194._lock_models`` documents the
     opposite order in as many words — advisory lock for every affected model
     first, ``joins`` rows second — because the API's Save/revert path does the
     same, and "backfilling the live rows before taking the advisory lock would
     invert that order, and an ABBA deadlock aborts the whole transaction with
     an opaque 40P01".

Why a real database. Every assertion here is about what two concurrent
PostgreSQL transactions do to each other: advisory-lock waits, FK ``KEY SHARE``
locks on ``models``, row locks on ``joins``, and the deadlock detector. A mocked
session can express none of them, and the mocked unit guard in
``optimizer/tests/test_cascade_delete_physical_tables.py`` deliberately only
pins statement ORDER — not that the order actually serialises anything.

Skipped unless ``TESSALLITE_VERSIONING_DB_URL`` (or the importer-harness URL)
points at a reachable Postgres.

Run:
    cd tessallite/services/model-service
    TESSALLITE_VERSIONING_DB_URL=postgresql+asyncpg://user:pw@localhost:5432/db \
      pytest tests/integration/test_cascade_delete_model_lock_db.py -v
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from shared.db.model_lock import acquire_model_definition_lock
from shared.db.model_write_lock_guard import guard_mode
from shared.model_snapshot.cascade_delete import (
    delete_model_cascade,
    delete_project_cascade,
)

from tests.integration.test_versioning_consistency_db import (  # noqa: E402
    _DB_URL,
    _isolated_schema,
)

pytestmark = [pytest.mark.integration]

#: PostgreSQL SQLSTATE for a detected deadlock.
_DEADLOCK = "40P01"

#: How long a blocked cascade is observed before concluding it is genuinely
#: waiting. An unblocked cascade over a handful of rows finishes in single-digit
#: milliseconds, and ``MODEL_DEFINITION_LOCK_TIMEOUT_SECONDS`` defaults to 30,
#: so 3s sits far from both edges.
_BLOCKED_OBSERVATION_SECONDS = 3.0


async def _seed_project(session) -> tuple[uuid.UUID, uuid.UUID]:
    """A project and one connection for it. Returns ``(project_id, connection_id)``."""
    from shared.db.models import Project, ProjectConnection

    project_id, connection_id = uuid.uuid4(), uuid.uuid4()
    session.add(
        Project(id=project_id, slug=f"p-{project_id.hex[:8]}", display_name="P")
    )
    await session.flush()
    session.add(
        ProjectConnection(
            id=connection_id,
            project_id=project_id,
            display_name="C",
            connection_type="postgresql",
            encrypted_credentials=b"x",
            config={},
        )
    )
    await session.flush()
    return project_id, connection_id


async def _seed_model_with_children(
    session, project_id: uuid.UUID, connection_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """One model with the real FK chain the cascade walks, through the ORM.

    Returns ``(model_id, join_id)``. ``joins`` is the table migration 0194
    orders against, so a join row is what makes the ordering assertions mean
    anything; ``model_tables``/``data_sources`` exist because ``joins`` cannot.
    """
    from shared.db.models import (
        DataSource,
        Join,
        Measure,
        Model,
        ModelColumn,
        ModelTable,
    )

    model_id, source_id, join_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    left_id, right_id = uuid.uuid4(), uuid.uuid4()
    left_col, right_col = uuid.uuid4(), uuid.uuid4()
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
    session.add(
        DataSource(
            id=source_id,
            model_id=model_id,
            project_connection_id=connection_id,
            source_type="postgresql",
            display_name="S",
            config={},
        )
    )
    await session.flush()
    for tid, ttype, name in ((left_id, "fact", "fact"), (right_id, "dim_detail", "dim")):
        session.add(
            ModelTable(
                id=tid,
                model_id=model_id,
                source_id=source_id,
                table_type=ttype,
                physical_name=name,
                alias=f"{name}_{tid.hex[:6]}",
                display_name=name,
            )
        )
    await session.flush()
    for cid, tid in ((left_col, left_id), (right_col, right_id)):
        session.add(
            ModelColumn(
                id=cid, model_table_id=tid, column_name="k", data_type="integer"
            )
        )
    await session.flush()
    session.add(
        Join(
            id=join_id,
            model_id=model_id,
            left_table_id=left_id,
            right_table_id=right_id,
            left_column_id=left_col,
            right_column_id=right_col,
            join_type="inner",
        )
    )
    session.add(Measure(id=uuid.uuid4(), model_id=model_id, name="m1"))
    await session.flush()
    return model_id, join_id


async def _seed_model_with_named_query(
    session, project_id: uuid.UUID, connection_id: uuid.UUID, suffix: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed the real NQ -> artifact/run/policy -> target FK chain.

    The resolver is intentionally exercised against real tenant metadata; the
    target-side PostgreSQL table does not need to exist because the cascade
    only persists the detached cleanup outbox.
    """
    from shared.db.models import (
        DataTarget,
        Model,
        NamedQuery,
        NamedQueryArtifact,
        NamedQueryRefreshPolicy,
        NamedQueryRefreshRun,
    )

    model_id = uuid.uuid4()
    target_id, named_query_id = uuid.uuid4(), uuid.uuid4()
    run_id, artifact_id, policy_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    session.add(
        Model(
            id=model_id,
            project_id=project_id,
            slug=f"nq-model-{suffix}-{model_id.hex[:8]}",
            display_name=f"NQ Model {suffix}",
            seed=uuid.uuid4().hex,
        )
    )
    await session.flush()
    session.add(
        DataTarget(
            id=target_id,
            model_id=model_id,
            project_connection_id=connection_id,
            target_type="postgresql",
            display_name=f"NQ Target {suffix}",
            config={"schema": "analytics"},
        )
    )
    await session.flush()
    session.add(
        NamedQuery(
            id=named_query_id,
            model_id=model_id,
            name=f"orders_{suffix}",
            display_name=f"Orders {suffix}",
            definition_sql="SELECT * FROM orders",
            output_columns=[{"name": "id", "type": "number"}],
            shape="projection",
            certification_status="draft",
        )
    )
    await session.flush()
    session.add(
        NamedQueryRefreshRun(
            id=run_id,
            named_query_id=named_query_id,
            refresh_mode="full",
            status="completed",
            triggered_by="integration",
        )
    )
    session.add(
        NamedQueryRefreshPolicy(
            id=policy_id,
            named_query_id=named_query_id,
            cron_expression="0 * * * *",
            is_enabled=True,
        )
    )
    await session.flush()
    session.add(
        NamedQueryArtifact(
            id=artifact_id,
            named_query_id=named_query_id,
            target_id=target_id,
            physical_table_name=f"nq_{suffix}_result",
            target_schema="analytics",
            status="fresh",
            active_refresh_run_id=run_id,
            row_count=1,
        )
    )
    await session.flush()
    return model_id, target_id, artifact_id, named_query_id, run_id


async def _count(session, table: str, model_id: uuid.UUID) -> int:
    col = "id" if table == "models" else "model_id"
    return (
        await session.execute(
            text(f"SELECT count(*) FROM {table} WHERE {col} = :mid"),
            {"mid": model_id},
        )
    ).scalar_one()


async def _sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None)


# ---------------------------------------------------------------------------
# 1. Serialisation — the cascade must not run through a concurrent lock holder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_cascade_delete_waits_for_a_concurrent_model_lock_holder():
    """A definition writer (Save / revert / migration 0194) holds the per-model
    advisory lock. ``delete_model_cascade`` on the SAME model must BLOCK until
    that transaction ends, and then delete everything with no orphan left.

    Before the fix the cascade took no lock at all and ran straight to
    completion — deleting the holder's model out from under it. That is the
    ``assert not done`` below, and it is the whole finding.
    """
    async with _isolated_schema() as (factory, _schema):
        async with factory() as seed:
            project_id, connection_id = await _seed_project(seed)
            model_id, _join_id = await _seed_model_with_children(
                seed, project_id, connection_id
            )
            await seed.commit()

        holder = factory()
        deleter = factory()
        try:
            # The holder opens a transaction and takes the lock, exactly as
            # revert_to_version / Save / 0194._lock_models do.
            await acquire_model_definition_lock(holder, model_id)

            task = asyncio.create_task(delete_model_cascade(deleter, model_id))
            done, _pending = await asyncio.wait(
                {task}, timeout=_BLOCKED_OBSERVATION_SECONDS
            )
            assert not done, (
                "delete_model_cascade completed while another transaction held "
                "this model's definition lock — it acquires no lock, so a model "
                "DELETE / project DELETE / import-replace can run straight "
                "through a concurrent Save, revert or tenant migration."
            )

            # Release the lock; the cascade must then complete cleanly.
            await holder.rollback()
            errors = await asyncio.wait_for(task, timeout=30)
            assert errors == [], errors
            await deleter.commit()
        finally:
            await holder.close()
            await deleter.close()

        # Observable end state: the model and every child row are gone. No
        # orphan, no partially-deleted model.
        async with factory() as check:
            for table in ("joins", "measures", "model_tables", "models"):
                assert await _count(check, table, model_id) == 0, (
                    f"{table} still holds rows for the deleted model — the "
                    "cascade left an orphan"
                )


# ---------------------------------------------------------------------------
# 2. Lock ORDER — no ABBA deadlock against a lock-holding definition writer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_cascade_delete_does_not_deadlock_against_a_lock_holding_writer():
    """The exact ABBA cycle ``0194._lock_models`` warns about, constructed.

    Holder  : advisory lock(M) -> INSERT INTO joins (takes FK ``KEY SHARE`` on
              ``models``[M]) -> UPDATE the pre-existing join row J.
    Cascade : (pre-fix) DELETE FROM joins WHERE model_id = M  (row lock on J)
              -> ... -> DELETE FROM models WHERE id = M (needs to remove
              ``models``[M], blocked by the holder's ``KEY SHARE``).

    Pre-fix that is a cycle: the holder waits on the cascade's row lock on J
    while the cascade waits on the holder's ``KEY SHARE`` on ``models``[M].
    PostgreSQL aborts one side with ``40P01`` MID-CASCADE. Post-fix the cascade
    blocks on the advisory lock as its FIRST statement, never touches ``joins``
    while the holder is live, and the two serialise.
    """
    async with _isolated_schema() as (factory, _schema):
        async with factory() as seed:
            project_id, connection_id = await _seed_project(seed)
            model_id, join_id = await _seed_model_with_children(
                seed, project_id, connection_id
            )
            await seed.commit()

        holder = factory()
        deleter = factory()
        holder_error: list[BaseException] = []
        try:
            await acquire_model_definition_lock(holder, model_id)
            # Take the FK KEY SHARE on models[M] that the cascade's final
            # DELETE FROM models must wait behind.
            edge = (
                await holder.execute(
                    text(
                        "SELECT t.id AS table_id, c.id AS column_id "
                        "FROM model_tables t JOIN model_columns c "
                        "  ON c.model_table_id = t.id "
                        "WHERE t.model_id = :mid ORDER BY t.table_type"
                    ),
                    {"mid": model_id},
                )
            ).all()
            await holder.execute(
                text(
                    "INSERT INTO joins (id, model_id, left_table_id, "
                    "right_table_id, left_column_id, right_column_id, join_type) "
                    "VALUES (:id, :mid, :lt, :rt, :lc, :rc, 'left')"
                ),
                {
                    "id": uuid.uuid4(), "mid": model_id,
                    "lt": edge[0].table_id, "rt": edge[1].table_id,
                    "lc": edge[0].column_id, "rc": edge[1].column_id,
                },
            )

            cascade = asyncio.create_task(delete_model_cascade(deleter, model_id))

            async def _holder_updates_the_join():
                # Give the cascade time to reach (pre-fix) its joins DELETE, or
                # (post-fix) to park on the advisory lock. Either way this UPDATE
                # then contends with whatever the cascade is holding.
                await asyncio.sleep(1.0)
                try:
                    await holder.execute(
                        text(
                            "UPDATE joins SET join_type = 'full' WHERE id = :jid"
                        ),
                        {"jid": join_id},
                    )
                except BaseException as exc:  # noqa: BLE001 — recorded, re-raised below
                    holder_error.append(exc)

            update = asyncio.create_task(_holder_updates_the_join())
            await asyncio.wait_for(update, timeout=30)

            # PostgreSQL breaks a deadlock by aborting ONE of the two sides, and
            # which one is not ours to choose. Check both before anything else,
            # so a 40P01 is reported as the deadlock it is rather than as a
            # confusing downstream assertion.
            deadlocked: list[str] = []
            if holder_error:
                if await _sqlstate(holder_error[0]) == _DEADLOCK:
                    deadlocked.append("the concurrent definition writer")
                else:
                    raise holder_error[0]
            if cascade.done() and cascade.exception() is not None:
                if await _sqlstate(cascade.exception()) == _DEADLOCK:
                    deadlocked.append("the cascade, mid-delete")
                else:
                    raise cascade.exception()
            if cascade.done() and cascade.exception() is None:
                # ``delete_model_cascade`` catches each step's exception and
                # returns it as a string, so a deadlock that aborts the CASCADE
                # side arrives as a failed step, not as a raised error — and it
                # poisons the transaction, so the whole delete fails.
                failed = [e for e in cascade.result() if "deadlock" in e.lower()]
                if failed:
                    deadlocked.append(f"the cascade, mid-delete ({failed[0][:120]})")
            assert not deadlocked, (
                f"deadlock (40P01) aborted {' and '.join(deadlocked)}. The "
                "cascade reaches joins/models rows without first taking the "
                "model's advisory lock, inverting the lock-then-write order "
                "migration 0194._lock_models sets and documents."
            )

            assert not cascade.done(), (
                "the cascade completed while the holder still owned the model "
                "lock — it took no lock"
            )
            await holder.commit()

            try:
                errors = await asyncio.wait_for(cascade, timeout=30)
            except DBAPIError as exc:
                assert await _sqlstate(exc) != _DEADLOCK, (
                    "the cascade was aborted mid-delete with a deadlock (40P01) "
                    "against a lock-holding definition writer — this is exactly "
                    "the ABBA cycle 0194._lock_models documents."
                )
                raise
            assert errors == [], errors
            await deleter.commit()
        finally:
            await holder.close()
            await deleter.close()

        async with factory() as check:
            for table in ("joins", "measures", "model_tables", "models"):
                assert await _count(check, table, model_id) == 0, (
                    f"{table} still holds rows for the deleted model"
                )


# ---------------------------------------------------------------------------
# 3. The RUNTIME write guard agrees — no unlocked write to a guarded table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_cascade_delete_passes_the_strict_runtime_write_lock_guard():
    """``shared/db/model_write_lock_guard.py`` in ``strict`` mode raises on any
    write to a snapshot-owned table without a recorded model lock on that
    connection. The cascade writes ``joins``, ``measures``, ``dimensions`` and
    more, so before the fix every model delete tripped it — and in the default
    ``warn`` mode it has been logging that violation in production all along.
    """
    async with _isolated_schema() as (factory, _schema):
        async with factory() as seed:
            project_id, connection_id = await _seed_project(seed)
            model_id, _join_id = await _seed_model_with_children(
                seed, project_id, connection_id
            )
            await seed.commit()

        async with factory() as session:
            with guard_mode("strict"):
                errors = await delete_model_cascade(session, model_id)
            assert errors == [], errors
            await session.commit()

        async with factory() as check:
            assert await _count(check, "models", model_id) == 0


# ---------------------------------------------------------------------------
# 4. Project cascade — every model locked, in the order 0194 uses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_project_cascade_waits_for_a_lock_held_on_any_of_its_models():
    """A project DELETE fans out over the same primitive. Holding the lock on ONE
    of the project's models must block the whole project cascade — the
    ``{project_id}``-shaped route the static lock-coverage guard cannot see.
    """
    async with _isolated_schema() as (factory, _schema):
        async with factory() as seed:
            project_id, connection_id = await _seed_project(seed)
            m1, _ = await _seed_model_with_children(seed, project_id, connection_id)
            m2, _ = await _seed_model_with_children(seed, project_id, connection_id)
            await seed.commit()

        # Hold the lock on whichever model the cascade reaches LAST, so the test
        # cannot pass by accident on the first model it happens to visit.
        held = sorted((m1, m2), key=str)[-1]
        holder = factory()
        deleter = factory()
        try:
            await acquire_model_definition_lock(holder, held)
            task = asyncio.create_task(delete_project_cascade(deleter, project_id))
            done, _pending = await asyncio.wait(
                {task}, timeout=_BLOCKED_OBSERVATION_SECONDS
            )
            assert not done, (
                "delete_project_cascade completed while one of its models' "
                "definition locks was held by another transaction"
            )
            await holder.rollback()
            errors = await asyncio.wait_for(task, timeout=30)
            assert errors == [], errors
            await deleter.commit()
        finally:
            await holder.close()
            await deleter.close()

        async with factory() as check:
            remaining = (
                await check.execute(
                    text("SELECT count(*) FROM models WHERE project_id = :pid"),
                    {"pid": project_id},
                )
            ).scalar_one()
            assert remaining == 0
            assert await _count(check, "joins", m1) == 0


# ---------------------------------------------------------------------------
# Bug-9162 — real PostgreSQL FK proof for the Named Query family
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_bug9162_model_delete_removes_nq_family_and_persists_cleanup_identity():
    """A real model delete must clear every NQ FK before its target row."""
    from shared.db.models import PhysicalCleanupTask

    async with _isolated_schema() as (factory, _schema):
        async with factory() as seed:
            project_id, connection_id = await _seed_project(seed)
            (
                model_id,
                _target_id,
                artifact_id,
                named_query_id,
                run_id,
            ) = await _seed_model_with_named_query(
                seed, project_id, connection_id, "model"
            )
            await seed.commit()

        async with factory() as deleter:
            errors = await delete_model_cascade(
                deleter, model_id, cleanup_reason="bug9162_model_delete"
            )
            assert errors == [], errors
            await deleter.commit()

        async with factory() as check:
            assert await _count(check, "models", model_id) == 0
            assert await _count(check, "data_targets", model_id) == 0
            metadata_counts = {
                "named_queries": (
                    "SELECT count(*) FROM named_queries WHERE id = :nqid",
                    {"nqid": named_query_id},
                ),
                "named_query_refresh_policies": (
                    "SELECT count(*) FROM named_query_refresh_policies "
                    "WHERE named_query_id = :nqid",
                    {"nqid": named_query_id},
                ),
                "named_query_refresh_runs": (
                    "SELECT count(*) FROM named_query_refresh_runs "
                    "WHERE named_query_id = :nqid OR id = :rid",
                    {"nqid": named_query_id, "rid": run_id},
                ),
            }
            for table, (statement, params) in metadata_counts.items():
                result = await check.execute(text(statement), params)
                assert result.scalar_one() == 0, table
            artifact_count = await check.execute(
                text(
                    "SELECT count(*) FROM named_query_artifacts "
                    "WHERE id = :aid"
                ),
                {"aid": artifact_id},
            )
            assert artifact_count.scalar_one() == 0
            task = (
                await check.execute(
                    select(PhysicalCleanupTask).where(
                        PhysicalCleanupTask.artifact_id == artifact_id
                    )
                )
            ).scalar_one()
            assert task.artifact_kind == "named_query"
            assert task.model_id == model_id
            assert task.project_id == project_id
            assert task.connection_id == connection_id
            assert task.target_schema == "analytics"
            assert task.qualified_table_name == "analytics.nq_model_result"
            assert task.status == "pending"
            assert task.artifact_id == artifact_id


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_bug9162_project_delete_removes_nq_families_and_keeps_outbox_rows():
    """The project-shaped cascade must apply the same real FK ordering."""
    from shared.db.models import PhysicalCleanupTask

    async with _isolated_schema() as (factory, _schema):
        async with factory() as seed:
            project_id, connection_id = await _seed_project(seed)
            model_ids = []
            artifact_ids = []
            named_query_ids = []
            run_ids = []
            for suffix in ("one", "two"):
                model_id, _target_id, artifact_id, named_query_id, run_id = await _seed_model_with_named_query(
                    seed, project_id, connection_id, suffix
                )
                model_ids.append(model_id)
                artifact_ids.append(artifact_id)
                named_query_ids.append(named_query_id)
                run_ids.append(run_id)
            await seed.commit()

        async with factory() as deleter:
            errors = await delete_project_cascade(deleter, project_id)
            assert errors == [], errors
            await deleter.commit()

        async with factory() as check:
            model_count = await check.execute(
                text("SELECT count(*) FROM models WHERE project_id = :pid"),
                {"pid": project_id},
            )
            assert model_count.scalar_one() == 0
            for artifact_id in artifact_ids:
                artifact_count = await check.execute(
                    text(
                        "SELECT count(*) FROM named_query_artifacts "
                        "WHERE id = :aid"
                    ),
                    {"aid": artifact_id},
                )
                assert artifact_count.scalar_one() == 0
            for named_query_id, run_id in zip(named_query_ids, run_ids):
                nq_count = await check.execute(
                    text("SELECT count(*) FROM named_queries WHERE id = :nqid"),
                    {"nqid": named_query_id},
                )
                assert nq_count.scalar_one() == 0
                child_counts = await check.execute(
                    text(
                        "SELECT "
                        "(SELECT count(*) FROM named_query_refresh_policies "
                        "WHERE named_query_id = :nqid) + "
                        "(SELECT count(*) FROM named_query_refresh_runs "
                        "WHERE named_query_id = :nqid OR id = :rid)"
                    ),
                    {"nqid": named_query_id, "rid": run_id},
                )
                assert child_counts.scalar_one() == 0
            tasks = list(
                (
                    await check.execute(
                        select(PhysicalCleanupTask).where(
                            PhysicalCleanupTask.project_id == project_id
                        )
                    )
                ).scalars().all()
            )
            assert {task.artifact_id for task in tasks} == set(artifact_ids)
            assert {task.artifact_kind for task in tasks} == {"named_query"}
            assert {task.requested_by for task in tasks} == {"project_delete"}
            assert all(task.qualified_table_name.startswith("analytics.nq_") for task in tasks)
            assert set(task.model_id for task in tasks) == set(model_ids)


# ---------------------------------------------------------------------------
# 5. CR-1 — an artifact finalisation and a model delete must SERIALISE
# ---------------------------------------------------------------------------


#: How long the finaliser holds its control-plane locks before writing the
#: aggregate row. It only has to outlast the cascade's ~40 single-row DELETE
#: statements (single-digit milliseconds), and must stay far below
#: ``MODEL_DEFINITION_LOCK_TIMEOUT_SECONDS`` (30) so the post-fix wait is a wait
#: and not a timeout.
_FINALISER_HOLD_SECONDS = 2.0


async def _seed_aggregate(
    session,
    model_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    already_purged: bool = True,
    count: int = 1,
) -> tuple[uuid.UUID, uuid.UUID]:
    """A ``DataTarget`` and ``count`` ``AggregateDefinition`` rows on it.

    ``already_purged=True`` (the default, used by the lock-order tests) stamps
    ``physical_table_purged_at`` so the cascade's F-020-04 physical drop is a
    no-op — those tests are about metadata lock order, and no target database
    exists here to drop from.

    ``already_purged=False`` leaves the stamp NULL so the drop path ACTUALLY
    runs. TMP-20260811210534687 lives entirely inside that
    path, and every pre-existing test in this file switched it off, which is
    precisely how the defect escaped.

    Returns ``(target_id, last_aggregate_id)``.
    """
    from datetime import datetime, timezone

    from shared.db.models import AggregateDefinition, DataTarget

    target_id, aggregate_id = uuid.uuid4(), uuid.uuid4()
    session.add(
        DataTarget(
            id=target_id,
            model_id=model_id,
            project_connection_id=connection_id,
            target_type="postgresql",
            display_name="T",
            config={},
        )
    )
    await session.flush()
    for i in range(count):
        aggregate_id = uuid.uuid4()
        session.add(
            AggregateDefinition(
                id=aggregate_id,
                model_id=model_id,
                target_id=target_id,
                physical_table_name=f"agg_t{i}",
                target_schema="aggs",
                status="active",
                grain=[],
                physical_table_purged_at=(
                    datetime.now(timezone.utc) if already_purged else None
                ),
            )
        )
    await session.flush()
    return target_id, aggregate_id


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_artifact_finalisation_and_model_delete_do_not_deadlock():
    """CR-1, reproduced against real PostgreSQL.

    The four artifact finalisers (optimizer aggregate creation incl. predictive
    builds, scheduler full refresh, scheduler incremental refresh, pocket
    finalisation) all funnel through
    ``shared/artifact_target_binding.lock_finalization_rows``. It locks
    ``project_connections`` -> ``data_targets`` -> ``data_sources`` and the
    caller then dirties the artifact row.

    ``delete_model_cascade`` walks the OPPOSITE order: ``aggregate_definitions``
    (cascade_delete.py step list) and only later ``data_sources`` /
    ``data_targets``.

    Before the fix the finalisation took NO advisory lock, so nothing serialised
    the two and the interleaving below is a textbook ABBA:

        finaliser : FOR UPDATE data_targets + data_sources ....... UPDATE agg row
        cascade   :        advisory lock -> DELETE agg row -> DELETE data_sources

    the cascade waits on the finaliser's ``data_sources`` lock while the
    finaliser waits on the cascade's ``aggregate_definitions`` row — 40P01, and
    if the cascade is the side PostgreSQL aborts, its metadata rolls back while
    the source-side DROP TABLE it already issued does not: the model stays
    visible with active definitions pointing at physical tables that are gone.

    After the fix ``lock_finalization_rows`` takes the SAME per-model advisory
    lock as its first statement, so the cascade parks on that lock before it
    touches a single row and the two run one after the other.
    """
    from shared.artifact_target_binding import lock_finalization_rows

    async with _isolated_schema() as (factory, _schema):
        async with factory() as seed:
            project_id, connection_id = await _seed_project(seed)
            model_id, _join_id = await _seed_model_with_children(
                seed, project_id, connection_id
            )
            target_id, aggregate_id = await _seed_aggregate(
                seed, model_id, connection_id
            )
            await seed.commit()

        finaliser = factory()
        deleter = factory()
        prelocked = asyncio.Event()
        finaliser_error: list[BaseException] = []

        async def _finalise():
            try:
                await lock_finalization_rows(
                    finaliser,
                    connection_ids=(connection_id,),
                    target_id=target_id,
                    model_id=model_id,
                )
                prelocked.set()
                # Hold the control-plane rows while the cascade runs, then take
                # the artifact row — the write every finaliser makes next.
                await asyncio.sleep(_FINALISER_HOLD_SECONDS)
                await finaliser.execute(
                    text(
                        "UPDATE aggregate_definitions SET is_stale = true "
                        "WHERE id = :aid"
                    ),
                    {"aid": aggregate_id},
                )
                await finaliser.commit()
            except BaseException as exc:  # noqa: BLE001 — inspected below
                finaliser_error.append(exc)
                prelocked.set()

        try:
            finalise = asyncio.create_task(_finalise())
            await asyncio.wait_for(prelocked.wait(), timeout=30)
            cascade = asyncio.create_task(delete_model_cascade(deleter, model_id))

            await asyncio.wait_for(finalise, timeout=60)
            cascade_errors = await asyncio.wait_for(cascade, timeout=60)

            deadlocked: list[str] = []
            if finaliser_error:
                if await _sqlstate(finaliser_error[0]) == _DEADLOCK:
                    deadlocked.append("the artifact finalisation")
                else:
                    raise finaliser_error[0]
            failed = [e for e in cascade_errors if "deadlock" in e.lower()]
            if failed:
                deadlocked.append(f"the model delete, mid-cascade ({failed[0][:120]})")
            assert not deadlocked, (
                f"deadlock (40P01) aborted {' and '.join(deadlocked)}. The "
                "artifact finalisation takes data_targets/data_sources without "
                "first acquiring the model's advisory lock, so it runs the "
                "opposite row order to delete_model_cascade (CR-1). An aborted "
                "delete rolls its metadata back while the physical tables it "
                "already dropped stay dropped."
            )
            assert cascade_errors == [], cascade_errors
            await deleter.commit()
        finally:
            await finaliser.close()
            await deleter.close()

        # Observable end state: the delete completed in full. No model left
        # visible, and no aggregate definition left pointing at storage the
        # cascade already reclaimed.
        async with factory() as check:
            for table in (
                "aggregate_definitions", "data_sources", "data_targets", "models",
            ):
                assert await _count(check, table, model_id) == 0, (
                    f"{table} still holds rows for the deleted model — the "
                    "cascade was aborted part-way through"
                )


# ---------------------------------------------------------------------------
# 6. Bug-8140 + TMP-20260811210534687 — the cascade must issue no pre-commit
#    target DDL and leave no pending ORM state beyond its detached cleanup rows
# ---------------------------------------------------------------------------


def _stub_source_ddl(monkeypatch) -> tuple[list[str], dict]:
    """Neutralise ONLY the target-database DDL and record what it was asked to run.

    The boundary under test is the ORM session/transaction, and that stays a
    real PostgreSQL transaction throughout. The one thing the isolated schema
    cannot provide is a reachable target database for ``DROP TABLE``.

    Bug-8140 moved the owner-delete physical drops out of the metadata
    transaction: ``delete_model_cascade`` / ``delete_project_cascade`` persist
    detached ``PhysicalCleanupTask`` rows, and the caller drains them only
    AFTER its commit through ``shared.physical_cleanup.execute_source_ddl`` —
    which is therefore the symbol this stub replaces (a stub on the legacy
    ``aggregate_table_ops`` symbol would make the guard vacuous twice over).

    The returned ``executed`` list is the ordering witness. It must stay empty
    from the cascade through the metadata commit, and only gain entries once
    the post-commit drain runs — a pre-commit DROP fails the first assertion
    and a drain that silently never ran fails the second. The ``failures``
    switch makes the stub raise for the next N calls so the tests can prove a
    failed post-commit attempt preserves durable retry identity.
    """
    executed: list[str] = []
    failures = {"remaining": 0}

    async def _fake_execute_source_ddl(
        conn_obj, sql, *, tenant_session=None, purpose=None
    ):
        if failures["remaining"] > 0:
            failures["remaining"] -= 1
            raise RuntimeError("target unavailable")
        executed.append(sql)

    monkeypatch.setattr(
        "shared.physical_cleanup.execute_source_ddl", _fake_execute_source_ddl
    )
    return executed, failures


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_model_delete_with_live_aggregate_tables_leaves_no_pending_orm_state(
    monkeypatch,
):
    """TMP-20260811210534687 + Bug-8140: deleting a model that HAS aggregates.

    History (TMP-20260811210534687): the old cascade ran the F-020-04 physical
    drops inside the metadata transaction, and ``drop_aggregate_physical_table``
    left two ORM mutations pending per drop — the ``physical_table_purged_at``
    stamp and a ``purged`` ``AggregateLifecycleEvent`` carrying the
    ``model_id`` of the model being deleted. Every cascade step is a Core
    ``text()`` execute and SQLAlchemy 2.0 autoflushes only on ORM-enabled
    statements, so the pending rows outlived the whole cascade — including
    ``DELETE FROM models`` — and were inserted by the first ORM statement the
    caller ran afterwards (``audit()``'s ``select(TenantSetting)``), violating
    ``aggregate_lifecycle_events_model_id_fkey`` and 500'ing the route. The
    metadata rolled back while the already-committed ``DROP TABLE``s did not.

    Bug-8140 changed the contract this test now pins. The cascade performs NO
    target DDL inside its transaction: it persists complete detached
    ``PhysicalCleanupTask`` rows that commit atomically with the metadata
    delete, and the endpoint drains them only after commit. This test replays
    the endpoint's exact sequence — cascade, one ORM statement, commit,
    post-commit drain — and asserts all four halves of the contract:

      * the cascade leaves no pending ORM state other than the detached
        cleanup rows (no FK-violating ``AggregateLifecycleEvent``, nothing
        dirty) and the commit succeeds;
      * no DDL runs before or during commit — the drop is strictly
        post-commit, so a rollback can never be half-applied on the target;
      * a failing post-commit drain preserves complete durable retry
        identity (status/attempts/identity survive the owner rows' deletion);
      * the retry drain drops the right physical tables and records terminal
        success evidence.
    """
    from shared.db.models import PhysicalCleanupTask, TenantSetting
    from shared.physical_cleanup import (
        attempt_scheduled_physical_cleanup,
        drain_physical_cleanup_tasks,
    )

    from sqlalchemy import select

    executed_ddl, failures = _stub_source_ddl(monkeypatch)

    async with _isolated_schema() as (factory, _schema):
        async with factory() as seed:
            project_id, connection_id = await _seed_project(seed)
            model_id, _join_id = await _seed_model_with_children(
                seed, project_id, connection_id
            )
            await _seed_aggregate(
                seed, model_id, connection_id, already_purged=False, count=2
            )
            await seed.commit()

        async with factory() as session:
            errors = await delete_model_cascade(session, model_id)
            assert errors == [], errors

            # Ordering, first half: the cascade's transaction issues NO target
            # DDL. A pre-commit DROP is irreversible on rollback and would
            # orphan the metadata delete — the exact Bug-8140 failure mode.
            assert executed_ddl == [], (
                "delete_model_cascade issued target DDL inside the metadata "
                f"transaction — drops must run only after commit: {executed_ddl}"
            )

            # Snapshot the session's pending ORM state BEFORE the flush, so the
            # assertion below can name what leaked even though the flush itself
            # is what failed pre-fix.
            leaked_new = [type(o).__name__ for o in session.new]
            leaked_dirty = [type(o).__name__ for o in session.dirty]

            # The ONLY pending state the cascade may leave is detached
            # cleanup rows, which flush atomically with the metadata delete.
            # (ORM-enabled reads inside identity resolution may legitimately
            # flush earlier tasks mid-cascade, so the durable count — not the
            # pending count — is asserted after commit below.)
            assert "AggregateLifecycleEvent" not in leaked_new, (
                "the cascade left a purged AggregateLifecycleEvent pending. It "
                "carries the model_id of the model just deleted, so the next "
                "flush violates aggregate_lifecycle_events_model_id_fkey."
            )
            assert set(leaked_new) <= {"PhysicalCleanupTask"}, (
                "delete_model_cascade returned pending ORM state other than "
                f"detached cleanup rows (new={leaked_new}, "
                f"dirty={leaked_dirty})."
            )
            assert not leaked_dirty, (
                "delete_model_cascade dirtied rows it then deleted "
                f"(dirty={leaked_dirty})."
            )

            # Exactly what ``delete_model`` / ``delete_project`` do next: one
            # ORM-enabled statement (this is audit()'s own first query) — which
            # DOES autoflush — and then commit.
            await session.execute(
                select(TenantSetting.value_json).where(
                    TenantSetting.key == "audit.log_level"
                )
            )
            await session.commit()

            # Ordering, second half: commit persisted the metadata delete and
            # the cleanup rows atomically, and STILL no DDL ran. The drop is a
            # strictly post-commit consumer invoked by the caller, not by the
            # commit itself.
            assert executed_ddl == [], (
                "target DDL ran before the post-commit drain: " f"{executed_ddl}"
            )

            # Both detached cleanup rows are durable now — the commit flushed
            # and persisted them atomically with the metadata delete.
            tasks = list(
                (
                    await session.execute(
                        select(PhysicalCleanupTask).order_by(
                            PhysicalCleanupTask.qualified_table_name
                        )
                    )
                ).scalars().all()
            )
            assert len(tasks) == 2, [t.id for t in tasks]
            for task in tasks:
                assert task.status == "pending"
                assert task.connection_id and task.project_id and task.model_id
                assert task.connection_type == "postgresql"
            assert {t.qualified_table_name for t in tasks} == {
                "aggs.agg_t0",
                "aggs.agg_t1",
            }, [t.qualified_table_name for t in tasks]

            # Post-commit drain, first attempt: both target drops fail. The
            # durable rows — not this request — own retry, so a failure must
            # preserve complete detached identity and count the attempt.
            failures["remaining"] = 2
            attempted = await attempt_scheduled_physical_cleanup(session)
            assert attempted == 2, attempted
            assert executed_ddl == [], (
                "a failed drop must not be recorded as executed: "
                f"{executed_ddl}"
            )

            tasks = list(
                (
                    await session.execute(
                        select(PhysicalCleanupTask).order_by(
                            PhysicalCleanupTask.qualified_table_name
                        )
                    )
                ).scalars().all()
            )
            assert len(tasks) == 2
            for task in tasks:
                assert task.status == "failed", task.id
                assert task.attempts == 1
                assert task.next_attempt_at is not None
                assert task.completed_at is None
                assert (
                    task.error_message
                    and "target unavailable" in task.error_message
                )
                # Complete detached identity survives the owner rows' deletion.
                assert task.connection_id and task.project_id and task.model_id
                assert task.connection_type == "postgresql"
            assert {t.qualified_table_name for t in tasks} == {
                "aggs.agg_t0",
                "aggs.agg_t1",
            }, [t.qualified_table_name for t in tasks]

            # Retry drain (the scheduler's shape: no explicit ids, claimable
            # due rows) drops the right physical tables and records terminal
            # evidence.
            await drain_physical_cleanup_tasks(session)
            assert sorted(executed_ddl) == [
                'DROP TABLE IF EXISTS "aggs"."agg_t0"',
                'DROP TABLE IF EXISTS "aggs"."agg_t1"',
            ], executed_ddl

            tasks = list(
                (await session.execute(select(PhysicalCleanupTask)))
                .scalars()
                .all()
            )
            assert len(tasks) == 2
            for task in tasks:
                assert task.status == "succeeded"
                assert task.attempts == 2
                assert task.completed_at is not None
                assert task.next_attempt_at is None
                assert task.error_message is None

        async with factory() as check:
            for table in (
                "aggregate_lifecycle_events",
                "aggregate_definitions",
                "data_targets",
                "models",
            ):
                assert await _count(check, table, model_id) == 0, (
                    f"{table} still holds rows for the deleted model"
                )


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_project_delete_with_live_aggregate_tables_survives_bug_9006(
    monkeypatch,
):
    """TMP-20260811210534687 + Bug-8140 on the OTHER caller of the same primitive.

    ``delete_project_cascade`` calls ``delete_model_cascade`` per model, and the
    per-model loop is itself the caller whose next ORM statement flushes the
    previous model's pending state — ``_collect_model_physical_tables``'
    ``select(AggregateDefinition)`` for model 2 autoflushes model 1's pending
    rows after model 1's ``models`` row is gone. That select is wrapped in a
    log-and-continue ``except``, so pre-fix the FK-violating purge event was
    swallowed into a warning and the poisoned session then failed every
    remaining step.

    Under the Bug-8140 contract the only pending rows the loop flushes are the
    detached ``PhysicalCleanupTask`` inserts (which carry no FKs at all), and
    no DDL runs anywhere in the project transaction. This test pins that on
    real Postgres: the per-model flush is safe, the commit succeeds, the drops
    stay strictly post-commit, and the drain's failure/retry lifecycle
    preserves identity for every one of the project's tasks.

    CLAUDE.md's shared-primitive rule: the fix is in the cascade, so both
    callers are covered — and this asserts it rather than assuming it.
    """
    from shared.db.models import PhysicalCleanupTask, TenantSetting
    from shared.physical_cleanup import (
        attempt_scheduled_physical_cleanup,
        drain_physical_cleanup_tasks,
    )

    from sqlalchemy import select

    executed_ddl, failures = _stub_source_ddl(monkeypatch)

    async with _isolated_schema() as (factory, _schema):
        async with factory() as seed:
            project_id, connection_id = await _seed_project(seed)
            m1, _ = await _seed_model_with_children(seed, project_id, connection_id)
            m2, _ = await _seed_model_with_children(seed, project_id, connection_id)
            for mid in (m1, m2):
                await _seed_aggregate(
                    seed, mid, connection_id, already_purged=False, count=2
                )
            await seed.commit()

        async with factory() as session:
            errors = await delete_project_cascade(session, project_id)
            assert errors == [], errors

            # Bug-8140 ordering: nothing in the project transaction may issue
            # target DDL — model 1's drops included.
            assert executed_ddl == [], (
                "delete_project_cascade issued target DDL inside the metadata "
                f"transaction — drops must run only after commit: {executed_ddl}"
            )
            leaked_new = [type(o).__name__ for o in session.new]
            leaked_dirty = [type(o).__name__ for o in session.dirty]
            assert set(leaked_new) <= {"PhysicalCleanupTask"}, (
                "delete_project_cascade left pending ORM state other than "
                f"detached cleanup rows (new={leaked_new}, "
                f"dirty={leaked_dirty})."
            )
            assert not leaked_dirty, f"new={leaked_new}, dirty={leaked_dirty}"
            await session.execute(
                select(TenantSetting.value_json).where(
                    TenantSetting.key == "audit.log_level"
                )
            )
            await session.commit()
            assert executed_ddl == [], (
                "target DDL ran before the post-commit drain: " f"{executed_ddl}"
            )

            # All four detached cleanup rows are durable now.
            tasks = list(
                (await session.execute(select(PhysicalCleanupTask)))
                .scalars()
                .all()
            )
            assert len(tasks) == 4, [t.id for t in tasks]
            for task in tasks:
                assert task.status == "pending"
                assert task.model_id in (m1, m2)
                assert task.connection_id and task.project_id
                assert task.qualified_table_name in {
                    "aggs.agg_t0",
                    "aggs.agg_t1",
                }

            # Post-commit drain, first attempt: every drop fails; each of the
            # four durable tasks must keep its retry identity.
            failures["remaining"] = 4
            attempted = await attempt_scheduled_physical_cleanup(session)
            assert attempted == 4, attempted
            assert executed_ddl == [], executed_ddl
            tasks = list(
                (await session.execute(select(PhysicalCleanupTask)))
                .scalars()
                .all()
            )
            assert len(tasks) == 4
            for task in tasks:
                assert task.status == "failed", task.id
                assert task.attempts == 1
                assert task.model_id in (m1, m2)
                assert task.connection_id and task.project_id
                assert task.qualified_table_name in {
                    "aggs.agg_t0",
                    "aggs.agg_t1",
                }

            # Retry drain succeeds for all four and records terminal evidence.
            await drain_physical_cleanup_tasks(session)
            assert len(executed_ddl) == 4, executed_ddl
            assert {sql.split(" ")[-1] for sql in executed_ddl} == {
                '"aggs"."agg_t0"',
                '"aggs"."agg_t1"',
            }, executed_ddl
            tasks = list(
                (await session.execute(select(PhysicalCleanupTask)))
                .scalars()
                .all()
            )
            for task in tasks:
                assert task.status == "succeeded"
                assert task.attempts == 2

        async with factory() as check:
            for mid in (m1, m2):
                for table in ("aggregate_lifecycle_events", "models"):
                    assert await _count(check, table, mid) == 0, (
                        f"{table} still holds rows for deleted model {mid}"
                    )
            assert (
                await check.execute(
                    text("SELECT count(*) FROM projects WHERE id = :pid"),
                    {"pid": project_id},
                )
            ).scalar_one() == 0
