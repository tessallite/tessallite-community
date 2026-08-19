"""Real-Postgres guards for the Bug-7982 R7 (3rd Codex gate) findings.

Every assertion here was reproduced live by the external gate before the fix, and
every one of them is mutation-proven (revert the fix -> the test goes red). They
exist because this lane has now had three consecutive same-family "zero findings"
convergences overturned by cross-family EXECUTION: a mocked session cannot show
any of these.

  finding 1  the pre-upsert dedup skipped a same-value write and STRANDED the
             ordering token, letting a staler interleaved writer win
  finding 2  clock_timestamp() is not unique; an exact tie readmitted
             last-commit-wins. The token is now a DB sequence value
  findings 3+4  the static AST lock-coverage checker false-PASSes several write
             shapes, and its route-derived discovery is blind to non-route
             writers. Replaced by a RUNTIME guard at the cursor
  finding 5  a per-row publish failure was swallowed, evaluate-batch returned
             200 anyway, and the durable outbox row was deleted on that 200
  finding 6  the lock-timeout test could not actually prove its ~1s bound

Skipped unless ``TESSALLITE_VERSIONING_DB_URL`` (or the importer-harness URL)
points at a reachable Postgres.

Run:
    cd tessallite/services/model-service
    TESSALLITE_VERSIONING_DB_URL=postgresql+asyncpg://user:pw@localhost:5432/db \
      pytest tests/integration/test_bug7982_r7_db.py -v
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select, text, update

from shared.db.kpi_eval_generation import allocate_kpi_eval_marker
from shared.db.model_write_lock_guard import (
    ModelWriteWithoutLockError,
    guard_mode,
)
from shared.db.models import KPI, KPILatest, Measure
from src.api._model_lock import acquire_model_definition_lock
from src.api.kpi_latest import _upsert_kpi_latest_batch

from tests.integration.test_versioning_consistency_db import (  # noqa: E402
    _DB_URL,
    _isolated_schema,
    _seed_model,
)

pytestmark = [pytest.mark.integration]

_EPOCH = 6
_TIME = datetime(2026, 7, 28, 10, 0, 0, tzinfo=timezone.utc)


def _resp(value, formatted=None):
    return SimpleNamespace(
        value=value, target=None, status=1, status_label="ok",
        trend_pct=None,
        formatted_value=str(value) if formatted is None else formatted,
    )


async def _seed_kpi(session, model_id) -> uuid.UUID:
    kpi_id = uuid.uuid4()
    session.add(KPI(id=kpi_id, model_id=model_id, name=f"kpi-{kpi_id.hex[:6]}"))
    await session.flush()
    await session.commit()
    return kpi_id


async def _stored(session, model_id, kpi_id) -> KPILatest | None:
    # ``populate_existing`` rather than ``expire_all``: expiring the whole
    # identity map would make the KPI object the next publish call reads go
    # lazy-load outside the greenlet context (a harness artefact, not a
    # production path — evaluate-batch loads its KPI objects fresh).
    return (
        await session.execute(
            select(KPILatest)
            .where(
                KPILatest.model_id == model_id, KPILatest.kpi_id == kpi_id
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Finding 1 — the dedup skip must not strand the ordering token
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
async def test_same_value_later_write_advances_the_token_so_a_staler_writer_loses():
    """The gate's reproduced three-writer sequence.

      A  publishes value=250 with ordering token 10
      B  evaluates LATER (token 30) and computes the SAME value 250
      C  evaluated BETWEEN them (token 20) with a STALE value 999

    B's write is numerically a no-op, but skipping it leaves the published token
    at 10, so C then passes the ordering guard and 999 is served — even though
    B's later, fresher evaluation confirmed 250.

    Mutation proof: change ``kpi_latest_write_is_noop`` back to comparing only
    ``(value tuple, epoch, version_id)`` and this test fails with 999.0.
    """
    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            kpi_id = await _seed_kpi(s, model_id)
            kpi = await s.get(KPI, kpi_id)
            objs = {kpi_id: kpi}

            # A — first publish.
            await _upsert_kpi_latest_batch(
                s, model_id, objs, {kpi_id: _resp(250)},
                eval_epoch=_EPOCH, eval_started_at=_TIME, eval_generation=10,
            )
            row = await _stored(s, model_id, kpi_id)
            assert float(row.value) == 250 and row.eval_generation == 10

            # B — same value, LATER token. Must still advance the stored token.
            outcome = await _upsert_kpi_latest_batch(
                s, model_id, objs, {kpi_id: _resp(250)},
                eval_epoch=_EPOCH, eval_started_at=_TIME, eval_generation=30,
            )
            assert outcome.succeeded
            row = await _stored(s, model_id, kpi_id)
            assert row.eval_generation == 30, (
                "the same-value write was skipped and left the ordering token "
                "stranded at A's value — finding 1"
            )

            # C — staler evaluation, newer commit. Must be SUPPRESSED.
            outcome = await _upsert_kpi_latest_batch(
                s, model_id, objs, {kpi_id: _resp(999)},
                eval_epoch=_EPOCH, eval_started_at=_TIME, eval_generation=20,
            )
            assert outcome.suppressed == 1 and outcome.persisted == 0
            row = await _stored(s, model_id, kpi_id)
            assert float(row.value) == 250, (
                f"a staler evaluation (token 20) clobbered the fresher one "
                f"(token 30) and $KPIs now serves {row.value} — finding 1"
            )


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
async def test_a_true_noop_is_still_skipped():
    """The dedup must keep suppressing genuinely identical writes.

    Same value, same binding, same token = nothing stored would change, so the
    write is still skipped (the F-017-29 write-amplification property survives
    the finding-1 fix).
    """
    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            kpi_id = await _seed_kpi(s, model_id)
            kpi = await s.get(KPI, kpi_id)
            objs = {kpi_id: kpi}
            args = dict(eval_epoch=_EPOCH, eval_started_at=_TIME, eval_generation=7)

            await _upsert_kpi_latest_batch(
                s, model_id, objs, {kpi_id: _resp(250)}, **args
            )
            outcome = await _upsert_kpi_latest_batch(
                s, model_id, objs, {kpi_id: _resp(250)}, **args
            )
            assert outcome.skipped == 1 and outcome.persisted == 0
            assert outcome.succeeded


# ---------------------------------------------------------------------------
# Finding 2 — an exact tie must not readmit last-commit-wins
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
async def test_identical_timestamps_are_ordered_by_the_unique_generation():
    """``clock_timestamp()`` repeats (82,242 duplicates in 100k live samples).

    Two same-epoch writers whose ``eval_started_at`` is byte-identical must still
    be totally ordered. With the generation as the ordering key the lower one is
    suppressed; under R6's timestamp ``<=`` comparison both were admitted and the
    last to commit won.

    Mutation proof: drop the ``eval_generation`` branch from
    ``_within_epoch_clause`` and this test fails with 999.0.
    """
    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            kpi_id = await _seed_kpi(s, model_id)
            kpi = await s.get(KPI, kpi_id)
            objs = {kpi_id: kpi}

            await _upsert_kpi_latest_batch(
                s, model_id, objs, {kpi_id: _resp(250)},
                eval_epoch=_EPOCH, eval_started_at=_TIME, eval_generation=20,
            )
            outcome = await _upsert_kpi_latest_batch(
                s, model_id, objs, {kpi_id: _resp(999)},
                eval_epoch=_EPOCH, eval_started_at=_TIME, eval_generation=10,
            )
            assert outcome.suppressed == 1
            row = await _stored(s, model_id, kpi_id)
            assert float(row.value) == 250, (
                "an exact-tie timestamp let the staler writer win — finding 2"
            )


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
async def test_a_generationless_write_cannot_erase_a_stored_ordering_token():
    """R7 review round 1, finding 5 — a wrong number produced by the ordering
    mechanism itself.

    A DEGRADED writer (its ``nextval`` failed, so it carries no generation) used
    to write ``eval_generation = NULL`` over a stored, stronger token. The row
    then sorted as "oldest", and a genuinely STALER evaluation could publish over
    it. Worse, the no-op predicate made that write MANDATORY rather than
    optional, because its later timestamp counted as an advance.

    Mutation proof: drop ``strip_ungenerated_token`` from the upsert's ``set_``
    and the second assertion fails with eval_generation None, then the third
    publishes the stale 999.
    """
    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            kpi_id = await _seed_kpi(s, model_id)
            kpi = await s.get(KPI, kpi_id)
            objs = {kpi_id: kpi}

            # A healthy writer establishes a strong token.
            await _upsert_kpi_latest_batch(
                s, model_id, objs, {kpi_id: _resp(250)},
                eval_epoch=_EPOCH, eval_started_at=_TIME, eval_generation=500,
            )
            # A DEGRADED writer (no generation) with a later timestamp.
            await _upsert_kpi_latest_batch(
                s, model_id, objs, {kpi_id: _resp(250)},
                eval_epoch=_EPOCH,
                eval_started_at=_TIME.replace(minute=5),
                eval_generation=None,
            )
            row = await _stored(s, model_id, kpi_id)
            assert row.eval_generation == 500, (
                "a generation-less write erased the stored ordering token; the "
                "row now sorts oldest and any staler evaluation can publish"
            )

            # A genuinely staler evaluation must still lose.
            outcome = await _upsert_kpi_latest_batch(
                s, model_id, objs, {kpi_id: _resp(999)},
                eval_epoch=_EPOCH, eval_started_at=_TIME, eval_generation=100,
            )
            assert outcome.suppressed == 1
            row = await _stored(s, model_id, kpi_id)
            assert float(row.value) == 250


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
async def test_generation_sequence_is_strictly_increasing_and_unique():
    """The property the whole ordering rests on, measured against real Postgres."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            markers = [await allocate_kpi_eval_marker(s) for _ in range(200)]
            gens = [m.generation for m in markers]
            assert all(g is not None for g in gens), (
                "kpi_eval_generation_seq is missing — ordering degraded to the "
                "non-unique clock marker"
            )
            assert len(set(gens)) == len(gens), "generation values repeated"
            assert gens == sorted(gens), "generation values were not increasing"


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
async def test_marker_degrades_cleanly_when_the_sequence_is_absent():
    """A deployment whose migration has not run must keep publishing.

    The allocation falls back to the timestamp-only marker inside a SAVEPOINT, so
    the caller's transaction is not poisoned by the failed ``nextval``.
    """
    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            await s.execute(text("DROP SEQUENCE kpi_eval_generation_seq"))
            await s.commit()
            marker = await allocate_kpi_eval_marker(s)
            assert marker.generation is None
            assert marker.started_at is not None
            # The session is still usable — the savepoint contained the failure.
            assert (await s.execute(select(text("1")))).scalar_one() == 1


# ---------------------------------------------------------------------------
# Findings 3+4 — the RUNTIME guard catches what the AST checker cannot
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["name_bound", "chained_builder", "raw_cte"])
async def test_runtime_guard_catches_write_shapes_the_ast_checker_passes(shape):
    """Each shape was reported by the R7 gate as ``(True, "ok")`` from the static
    checker. At the cursor there is no shape to hide behind."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            await s.commit()
            with guard_mode("strict"):
                with pytest.raises(ModelWriteWithoutLockError) as exc:
                    if shape == "name_bound":
                        stmt = (
                            update(Measure)
                            .where(Measure.model_id == model_id)
                            .values(name="x")
                        )
                        await s.execute(stmt)
                    elif shape == "chained_builder":
                        await s.execute(
                            delete(Measure).where(Measure.model_id == model_id)
                        )
                    else:
                        await s.execute(
                            text(
                                "WITH d AS (DELETE FROM measures WHERE model_id = :m "
                                "RETURNING id) SELECT count(*) FROM d"
                            ),
                            {"m": model_id},
                        )
            assert "measures" in str(exc.value)
            await s.rollback()


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
async def test_runtime_guard_catches_a_write_on_an_unrecognised_session_name():
    """R6 recognised writes only on a hardcoded receiver-name allow-list."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as some_other_name:
            model_id = await _seed_model(some_other_name)
            await some_other_name.commit()
            with guard_mode("strict"):
                with pytest.raises(ModelWriteWithoutLockError):
                    await some_other_name.execute(
                        text("UPDATE measures SET name = 'x' WHERE model_id = :m"),
                        {"m": model_id},
                    )
            await some_other_name.rollback()


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
async def test_runtime_guard_allows_a_write_under_a_real_lock_and_reads_always():
    """Mutation proof for the guard's positive side: it must NOT fire when the
    lock is genuinely held, and must never fire on a read."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            await s.commit()
            with guard_mode("strict"):
                # A read never trips it.
                await s.execute(select(Measure).where(Measure.model_id == model_id))
                # A write after a genuine acquisition is allowed.
                await acquire_model_definition_lock(s, model_id)
                await s.execute(delete(Measure).where(Measure.model_id == model_id))
                await s.commit()


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
async def test_runtime_guard_lock_record_does_not_survive_the_transaction():
    """``pg_advisory_xact_lock`` is released at commit, so the guard's record of
    it must be too — otherwise a later unlocked write in a REUSED connection
    would be waved through (a false PASS of exactly the kind this lane keeps
    producing)."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            await s.commit()
            with guard_mode("strict"):
                await acquire_model_definition_lock(s, model_id)
                await s.execute(delete(Measure).where(Measure.model_id == model_id))
                await s.commit()  # lock released here
                with pytest.raises(ModelWriteWithoutLockError):
                    await s.execute(
                        delete(Measure).where(Measure.model_id == model_id)
                    )
            await s.rollback()


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
@pytest.mark.parametrize("savepoint_outcome", ["released", "rolled_back"])
async def test_runtime_guard_lock_record_survives_a_savepoint(savepoint_outcome):
    """A SAVEPOINT must NOT clear the lock record.

    PostgreSQL does not release a transaction-level advisory lock on
    subtransaction end, so clearing on a savepoint boundary would make every
    locked handler that uses ``begin_nested`` (the kpi_latest upsert helpers, the
    sweep's per-row isolation, the rehydrator) report a FALSE violation — and in
    ``strict`` mode, break. The guard's clearing must track the OUTER transaction
    only.
    """
    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            await s.commit()
            with guard_mode("strict"):
                await acquire_model_definition_lock(s, model_id)
                if savepoint_outcome == "released":
                    async with s.begin_nested():
                        await s.execute(select(Measure))
                else:
                    with pytest.raises(Exception):
                        async with s.begin_nested():
                            await s.execute(text("SELECT 1/0"))
                # Still inside the SAME outer transaction, so still locked.
                await s.execute(delete(Measure).where(Measure.model_id == model_id))
                await s.commit()


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
async def test_an_exempt_session_is_not_reported_and_the_exemption_is_scoped():
    """R7 review round 1, finding 2 — the exemption must actually work AND end.

    Wholesale-rebuild paths (project/catalogue/dbt/cube/AtScale import) declare
    themselves deliberate non-holders. Without that, they claim every table-set
    report key within minutes of process start and mute the guard for everything
    that matters. The exemption must also be strictly scoped: a write after the
    block is a violation again.
    """
    from shared.db.model_write_lock_guard import model_write_lock_exempt

    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            await s.commit()
            with guard_mode("strict"):
                async with model_write_lock_exempt(s, "test: wholesale rebuild"):
                    await s.execute(
                        delete(Measure).where(Measure.model_id == model_id)
                    )
                # Exemption ended with the block — the guard is armed again.
                with pytest.raises(ModelWriteWithoutLockError):
                    await s.execute(
                        delete(Measure).where(Measure.model_id == model_id)
                    )
            await s.rollback()


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
async def test_the_lock_primitive_refuses_an_autocommit_connection():
    """R7 review round 1, finding 4 case 8.

    ``pg_advisory_xact_lock`` is released at TRANSACTION end. Under AUTOCOMMIT
    there is no transaction beyond the statement, so the lock is gone the instant
    the call returns — yet the runtime guard would go on treating the connection
    as locked. The primitive refuses rather than hand back a lock that does not
    exist.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from shared.db.model_lock import AutocommitLockError

    engine = create_async_engine(_DB_URL, isolation_level="AUTOCOMMIT")
    try:
        session = async_sessionmaker(engine, expire_on_commit=False)
        async with session() as s:
            with pytest.raises(AutocommitLockError):
                await acquire_model_definition_lock(s, uuid.uuid4())
    finally:
        await engine.dispose()


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
async def test_a_real_revert_writes_only_revert_owned_tables():
    """GROUND TRUTH for the guarded table set (R7 review rounds 2 B1, 3 B2).

    The guarded set claims to be "tables a model REVERT delete-and-reinserts".
    The first R7 draft derived it from every insert/delete in rehydrator.py,
    which over-counted by a whole family: the aggregate and pocket teardown is
    gated on ``if not preserve_aggregates:`` / ``if not preserve_pockets:`` and
    the revert path passes BOTH True. Guarding that family made every
    aggregate/pocket lifecycle writer look like an unlocked violation against a
    DELETE-REINSERT race that cannot occur -- the flood that drives an operator
    to switch the guard off. Inferring the exclusion from the call graph was
    tried and rejected (it wrongly excluded ``personas``), so the exclusion is
    an explicit list and THIS test is what makes it trustworthy.

    Round 3's version omitted ``restore_governance=False`` and ``actor``, so it
    ran the DEPLOY governance path, not the revert: the revert-only branches
    (governance capture/detach, the ModelAlert, the preserved measure-link
    restore) were unreachable and the exclusion was never actually exercised
    against a revert. It also asserted that any table outside guarded|excluded
    was a HOLE, which is false -- a revert legitimately APPENDS a governance
    alert; that class is allowlisted explicitly instead.

    Mutation proofs: (a) drop ``preserve_aggregates=True`` -> RED naming the
    aggregate tables; (b) remove ``model_alerts`` from _APPEND_ONLY -> RED,
    proving the revert-only alert branch is genuinely reached.
    """
    import re as _re

    from sqlalchemy import event as _event
    from sqlalchemy.engine import Engine as _Engine

    from shared.db.models import (
        AggregateColumn, AggregateDefinition, DataSource, DataTag, DataTarget,
        Dimension, ModelColumn, ModelTable, Persona, PocketDefinition,
        PocketPredicate, ProjectConnection, RowSecurityRule, data_tag_columns,
    )
    from shared.model_snapshot.rehydrator import rehydrate_into_live
    from shared.model_snapshot.snapshot_owned_tables import (
        APPEND_ONLY_TABLES, gated_families, snapshot_owned_tables,
    )
    from shared.db.model_write_lock_guard import written_tables as _written

    #: Tables a revert APPENDS to and never delete-and-reinserts. Not
    #: snapshot-owned: an append cannot be lost to, or clobber, a revert.
    #:
    #: Bug-8439: this used to be a LOCAL literal ``{"model_alerts"}`` while the
    #: derivation carried no notion of append-only at all — the same claim in two
    #: places, one of which nothing checked. The ORM-construction write shape now
    #: makes ``model_alerts`` visible to the derivation, so the claim lives in
    #: ``APPEND_ONLY_TABLES`` (with a derived justification: the rehydrator must
    #: never delete or update the table) and this test reads it from there.
    _APPEND_ONLY = set(APPEND_ONLY_TABLES)

    observed: set[tuple[str, str]] = set()
    _DESTRUCTIVE = _re.compile(r"^\s*(?:WITH.*?)?(INSERT|DELETE|TRUNCATE)",
                               _re.IGNORECASE | _re.DOTALL)

    def _spy(_conn, _cur, statement, _params, _ctx, _many):
        tables = _written(statement)
        if not tables:
            return
        m = _DESTRUCTIVE.match(statement)
        verb = (m.group(1).upper() if m else "UPDATE")
        for t in tables:
            observed.add((verb, t))

    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            project_id = (await s.execute(
                text("SELECT project_id FROM models WHERE id = :m"),
                {"m": model_id},
            )).scalar_one()
            conn_id, target_id, source_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            table_id, col_id, tag_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            agg_id, measure_id, pocket_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            s.add(ProjectConnection(
                id=conn_id, project_id=project_id, display_name="c",
                connection_type="postgresql", encrypted_credentials=b"x"))
            await s.flush()
            s.add(DataSource(id=source_id, model_id=model_id,
                             project_connection_id=conn_id, display_name="src",
                             source_type="postgresql"))
            s.add(DataTarget(id=target_id, model_id=model_id,
                             project_connection_id=conn_id,
                             target_type="postgresql", display_name="t"))
            await s.flush()
            s.add(ModelTable(id=table_id, model_id=model_id, source_id=source_id,
                             table_type="fact", physical_name="f", alias="f",
                             display_name="F"))
            await s.flush()
            s.add(ModelColumn(id=col_id, model_table_id=table_id,
                              column_name="c1", data_type="int"))
            s.add(Measure(id=measure_id, model_id=model_id, name="m_probe",
                          display_name="M"))
            s.add(Dimension(id=uuid.uuid4(), model_id=model_id, name="d_probe",
                            display_name="D"))
            # Governance: PRESERVED by the revert, and detached by it here (the
            # reverted-to snapshot drops the table/column they point at), which
            # is what reaches the revert-only ModelAlert branch.
            s.add(Persona(id=uuid.uuid4(), model_id=model_id, name="p", slug="p"))
            s.add(DataTag(id=tag_id, model_id=model_id, tag_name="pii"))
            await s.flush()
            s.add(RowSecurityRule(id=uuid.uuid4(), model_id=model_id, name="r",
                                  dimension_path="d_probe", rule_type="static",
                                  mapping_table_id=table_id))
            await s.execute(data_tag_columns.insert().values(
                tag_id=tag_id, model_column_id=col_id))
            # Preserve-gated family, with the measure link and the predicate
            # whose teardown WOULD be visible if the revert tore it down.
            s.add(AggregateDefinition(id=agg_id, model_id=model_id,
                                      target_id=target_id,
                                      physical_table_name="agg_probe",
                                      status="active", grain=["d_probe"]))
            s.add(PocketDefinition(id=pocket_id, model_id=model_id,
                                   target_id=target_id,
                                   physical_table_name="pk_probe",
                                   defining_sql="select 1",
                                   query_fingerprint="qf",
                                   predicate_set_hash="ph"))
            await s.flush()
            s.add(AggregateColumn(id=uuid.uuid4(), aggregate_definition_id=agg_id,
                                  physical_col_name="c", stat_type="sum",
                                  measure_id=measure_id))
            s.add(PocketPredicate(id=uuid.uuid4(), pocket_definition_id=pocket_id,
                                  column_name="d_probe", operator="=",
                                  value_json=["x"]))
            await s.commit()

        snapshot = {
            "schema_version": 4,
            "model": {"id": str(model_id)},
            "measures": [],
            "hierarchies": [],
        }
        async with factory() as s:
            _event.listen(_Engine, "before_cursor_execute", _spy)
            try:
                # EXACTLY the production revert's arguments (versions.py:1014).
                await rehydrate_into_live(
                    model_id, snapshot, s,
                    drop_orphan_aggregates=True,
                    preserve_aggregates=True,
                    preserve_pockets=True,
                    restore_governance=False,
                    actor="reviewer@test",
                )
                await s.commit()
            finally:
                _event.remove(_Engine, "before_cursor_execute", _spy)

    guarded, excluded = snapshot_owned_tables(), gated_families()
    written = {t for _v, t in observed}

    # ``excluded`` now also carries the APPEND-ONLY family, whose whole point is
    # that a revert DOES insert into it. Subtract it before asking "did the
    # revert tear down something we excluded as preserve-gated?", or this test
    # would flag its own fixture's governance-detach alert as a teardown.
    torn_down = {t for v, t in observed
                 if v in {"INSERT", "DELETE", "TRUNCATE"}} & (excluded - _APPEND_ONLY)
    assert not torn_down, (
        "a REAL revert DELETE/INSERTed table(s) the guarded set excludes as "
        f"preserve-gated: {sorted(torn_down)}. PRESERVE_GATED_TABLES in "
        "shared/model_snapshot/snapshot_owned_tables.py is wrong and those "
        "tables' writers are unguarded against the delete-reinsert race."
    )
    unknown = {t for t in written if t not in (guarded | excluded | _APPEND_ONLY)}
    assert not unknown, (
        f"a REAL revert wrote table(s) that are neither guarded, knowingly "
        f"excluded, nor append-only: {sorted(unknown)} -- the derivation has a HOLE."
    )
    # Non-vacuity: the revert-only branches must actually have been reached.
    assert written & guarded, f"no guarded table written (observed={sorted(observed)})"
    assert ("UPDATE", "row_security_rules") in observed, (
        "the restore_governance=False capture/detach branch was not reached -- "
        "this test is not running the revert path"
    )
    assert written & _APPEND_ONLY, (
        "the governance-detach alert branch was not reached -- the fixture no "
        "longer exercises the revert-only path this test exists to cover"
    )


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
async def test_a_revert_never_regresses_models_data_epoch():
    """``data_epoch`` is a MONOTONIC data-freshness counter folded into the KPI
    evaluation cache key (shared/model_refresh_epoch.py, kpi_cache.py). If a
    revert restores an OLDER snapshot value, the cache key moves BACKWARDS and a
    pre-refresh scorecard entry is re-served after the revert.

    ``deploy_epoch`` is excluded from rehydration for exactly this reason
    (rehydrator.py _MODEL_SCALAR_EXCLUDE); ``data_epoch`` was not, on either the
    serialiser or the rehydrate side. Live-measured 9 -> 3 before the fix -- a
    deterministic wrong number on every revert of a refreshed model, no race
    required (R7 review round 3, B3).
    """
    from shared.db.models import Model
    from shared.model_snapshot.rehydrator import rehydrate_into_live
    from shared.model_snapshot.serialiser import snapshot_model

    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            m = await s.get(Model, model_id)
            m.data_epoch, m.deploy_epoch = 3, 1
            await s.commit()

        async with factory() as s:
            snapshot = await snapshot_model(model_id, s)
        assert "data_epoch" not in snapshot["model"], (
            "the serialiser must not carry data_epoch in the snapshot -- it is a "
            "monotonic counter, not model shape (mirrors deploy_epoch)"
        )

        async with factory() as s:  # six refreshes advance the epoch
            m = await s.get(Model, model_id)
            m.data_epoch, m.deploy_epoch = 9, 4
            await s.commit()

        async with factory() as s:
            await rehydrate_into_live(
                model_id, snapshot, s,
                drop_orphan_aggregates=True, preserve_aggregates=True,
                preserve_pockets=True, restore_governance=False,
                actor="reviewer@test",
            )
            await s.commit()

        async with factory() as s:
            row = (await s.execute(
                select(Model.data_epoch, Model.deploy_epoch)
                .where(Model.id == model_id)
            )).one()
        assert row[0] == 9, (
            f"revert regressed models.data_epoch 9 -> {row[0]}: the KPI "
            "evaluation cache key moves backwards and a pre-refresh scorecard "
            "entry is re-served"
        )
        assert row[1] == 4, "revert must not rehydrate deploy_epoch either"

        # LEGACY-SNAPSHOT LEG (R7 review round 4, B2). The leg above cannot see
        # the rehydrator-side exclusion at all: ``_apply_model_scalars`` only
        # writes a field that is PRESENT in the snapshot, and the fixed
        # serialiser no longer emits ``data_epoch`` — so removing "data_epoch"
        # from _MODEL_SCALAR_EXCLUDE leaves the assertions above GREEN. That
        # half is the ONLY thing protecting the version rows ALREADY STORED in
        # production, every one of which still carries data_epoch in its
        # snapshot_json. Reverting to any pre-upgrade version takes this path.
        #
        # Mutation proof: drop "data_epoch" from _MODEL_SCALAR_EXCLUDE only ->
        # this assert fails with 3 while the leg above stays green.
        legacy = dict(snapshot)
        legacy["model"] = dict(snapshot["model"])
        legacy["model"]["data_epoch"] = 3
        async with factory() as s:
            m = await s.get(Model, model_id)
            m.data_epoch = 9
            await s.commit()
        async with factory() as s:
            await rehydrate_into_live(
                model_id, legacy, s,
                drop_orphan_aggregates=True, preserve_aggregates=True,
                preserve_pockets=True, restore_governance=False,
                actor="reviewer@test",
            )
            await s.commit()
        async with factory() as s:
            legacy_epoch = (await s.execute(
                select(Model.data_epoch).where(Model.id == model_id)
            )).scalar_one()
        assert legacy_epoch == 9, (
            f"revert of a PRE-UPGRADE version snapshot regressed data_epoch "
            f"9 -> {legacy_epoch}: _MODEL_SCALAR_EXCLUDE is the only thing "
            "protecting every version row already stored in production"
        )


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
async def test_the_refresh_epoch_bump_is_not_reported_by_the_write_guard(caplog):
    """B3's rationale pinned to BEHAVIOUR, not to the constant that encodes it.

    ``bump_data_epoch`` runs on EVERY successful data refresh (scheduler
    full/incremental refresh, pocket refresh) and holds no per-model definition
    lock. While ``models`` was in the guarded set, a healthy tenant re-emitted
    the ``{models}`` report every re-arm window forever and kept that key
    claimed — the guard self-muting on the highest-frequency benign writer.

    The unit-level assertion ``"models" in UPDATE_IN_PLACE_TABLES`` only
    restates the constant, so mutating the constant is guaranteed to fail it and
    it proves nothing about why the constant exists (R7 review round 5, O2).
    This asserts the property.

    Mutation proof: drop "models" from UPDATE_IN_PLACE_TABLES -> RED with one
    ``[models]`` report naming the epoch UPDATE.
    """
    import logging

    from shared.db.model_write_lock_guard import reset_reported_cache
    from shared.model_refresh_epoch import bump_data_epoch

    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            await s.commit()
        reset_reported_cache()
        caplog.clear()
        with caplog.at_level(
            logging.ERROR, logger="shared.db.model_write_lock_guard"
        ):
            async with factory() as s:
                await bump_data_epoch(s, model_id)
                await s.commit()
        reports = [r.getMessage() for r in caplog.records
                   if "Bug-7982" in r.getMessage()]
    assert not reports, (
        "the ordinary refresh epoch bump was reported as an unlocked "
        f"snapshot-owned write; the guard self-mutes on it: {reports}"
    )


# Finding 4's actual writer (``optimizer/src/stats/collector.py``) is proven at
# RUNTIME in the optimizer's own suite —
# ``services/optimizer/tests/test_bug7982_r7_stats_lock_db.py`` —
# because it must be exercised inside that service's package, not asserted about
# from here by reading its source text.


# ---------------------------------------------------------------------------
# Finding 5 — a publish failure must not delete its own safety net
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
async def test_overlong_formatted_value_is_bounded_not_a_publish_failure():
    """The concrete, reachable trigger the gate identified.

    ``KPILatest.formatted_value`` is ``String(128)`` and was NOT truncated
    (unlike ``status_label``), so a long formatted value raised
    ``StringDataRightTruncationError`` inside the swallowed per-row handler.

    Mutation proof: drop ``formatted_value`` from ``bound_kpi_latest_strings``
    and this test fails — ``outcome.failed`` becomes 1 and nothing is stored.
    """
    long_value = "£" + "9" * 400
    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            kpi_id = await _seed_kpi(s, model_id)
            kpi = await s.get(KPI, kpi_id)

            outcome = await _upsert_kpi_latest_batch(
                s, model_id, {kpi_id: kpi}, {kpi_id: _resp(250, formatted=long_value)},
                eval_epoch=_EPOCH, eval_started_at=_TIME, eval_generation=1,
            )
            assert outcome.failed == 0, "an over-long formatted value still failed"
            assert outcome.persisted == 1 and outcome.succeeded
            row = await _stored(s, model_id, kpi_id)
            assert len(row.formatted_value) <= 128
            assert row.formatted_value.endswith("…")


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
async def test_a_publish_failure_is_reported_not_swallowed():
    """A row that cannot be written must make ``succeeded`` False.

    Forced with a KPI id that has no ``kpis`` row (FK violation). Previously the
    exception was logged and dropped, the helper returned None, evaluate-batch
    returned 200, and the durable outbox row was deleted on that 200.
    """
    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            await s.commit()
            ghost_kpi_id = uuid.uuid4()
            ghost = SimpleNamespace(name="ghost")

            outcome = await _upsert_kpi_latest_batch(
                s, model_id, {ghost_kpi_id: ghost}, {ghost_kpi_id: _resp(250)},
                eval_epoch=_EPOCH, eval_started_at=_TIME, eval_generation=1,
            )
            assert outcome.failed == 1, outcome
            assert not outcome.succeeded, (
                "a failed publish reported success — the durable outbox row "
                "would be deleted on this (finding 5)"
            )


def test_outbox_is_retained_when_the_response_does_not_confirm_publication():
    """Both clearing paths read the SAME explicit contract, and fail closed.

    Pure — no DB. Covers the trigger and the sweep drain together so the two
    cannot drift.
    """
    from src.kpi_reeval_trigger import _publish_confirmed

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            if self._payload is None:
                raise ValueError("not json")
            return self._payload

    mid, tid = uuid.uuid4(), "acme"
    assert _publish_confirmed(_Resp({"kpi_latest_published": True}), mid, tid)
    assert not _publish_confirmed(_Resp({"kpi_latest_published": False}), mid, tid)
    assert not _publish_confirmed(
        _Resp({"kpi_latest_published": None, "kpi_latest_failed": 2}), mid, tid
    )
    # An OLD model-service that does not report the field at all.
    assert not _publish_confirmed(_Resp({"results": []}), mid, tid)
    # A 200 whose body cannot be parsed is not proof either.
    assert not _publish_confirmed(_Resp(None), mid, tid)


# ---------------------------------------------------------------------------
# Bug-8437 / Bug-8441 - the newly locked writers must actually SERIALISE
#
# Review round 2 (Bug-8710): the lane's thesis is "a lost update cannot be closed
# by DETECTION, only by mutual EXCLUSION". The evidence that exclusion actually
# happens was a STATIC AST checker and nothing else - the same family of checker
# four consecutive external Codex gates rejected, now carrying the closure of a
# lost-update bug on its own. ``tests/conftest.py`` additionally stubs the lock
# out in exactly these three modules, so not one of the ~4,000 unit tests
# executes a real acquisition on these paths. (Writing this test proved that
# immediately: with the stub in place the handler walked straight through a HELD
# lock.)
#
# These drive the REAL handler functions against a REAL Postgres while another
# session holds the same advisory lock a revert holds. A handler that merely
# CONTAINS the lock call cannot pass them.
# ---------------------------------------------------------------------------


async def _pinned(factory, schema):
    """A session pinned to the isolated schema.

    The shared harness sets ``search_path`` in an engine ``connect`` event. These
    tests deliberately provoke a lock-timeout DBAPIError, after which a
    replacement pooled connection can come back without it — which would mask the
    property under test behind a harness artefact. Pin it per session instead.
    """
    session = factory()
    await session.execute(text(f'SET search_path TO "{schema}"'))
    return session


def _bind_handler_module(monkeypatch, module, session):
    """Point a handler module at ``session`` and restore its REAL lock call."""
    async def _fake_get_tenant_db(_tenant):
        yield session

    monkeypatch.setattr(module, "get_tenant_db", _fake_get_tenant_db)
    # Undo ``tests/conftest.py``'s autouse ``_stub_ordinary_writer_model_lock``
    # for this module: without this the test proves nothing at all.
    monkeypatch.setattr(
        module, "acquire_model_definition_lock", acquire_model_definition_lock,
    )


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_newly_locked_writers_block_on_a_held_revert_lock(monkeypatch):
    """Mutation proof: delete the ``acquire_model_definition_lock`` line from any
    one of the three handlers and its "blocked" assertion fails - the call
    completes against the model row while the revert still holds the lock, which
    is precisely the silent lost update Bug-8437 describes."""
    from fastapi import HTTPException

    import src.api.models as models_api
    import src.api.sources as sources_api
    import src.api.targets as targets_api
    from shared.config.settings import get_settings
    from shared.db.models import Model as _Model
    from shared.schemas.pydantic_models import (
        DataSourceUpdate, DataTargetUpdate, ModelUpdate,
    )
    from src.auth.middleware import CurrentUser

    # Bound the wait so a genuine block surfaces as the retryable 503 in ~1s
    # instead of hanging the suite.
    monkeypatch.setattr(
        get_settings(), "MODEL_DEFINITION_LOCK_TIMEOUT_SECONDS", 1, raising=False
    )

    async with _isolated_schema() as (factory, schema):
        setup = await _pinned(factory, schema)
        try:
            model_id = await _seed_model(setup)
            project_id = (await setup.get(_Model, model_id)).project_id
            await setup.commit()
        finally:
            await setup.close()

        user = CurrentUser(user_id="u@test", tenant_id="t", email="u@test")

        async def _call_update_model(session):
            _bind_handler_module(monkeypatch, models_api, session)
            return await models_api.update_model(
                project_id, model_id, ModelUpdate(display_name="renamed-by-user"),
                current_user=user,
            )

        async def _call_update_source(session):
            _bind_handler_module(monkeypatch, sources_api, session)
            return await sources_api.update_source(
                project_id, model_id, uuid.uuid4(),
                DataSourceUpdate(display_name="edited"), current_user=user,
            )

        async def _call_update_target(session):
            _bind_handler_module(monkeypatch, targets_api, session)
            return await targets_api.update_target(
                project_id, model_id, uuid.uuid4(),
                DataTargetUpdate(display_name="edited"), current_user=user,
            )

        async def _call_delete_target(session):
            _bind_handler_module(monkeypatch, targets_api, session)
            return await targets_api.delete_target(
                project_id, model_id, uuid.uuid4(), current_user=user,
            )

        async def _call_create_source(session):
            from shared.schemas.pydantic_models import DataSourceCreate
            _bind_handler_module(monkeypatch, sources_api, session)
            return await sources_api.create_source(
                project_id, model_id,
                DataSourceCreate(
                    project_connection_id=uuid.uuid4(),
                    source_type="jdbc", display_name="probe",
                ),
                current_user=user,
            )

        async def _call_create_target(session):
            from shared.schemas.pydantic_models import DataTargetCreate
            _bind_handler_module(monkeypatch, targets_api, session)
            return await targets_api.create_target(
                project_id, model_id,
                DataTargetCreate(
                    project_connection_id=uuid.uuid4(),
                    target_type="postgresql", display_name="probe",
                ),
                current_user=user,
            )

        # Bug-8726 / Bug-8730: ALL SIX newly locked endpoints are proven here.
        # ``create_target`` became coverable once its ownership check was hoisted
        # so the lock sits where ``create_source`` puts it - before the
        # connection validation that would otherwise 422 first.
        calls = {
            "update_model": _call_update_model,
            "update_source": _call_update_source,
            "update_target": _call_update_target,
            "delete_target": _call_delete_target,
            "create_source": _call_create_source,
            "create_target": _call_create_target,
        }

        for name, call in calls.items():
            # --- non-vacuity: with the lock FREE the call must get PAST it ----
            writer = await _pinned(factory, schema)
            try:
                await call(writer)
            except HTTPException as exc:
                assert exc.status_code != 503, (
                    name + " 503s with the lock FREE - a later 503 would not "
                    "prove serialisation"
                )
                # 404 (unknown source/target id) and 422 (unknown connection)
                # are both raised AFTER the lock, which is exactly what this half
                # proves.
                assert exc.status_code in (404, 422), exc.status_code
            finally:
                await writer.rollback()
                await writer.close()

            # --- the revert holds the per-model lock, uncommitted -------------
            holder = await _pinned(factory, schema)
            try:
                await acquire_model_definition_lock(holder, model_id)
                writer = await _pinned(factory, schema)
                try:
                    with pytest.raises(HTTPException) as excinfo:
                        await call(writer)
                    assert excinfo.value.status_code == 503, (
                        name + " did not serialise against a held revert lock - "
                        "it returned " + str(excinfo.value.status_code) + ". Its "
                        "write can be silently discarded by the revert "
                        "(Bug-8437 / Bug-8441)."
                    )
                finally:
                    await writer.rollback()
                    await writer.close()
            finally:
                await holder.rollback()
                await holder.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_a_revert_and_a_concurrent_model_rename_cannot_interleave(monkeypatch):
    """Bug-8437 stated as the user outcome, not as a lock call.

    A modeller renames a model while a revert restores that same row's scalars
    from the snapshot. Before this lane the rename ran unlocked, so the two
    UPDATEs raced and one was silently discarded with no error and no audit
    trail. The rename must now WAIT for the revert's transaction, and the value
    that survives must be one someone actually asked for - never a mix.
    """
    from fastapi import HTTPException

    import src.api.models as models_api
    from shared.config.settings import get_settings
    from shared.db.models import Model as _Model
    from shared.schemas.pydantic_models import ModelUpdate
    from src.auth.middleware import CurrentUser

    monkeypatch.setattr(
        get_settings(), "MODEL_DEFINITION_LOCK_TIMEOUT_SECONDS", 1, raising=False
    )

    async with _isolated_schema() as (factory, schema):
        setup = await _pinned(factory, schema)
        try:
            model_id = await _seed_model(setup)
            project_id = (await setup.get(_Model, model_id)).project_id
            await setup.commit()
        finally:
            await setup.close()

        # The revert: hold the lock and write the model scalars, uncommitted.
        revert = await _pinned(factory, schema)
        try:
            await acquire_model_definition_lock(revert, model_id)
            await revert.execute(
                update(_Model).where(_Model.id == model_id)
                .values(display_name="restored-from-snapshot")
            )

            renamer = await _pinned(factory, schema)
            try:
                _bind_handler_module(monkeypatch, models_api, renamer)
                with pytest.raises(HTTPException) as excinfo:
                    await models_api.update_model(
                        project_id, model_id,
                        ModelUpdate(display_name="renamed-by-modeller"),
                        current_user=CurrentUser(
                            user_id="u@test", tenant_id="t", email="u@test",
                        ),
                    )
                assert excinfo.value.status_code == 503, (
                    "the rename did not wait for the revert; it can silently "
                    "discard the restored value (Bug-8437)"
                )
            finally:
                await renamer.rollback()
                await renamer.close()

            await revert.commit()
        finally:
            await revert.close()

        check = await _pinned(factory, schema)
        try:
            surviving = (await check.get(_Model, model_id)).display_name
        finally:
            await check.close()
        assert surviving == "restored-from-snapshot", (
            "the revert's restored value did not survive; the two writers "
            "interleaved and left " + repr(surviving)
        )
