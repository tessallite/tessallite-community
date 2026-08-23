"""DB-backed importer rehydration round-trip (F-020-E2).

Each ecosystem importer snapshot is rehydrated through the real
``prepare_snapshot_for_import`` + ``rehydrate_into_live`` path against a
throwaway Postgres schema, proving the importers emit ORM-aligned snapshots
that land as live rows. Skipped unless TESSALLITE_IMPORTER_REHYDRATION_DB_URL
points at a reachable Postgres.

Run:
    cd tessallite/services/model-service
    TESSALLITE_IMPORTER_REHYDRATION_DB_URL=postgresql+asyncpg://user:pw@localhost:5432/db \
      pytest tests/integration/test_importer_rehydration_harness.py -v
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from shared.db.models import (
    AggregateRefreshPolicy,
    CalendarTable,
    DataQualityRule,
    DataSource,
    Dimension,
    Join,
    KPI,
    Measure,
    Model,
    ModelColumn,
    ModelTable,
    Project,
    ProjectConnection,
    TenantBase,
)
from shared.model_snapshot.importer import prepare_snapshot_for_import
from shared.model_snapshot.rehydrator import (
    RehydrationMode,
    SnapshotSchemaError,
    rehydrate_into_live,
)
from tests.importer_rehydration_harness import (
    ImporterRehydrationCase,
    importer_rehydration_cases,
)

pytestmark = [pytest.mark.integration]

DB_URL_ENV = "TESSALLITE_IMPORTER_REHYDRATION_DB_URL"


@asynccontextmanager
async def _isolated_schema_session(db_url: str) -> AsyncIterator[AsyncSession]:
    schema = f"importer_rehydration_{uuid.uuid4().hex}"
    engine = create_async_engine(db_url, future=True)

    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'SET search_path TO "{schema}"'))
        await conn.run_sync(TenantBase.metadata.create_all)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_search_path(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f'SET search_path TO "{schema}"')
        cursor.close()

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            yield session
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def _seed_project(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    project_id = uuid.uuid4()
    connection_id = uuid.uuid4()
    session.add(
        Project(
            id=project_id,
            slug=f"importer-harness-{project_id.hex[:8]}",
            display_name="Importer Harness",
        )
    )
    session.add(
        ProjectConnection(
            id=connection_id,
            project_id=project_id,
            display_name="Harness Source",
            connection_type="postgresql",
            encrypted_credentials=b"test",
            config={},
        )
    )
    await session.flush()
    return project_id, connection_id


async def _rehydrate_case(
    session: AsyncSession,
    case: ImporterRehydrationCase,
    project_id: uuid.UUID,
    connection_id: uuid.UUID,
) -> uuid.UUID:
    snapshot = case.build_snapshot()
    if case.inject_project_connection:
        for source in snapshot.get("data_sources", []) or []:
            source.setdefault("project_connection_id", str(connection_id))

    new_model_id = uuid.uuid4()
    rewritten, _missing = prepare_snapshot_for_import(
        snapshot,
        new_model_id=new_model_id,
    )
    slug = rewritten.get("model", {}).get("slug") or case.name
    session.add(
        Model(
            id=new_model_id,
            project_id=project_id,
            slug=slug,
            display_name=rewritten.get("model", {}).get("display_name") or slug,
            seed=str(uuid.uuid4()),
        )
    )
    await session.flush()

    await rehydrate_into_live(
        new_model_id,
        rewritten,
        session,
        drop_orphan_aggregates=False,
        actor="importer-rehydration-harness",
        force_aggregate_pending=True,
        force_pocket_stale=True,
    )
    await session.flush()
    return new_model_id


async def _count(session: AsyncSession, model_cls, *criteria) -> int:
    result = await session.execute(
        select(func.count()).select_from(model_cls).where(*criteria)
    )
    return int(result.scalar_one())


async def test_bug_8702_pre_0194_bundle_persists_declared_join_orientation():
    """A legacy-shaped bundle lands as the same declaration repaired by 0194.

    This runs the production import rewrite and rehydrator against PostgreSQL,
    then reads the actual live join row.  A raw ``many_to_one`` orientation
    would make pocket population proof refuse the imported edge indefinitely.
    """
    db_url = os.environ.get(DB_URL_ENV)
    if not db_url:
        pytest.skip(f"Set {DB_URL_ENV} to run DB-backed importer rehydration tests")

    async with _isolated_schema_session(db_url) as session:
        project_id, connection_id = await _seed_project(session)
        case = next(
            item for item in importer_rehydration_cases()
            if item.name == "yaml-roundtrip"
        )
        snapshot = case.build_snapshot()
        source_connection_id = str(uuid.uuid4())
        snapshot["data_sources"][0]["project_connection_id"] = source_connection_id
        legacy_join = snapshot["joins"][0]
        legacy_join["join_type"] = "many_to_one"
        # Hand-edited bundles bypass the API enum. Migration 0194 treats an
        # invalid non-null cardinality as unresolved and recovers fan-out from
        # the legacy join token; the post-migration import boundary must match.
        legacy_join["cardinality"] = "not_a_cardinality"

        new_model_id = uuid.uuid4()
        rewritten, missing = prepare_snapshot_for_import(
            snapshot,
            new_model_id=new_model_id,
            connection_mapping={source_connection_id: str(connection_id)},
        )
        assert missing == []
        session.add(
            Model(
                id=new_model_id,
                project_id=project_id,
                slug=rewritten["model"]["slug"],
                display_name=rewritten["model"]["display_name"],
                seed=str(uuid.uuid4()),
            )
        )
        await session.flush()

        await rehydrate_into_live(
            new_model_id,
            rewritten,
            session,
            drop_orphan_aggregates=False,
            actor="bug-8702-import-test",
            preserve_destination_seed=True,
        )
        await session.commit()

        result = await session.execute(
            select(Join.join_type, Join.cardinality).where(
                Join.model_id == new_model_id
            )
        )
        assert result.one() == ("left", "many_to_one")


_OMITTED = object()


def _snapshot_with_refresh_policy(raw_value: object = _OMITTED) -> tuple[dict, str, str]:
    """Build an importer-shaped snapshot carrying one refresh-policy value.

    The YAML importer supplies the normal model graph; this adds the target and
    aggregate rows needed to reach the policy INSERT in the real rehydrator.
    ``raw_value`` deliberately stays untyped so this exercises the JSON bundle
    boundary rather than a Pydantic model.
    """
    yaml_case = next(
        case for case in importer_rehydration_cases() if case.name == "yaml-roundtrip"
    )
    snapshot = yaml_case.build_snapshot()
    model_id = snapshot["model"]["id"]
    connection_id = str(uuid.uuid4())
    snapshot["data_sources"][0]["project_connection_id"] = connection_id

    target_id = str(uuid.uuid4())
    snapshot["data_targets"] = [
        {
            "id": target_id,
            "model_id": model_id,
            "project_connection_id": connection_id,
            "target_type": "postgresql",
            "display_name": "Import Harness Target",
            "config": {},
        }
    ]
    aggregate_id = str(uuid.uuid4())
    policy = {
        "id": str(uuid.uuid4()),
        "aggregate_definition_id": aggregate_id,
        "refresh_mode": "incremental",
        "cron_expression": "0 2 * * *",
        "incremental_column": "business_date",
        "incremental_lookback": 7,
        "is_enabled": True,
    }
    if raw_value is not _OMITTED:
        policy["incremental_append_only"] = raw_value
    snapshot["aggregates"] = [
        {
            "id": aggregate_id,
            "model_id": model_id,
            "target_id": target_id,
            "physical_table_name": "agg_source_seed_policy",
            "target_schema": "public",
            "status": "active",
            "grain": [],
            "columns": [],
            "refresh_policy": policy,
        }
    ]
    return snapshot, connection_id, aggregate_id


@pytest.mark.parametrize(
    ("raw_value", "valid", "expected"),
    [
        pytest.param(1, False, None, id="numeric-one-rejected"),
        pytest.param(0, False, None, id="numeric-zero-rejected"),
        pytest.param("true", False, None, id="string-true-rejected"),
        pytest.param("false", False, None, id="string-false-rejected"),
        pytest.param(None, False, None, id="explicit-null-rejected"),
        pytest.param(_OMITTED, True, False, id="omitted-legacy-false"),
        pytest.param(False, True, False, id="canonical-false"),
        # Bug-8768 (Adjustment 4): on IMPORT (force_aggregate_pending=True) even a
        # canonical-true declaration is RESET to false — the append-only assertion
        # is about one specific source and the imported model may point at data
        # that behaves differently. It validates strictly (a non-bool still
        # raises) and is then disabled pending review.
        pytest.param(True, True, False, id="canonical-true-reset-on-import"),
    ],
)
async def test_snapshot_import_rehydrates_strict_append_only_boolean(
    raw_value: object,
    valid: bool,
    expected: bool | None,
):
    """Bug-8768: the real import path must not coerce JSON values to Boolean, and
    (Adjustment 4) must reset an imported append-only declaration to false.

    This uses the same prepare/rekey + rehydrate sequence as the model-service
    import endpoints and a real PostgreSQL schema. Invalid values must fail
    before the policy INSERT; omitted legacy snapshots retain a durable False;
    and a valid true value validates strictly then lands as false because import
    (force_aggregate_pending=True) disables incremental pending review.
    """
    db_url = os.environ.get(DB_URL_ENV)
    if not db_url:
        pytest.skip(f"Set {DB_URL_ENV} to run DB-backed importer rehydration tests")

    async with _isolated_schema_session(db_url) as session:
        project_id, connection_id = await _seed_project(session)
        snapshot, source_connection_id, _ = _snapshot_with_refresh_policy(raw_value)
        new_model_id = uuid.uuid4()
        rewritten, missing = prepare_snapshot_for_import(
            snapshot,
            new_model_id=new_model_id,
            connection_mapping={source_connection_id: str(connection_id)},
        )
        assert missing == []
        session.add(
            Model(
                id=new_model_id,
                project_id=project_id,
                slug=rewritten["model"]["slug"],
                display_name=rewritten["model"]["display_name"],
                seed=str(uuid.uuid4()),
            )
        )
        await session.flush()
        aggregate_id = uuid.UUID(rewritten["aggregates"][0]["id"])

        if not valid:
            with pytest.raises(
                SnapshotSchemaError,
                match="incremental_append_only must be a JSON boolean",
            ):
                await rehydrate_into_live(
                    new_model_id,
                    rewritten,
                    session,
                    drop_orphan_aggregates=False,
                    actor="bug-8768-import-test",
                    force_aggregate_pending=True,
                    force_pocket_stale=True,
                    preserve_destination_seed=True,
                )
            await session.rollback()
            assert await _count(
                session,
                AggregateRefreshPolicy,
                AggregateRefreshPolicy.aggregate_definition_id == aggregate_id,
            ) == 0
            return

        await rehydrate_into_live(
            new_model_id,
            rewritten,
            session,
            drop_orphan_aggregates=False,
            actor="bug-8768-import-test",
            force_aggregate_pending=True,
            force_pocket_stale=True,
            preserve_destination_seed=True,
        )
        await session.commit()
        result = await session.execute(
            select(AggregateRefreshPolicy.incremental_append_only).where(
                AggregateRefreshPolicy.aggregate_definition_id == aggregate_id
            )
        )
        assert result.scalar_one() is expected


async def _import_append_only_and_read_back(session, *, mode, force_pending):
    """Rehydrate an append-only=true policy and return its persisted value."""
    project_id, connection_id = await _seed_project(session)
    snapshot, source_connection_id, _ = _snapshot_with_refresh_policy(True)
    new_model_id = uuid.uuid4()
    rewritten, missing = prepare_snapshot_for_import(
        snapshot,
        new_model_id=new_model_id,
        connection_mapping={source_connection_id: str(connection_id)},
    )
    assert missing == []
    session.add(
        Model(
            id=new_model_id,
            project_id=project_id,
            slug=rewritten["model"]["slug"],
            display_name=rewritten["model"]["display_name"],
            seed=str(uuid.uuid4()),
        )
    )
    await session.flush()
    aggregate_id = uuid.UUID(rewritten["aggregates"][0]["id"])
    kwargs = {"drop_orphan_aggregates": False, "actor": "bug-8768-r105-test"}
    if mode is not None:
        kwargs["mode"] = mode
    if force_pending:
        kwargs["force_aggregate_pending"] = True
        kwargs["force_pocket_stale"] = True
        kwargs["preserve_destination_seed"] = True
    await rehydrate_into_live(new_model_id, rewritten, session, **kwargs)
    await session.commit()
    return (
        await session.execute(
            select(AggregateRefreshPolicy.incremental_append_only).where(
                AggregateRefreshPolicy.aggregate_definition_id == aggregate_id
            )
        )
    ).scalar_one()


async def test_default_import_mode_resets_append_only_without_force_pending():
    """B8768-R1-05 root cause: the append-only reset is keyed on the rehydration
    MODE (default IMPORT), NOT on ``force_aggregate_pending``. The demo re-seed
    importer omitted the physical-rebind flag, and the OLD code only reset when
    that flag was set — so a true declaration transferred onto a new source.
    With the reset decoupled, a default (import) rehydration resets it even with
    no force-pending flag. Fails before the fix (reset was gated on force_pending)."""
    db_url = os.environ.get(DB_URL_ENV)
    if not db_url:
        pytest.skip(f"Set {DB_URL_ENV} to run DB-backed importer rehydration tests")
    async with _isolated_schema_session(db_url) as session:
        persisted = await _import_append_only_and_read_back(
            session, mode=None, force_pending=False
        )
    assert persisted is False


async def test_restore_mode_preserves_append_only():
    """B8768-R1-05: the single RESTORE caller (version restore, same source)
    preserves a valid append-only declaration."""
    db_url = os.environ.get(DB_URL_ENV)
    if not db_url:
        pytest.skip(f"Set {DB_URL_ENV} to run DB-backed importer rehydration tests")
    async with _isolated_schema_session(db_url) as session:
        persisted = await _import_append_only_and_read_back(
            session, mode=RehydrationMode.RESTORE, force_pending=False
        )
    assert persisted is True


async def _seed_neighbour_model(
    session: AsyncSession, project_id: uuid.UUID, connection_id: uuid.UUID
) -> dict[str, uuid.UUID]:
    """Seed a SECOND model in the same tenant schema and return its row ids.

    This is what makes the cross-model test faithful rather than a constraint
    test. The ids a hand-edited bundle carries are REAL rows that exist in the
    tenant schema and belong to somebody else — every database FK is satisfied,
    so the FK constraint catches nothing and only the rehydrator's membership
    guard stands between the import and a cross-model binding. Pointing at a
    fabricated id would merely prove Postgres enforces its own constraints.
    """
    model = Model(
        id=uuid.uuid4(), project_id=project_id, slug="neighbour-model",
        display_name="Neighbour", seed=str(uuid.uuid4()),
    )
    session.add(model)
    await session.flush()
    source = DataSource(
        id=uuid.uuid4(), model_id=model.id,
        project_connection_id=connection_id, source_type="jdbc",
        display_name="Neighbour Source", config={},
    )
    session.add(source)
    await session.flush()
    calendar = CalendarTable(
        id=uuid.uuid4(), data_source_id=source.id,
        table_name="neighbour_calendar", dialect="postgresql",
    )
    table = ModelTable(
        id=uuid.uuid4(), model_id=model.id, source_id=source.id,
        table_type="fact", physical_name="neighbour_fact",
        alias="neighbour_fact", display_name="Neighbour Fact",
    )
    session.add_all([calendar, table])
    await session.flush()
    column = ModelColumn(
        id=uuid.uuid4(), model_table_id=table.id, column_name="secret_amount",
        data_type="numeric",
    )
    measure = Measure(
        id=uuid.uuid4(), model_id=model.id, name="neighbour_revenue",
    )
    dimension = Dimension(
        id=uuid.uuid4(), model_id=model.id, name="neighbour_date",
        is_time_dim=True,
    )
    session.add_all([column, measure, dimension])
    await session.flush()
    return {
        "model_id": model.id, "calendar_id": calendar.id,
        "column_id": column.id, "measure_id": measure.id,
        "dimension_id": dimension.id,
    }


async def test_bug8950_import_drops_every_cross_model_fk_against_postgres():
    """Bug-8932 / Bug-8950 / L9-F2 through the REAL persistence path.

    The unit guards assert compiled INSERT parameters; they cannot show what
    actually LANDS. This drives ``prepare_snapshot_for_import`` +
    ``rehydrate_into_live`` against real PostgreSQL with a hand-edited bundle
    whose tenant-schema-wide foreign keys all name rows of a DIFFERENT model in
    the same schema, then reads the persisted rows back:

      * ``model_tables.calendar_table_id`` -> NULL (Bug-8932): otherwise the
        imported model dates itself off another model's calendar spine;
      * the ``data_quality_rules`` row is absent (Bug-8950): its ``target_id``
        is NOT NULL and ``data_quality/validator.py`` dereferences it with a
        bare ``db.get`` on a service token, so an imported foreign target reads
        another project's physical table;
      * all four ``kpis`` FKs -> NULL (Bug-8950 + L9-F2): a KPI left pointing
        at another model's measure drags it into target evaluation, dependency
        resolution, lineage and governance export.

    Fails against the pre-guard rehydrator, which persisted every one of them
    verbatim (the database FK constraints are all satisfied by the neighbour
    rows, so nothing else refuses them).
    """
    db_url = os.environ.get(DB_URL_ENV)
    if not db_url:
        pytest.skip(f"Set {DB_URL_ENV} to run DB-backed importer rehydration tests")

    async with _isolated_schema_session(db_url) as session:
        project_id, connection_id = await _seed_project(session)
        foreign = await _seed_neighbour_model(session, project_id, connection_id)

        case = next(
            item for item in importer_rehydration_cases()
            if item.name == "yaml-roundtrip"
        )
        snapshot = case.build_snapshot()
        source_connection_id = str(uuid.uuid4())
        snapshot["data_sources"][0]["project_connection_id"] = source_connection_id

        # The bundle declares no calendar of its own, yet binds a table to one.
        snapshot["calendar_tables"] = []
        snapshot["tables"][0]["calendar_table_id"] = str(foreign["calendar_id"])
        snapshot["data_quality_rules"] = [
            {
                "id": str(uuid.uuid4()),
                "name": "foreign_target_rule",
                "target_type": "column",
                "target_id": str(foreign["column_id"]),
                "rule_type": "not_null",
                "severity": "error",
                "is_enabled": True,
            }
        ]
        snapshot["kpis"] = [
            {
                "id": str(uuid.uuid4()),
                "name": "foreign_fk_kpi",
                "time_dimension_id": str(foreign["dimension_id"]),
                "value_measure_id": str(foreign["measure_id"]),
                "goal_measure_id": str(foreign["measure_id"]),
                "target_measure_id": str(foreign["measure_id"]),
            }
        ]

        new_model_id = uuid.uuid4()
        rewritten, missing = prepare_snapshot_for_import(
            snapshot,
            new_model_id=new_model_id,
            connection_mapping={source_connection_id: str(connection_id)},
        )
        assert missing == []
        # The re-key must not have quietly rewritten the foreign ids into
        # in-model ones; that would make the assertions below vacuous.
        assert rewritten["tables"][0]["calendar_table_id"] == str(
            foreign["calendar_id"]
        )
        assert rewritten["data_quality_rules"][0]["target_id"] == str(
            foreign["column_id"]
        )
        assert rewritten["kpis"][0]["value_measure_id"] == str(
            foreign["measure_id"]
        )

        session.add(
            Model(
                id=new_model_id,
                project_id=project_id,
                slug=rewritten["model"]["slug"],
                display_name=rewritten["model"]["display_name"],
                seed=str(uuid.uuid4()),
            )
        )
        await session.flush()

        await rehydrate_into_live(
            new_model_id,
            rewritten,
            session,
            drop_orphan_aggregates=False,
            actor="bug-8950-cross-model-fk-test",
            preserve_destination_seed=True,
        )
        await session.commit()

        bound = await _count(
            session,
            ModelTable,
            ModelTable.model_id == new_model_id,
            ModelTable.calendar_table_id.isnot(None),
        )
        assert bound == 0, "an imported table kept another model's calendar"

        assert await _count(
            session, DataQualityRule, DataQualityRule.model_id == new_model_id
        ) == 0, "a rule targeting another model's column was imported"

        kpi_row = (
            await session.execute(
                select(
                    KPI.time_dimension_id,
                    KPI.value_measure_id,
                    KPI.goal_measure_id,
                    KPI.target_measure_id,
                ).where(KPI.model_id == new_model_id)
            )
        ).one()
        assert kpi_row == (None, None, None, None), (
            f"imported KPI kept cross-model foreign keys: {kpi_row}"
        )

        # The neighbour model is untouched — the guard drops the reference, it
        # never deletes the referenced row.
        assert await _count(
            session, Measure, Measure.id == foreign["measure_id"]
        ) == 1
        assert await _count(
            session, CalendarTable, CalendarTable.id == foreign["calendar_id"]
        ) == 1


@pytest.mark.parametrize(
    "case",
    importer_rehydration_cases(),
    ids=lambda case: case.name,
)
async def test_importer_snapshot_rehydrates_against_postgres(
    case: ImporterRehydrationCase,
):
    db_url = os.environ.get(DB_URL_ENV)
    if not db_url:
        pytest.skip(f"Set {DB_URL_ENV} to run DB-backed importer rehydration tests")

    async with _isolated_schema_session(db_url) as session:
        project_id, connection_id = await _seed_project(session)

        model_id = await _rehydrate_case(
            session,
            case,
            project_id,
            connection_id,
        )

        assert await _count(session, DataSource, DataSource.model_id == model_id) >= 1
        assert await _count(session, ModelTable, ModelTable.model_id == model_id) >= 1

        table_ids = select(ModelTable.id).where(ModelTable.model_id == model_id)
        assert await _count(
            session,
            ModelColumn,
            ModelColumn.model_table_id.in_(table_ids),
        ) >= 1
        assert (
            await _count(session, Dimension, Dimension.model_id == model_id)
            + await _count(session, Measure, Measure.model_id == model_id)
        ) >= 1
