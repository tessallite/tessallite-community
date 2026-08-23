"""Real-Postgres behaviour of the body-parameter foreign-key scope primitives.

The companion file ``tests/test_scope_body_fk_helpers.py`` proves the SQL shape
and the fail-closed decisions without a database. It cannot prove the thing that
matters most, because a fake session does not evaluate a WHERE clause: that a
foreign row is REALLY rejected and an in-scope row is REALLY accepted. An
accept-everything guard and a deny-everything guard are both defects, and only a
real query distinguishes them (Bug-8864 was a deny-everything guard that unit
tests waved through).

Fixture layout — two projects, three models, so every hop of every helper has a
near-miss to be wrong about:

    project_a ── model_a  ── source_a  ── calendar_a, table_a(column_a, column_a_other)
             │            ├─ measure_a, dimension_a
             └─ model_a2 ── source_a2 ── calendar_a2, table_a2(column_a2)
                          ├─ measure_a2, dimension_a2     (same project, other model)
    project_b ── model_b  ── source_b  ── calendar_b, table_b(column_b)
                          ├─ measure_b, dimension_b       (other project entirely)

Skipped unless ``TESSALLITE_VERSIONING_DB_URL`` (or the importer-harness URL)
points at a reachable Postgres.

Run:
    cd tessallite/services/model-service
    TESSALLITE_VERSIONING_DB_URL=postgresql+asyncpg://user:pw@localhost:5432/db \
      pytest tests/integration/test_scope_body_fk_db.py -v
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from fastapi import HTTPException

from shared.db.models import (
    CalendarTable,
    DataSource,
    Dimension,
    HierarchyDefinition,
    HierarchyLevel,
    HierarchyLevelAttribute,
    LLMProviderConfig,
    Measure,
    Model,
    ModelColumn,
    ModelTable,
    Project,
    ProjectConnection,
)
from src.api._scope import (
    ensure_calendar_table_in_model,
    ensure_ref_in_model,
    ensure_ref_in_project,
    ensure_refs_in_model,
    ensure_target_in_model,
)

# Reuse the isolated-schema fixture rather than standing up a second one.
from tests.integration.test_versioning_consistency_db import (  # noqa: E402
    _DB_URL,
    _isolated_schema,
)

pytestmark = [pytest.mark.integration]

_GLOSSARY_TARGETS = {
    "dimension": Dimension,
    "measure": Measure,
    "column": ModelColumn,
    "concept": None,
}


@dataclass
class _Fixture:
    project_a: uuid.UUID
    project_b: uuid.UUID
    model_a: uuid.UUID
    model_a2: uuid.UUID
    model_b: uuid.UUID
    measure_a: uuid.UUID
    measure_a2: uuid.UUID
    measure_b: uuid.UUID
    dimension_a: uuid.UUID
    dimension_a2: uuid.UUID
    dimension_b: uuid.UUID
    table_a: uuid.UUID
    table_a2: uuid.UUID
    table_b: uuid.UUID
    column_a: uuid.UUID
    column_a_other: uuid.UUID
    column_a2: uuid.UUID
    column_b: uuid.UUID
    hierarchy_a: uuid.UUID
    hierarchy_b: uuid.UUID
    level_a: uuid.UUID
    level_b: uuid.UUID
    level_attr_a: uuid.UUID
    level_attr_b: uuid.UUID
    calendar_a: uuid.UUID
    calendar_a2: uuid.UUID
    calendar_b: uuid.UUID
    llm_a: uuid.UUID
    llm_b: uuid.UUID


async def _seed(session) -> _Fixture:
    ids = {}

    def _new(key):
        ids[key] = uuid.uuid4()
        return ids[key]

    project_a, project_b = _new("project_a"), _new("project_b")
    for pid in (project_a, project_b):
        session.add(
            Project(id=pid, slug=f"p-{pid.hex[:8]}", display_name="P")
        )

    conns = {}
    for pid in (project_a, project_b):
        cid = uuid.uuid4()
        conns[pid] = cid
        session.add(
            ProjectConnection(
                id=cid,
                project_id=pid,
                display_name="C",
                connection_type="postgresql",
                encrypted_credentials=b"x",
                config={},
            )
        )

    def _add_model(key, project_id):
        mid = _new(key)
        session.add(
            Model(
                id=mid,
                project_id=project_id,
                slug=f"m-{mid.hex[:8]}",
                display_name="M",
                seed=uuid.uuid4().hex,
            )
        )
        return mid

    model_a = _add_model("model_a", project_a)
    model_a2 = _add_model("model_a2", project_a)
    model_b = _add_model("model_b", project_b)

    sources = {}
    for mid, pid in ((model_a, project_a), (model_a2, project_a), (model_b, project_b)):
        sid = uuid.uuid4()
        sources[mid] = sid
        session.add(
            DataSource(
                id=sid,
                model_id=mid,
                project_connection_id=conns[pid],
                source_type="postgresql",
                display_name="S",
                config={},
            )
        )

    def _add_calendar(key, model_id):
        cal_id = _new(key)
        session.add(
            CalendarTable(
                id=cal_id,
                data_source_id=sources[model_id],
                table_name=f"cal_{cal_id.hex[:6]}",
                dialect="postgresql",
                date_column="d",
            )
        )
        return cal_id

    _add_calendar("calendar_a", model_a)
    _add_calendar("calendar_a2", model_a2)
    _add_calendar("calendar_b", model_b)

    def _add_entities(model_id, measure_key, dim_key, table_key, column_keys):
        session.add(
            Measure(id=_new(measure_key), model_id=model_id, name=f"m{measure_key}")
        )
        session.add(
            Dimension(id=_new(dim_key), model_id=model_id, name=f"d{dim_key}")
        )
        table_id = _new(table_key)
        session.add(
            ModelTable(
                id=table_id,
                model_id=model_id,
                source_id=sources[model_id],
                table_type="fact",
                physical_name="t",
                alias=f"t{table_id.hex[:6]}",
                display_name="T",
            )
        )
        hier_key = f"hierarchy_{measure_key.split('_')[-1]}"
        level_key = f"level_{measure_key.split('_')[-1]}"
        if hier_key in ("hierarchy_a", "hierarchy_b"):
            hier_id = _new(hier_key)
            session.add(
                HierarchyDefinition(
                    id=hier_id, model_id=model_id, name=f"h{hier_id.hex[:6]}",
                    type="explicit",
                )
            )
            level_id = _new(level_key)
            session.add(
                HierarchyLevel(
                    id=level_id, hierarchy_id=hier_id, name="L1",
                    ordinal=0, key_attribute_id=uuid.uuid4(),
                    key_attribute_source="dimension",
                )
            )
            session.add(
                HierarchyLevelAttribute(
                    id=_new(f"level_attr_{measure_key.split('_')[-1]}"),
                    level_id=level_id, attribute_id=uuid.uuid4(),
                    attribute_source="dimension", role="display",
                )
            )
        for key in column_keys:
            session.add(
                ModelColumn(
                    id=_new(key),
                    model_table_id=table_id,
                    column_name=f"c{key}",
                    data_type="text",
                )
            )

    _add_entities(
        model_a, "measure_a", "dimension_a", "table_a",
        ["column_a", "column_a_other"],
    )
    _add_entities(model_a2, "measure_a2", "dimension_a2", "table_a2", ["column_a2"])
    _add_entities(model_b, "measure_b", "dimension_b", "table_b", ["column_b"])

    # Project-OWNED entity for the ensure_ref_in_project family. One per
    # project, so a foreign reference has a real row to point at rather than a
    # dangling id — "a row that exists in another project" is the case a
    # get-then-forget-to-compare guard waves through.
    #
    # Flushed separately: the unit-of-work insert sort does not reliably place
    # llm_provider_configs after projects in this batch, and the resulting
    # ForeignKeyViolation is a fixture defect, not a finding.
    await session.flush()
    for key, pid in (("llm_a", project_a), ("llm_b", project_b)):
        session.add(
            LLMProviderConfig(
                id=_new(key),
                project_id=pid,
                provider="anthropic",
                display_name=f"llm-{key}",
                model_name="claude",
            )
        )

    await session.flush()
    await session.commit()
    return _Fixture(**ids)


# ---------------------------------------------------------------------------
# ensure_ref_in_model
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_single_ref_accepts_own_and_rejects_foreign_measure():
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            got = await ensure_ref_in_model(
                db, Measure, ref_id=f.measure_a, model_id=f.model_a,
                project_id=f.project_a,
                field_name="variant_of_measure_id",
            )
            assert got.id == f.measure_a

            with pytest.raises(HTTPException) as exc:
                await ensure_ref_in_model(
                    db, Measure, ref_id=f.measure_b, model_id=f.model_a,
                    project_id=f.project_a,
                    field_name="variant_of_measure_id",
                )
            assert exc.value.status_code == 422
            assert exc.value.detail["error_code"] == "REF_NOT_IN_MODEL"


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_single_ref_rejects_a_sibling_model_in_the_same_project():
    """Same project is not the same model — the guard is model-scoped."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            with pytest.raises(HTTPException):
                await ensure_ref_in_model(
                    db, Measure, ref_id=f.measure_a, model_id=f.model_a2,
                    project_id=f.project_a,
                    field_name="variant_of_measure_id",
                )


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_single_ref_rejects_when_the_project_hop_does_not_match():
    """The row and the model line up, but the model does not sit in the project
    the caller claims. A caller that skipped ensure_model_in_project — or that
    passed a body-supplied model_id — must not get an accept."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            with pytest.raises(HTTPException):
                await ensure_ref_in_model(
                    db, Measure, ref_id=f.measure_a, model_id=f.model_a,
                    project_id=f.project_b, field_name="measure_id",
                )


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_single_ref_reports_foreign_and_nonexistent_identically():
    """Anti-oracle: the response must not confirm that a row exists elsewhere
    in the tenant. Only the echoed (caller-supplied) id may differ."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            with pytest.raises(HTTPException) as foreign:
                await ensure_ref_in_model(
                    db, Measure, ref_id=f.measure_b, model_id=f.model_a,
                    project_id=f.project_a,
                    field_name="measure_id",
                )
            with pytest.raises(HTTPException) as unknown:
                await ensure_ref_in_model(
                    db, Measure, ref_id=uuid.uuid4(), model_id=f.model_a,
                    project_id=f.project_a,
                    field_name="measure_id",
                )

            assert foreign.value.status_code == unknown.value.status_code
            a = dict(foreign.value.detail)
            b = dict(unknown.value.detail)
            a.pop("ids"), b.pop("ids")
            a.pop("message"), b.pop("message")
            assert a == b


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_single_ref_scopes_a_column_through_its_table():
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            got = await ensure_ref_in_model(
                db, ModelColumn, ref_id=f.column_a, model_id=f.model_a,
                project_id=f.project_a,
                field_name="source_column_id",
            )
            assert got.id == f.column_a

            with pytest.raises(HTTPException):
                await ensure_ref_in_model(
                    db, ModelColumn, ref_id=f.column_b, model_id=f.model_a,
                    project_id=f.project_a,
                    field_name="source_column_id",
                )


# ---------------------------------------------------------------------------
# ensure_refs_in_model
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_single_ref_handles_an_entity_with_more_than_one_model_shaped_fk():
    """ModelTable carries both ``model_id`` and ``source_id``; Measure carries
    ``model_id`` and ``cross_model_source_model_id``. The generic branch joins
    Model on an explicit onclause, so an ambiguous-join regression would show up
    here as an error rather than as a silently wrong scope."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            got = await ensure_ref_in_model(
                db, ModelTable, ref_id=f.table_a, model_id=f.model_a,
                project_id=f.project_a, field_name="left_table_id",
            )
            assert got.id == f.table_a

            for foreign in (f.table_a2, f.table_b):
                with pytest.raises(HTTPException):
                    await ensure_ref_in_model(
                        db, ModelTable, ref_id=foreign, model_id=f.model_a,
                        project_id=f.project_a, field_name="left_table_id",
                    )


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_single_ref_rejects_a_column_of_a_sibling_model():
    """The column hop, isolated from the project hop: column_a2 lives in the
    caller's own PROJECT but in a different model. Only the
    ModelTable.model_id predicate can reject it."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            with pytest.raises(HTTPException):
                await ensure_ref_in_model(
                    db, ModelColumn, ref_id=f.column_a2, model_id=f.model_a,
                    project_id=f.project_a, field_name="source_column_id",
                )


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_list_accepts_every_in_scope_column():
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            rows = await ensure_refs_in_model(
                db, ModelColumn, ref_ids=[f.column_a, f.column_a_other],
                model_id=f.model_a, project_id=f.project_a, field_name="column_ids",
            )

            assert {r.id for r in rows} == {f.column_a, f.column_a_other}


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_list_rejects_the_whole_request_and_names_the_foreign_id():
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            with pytest.raises(HTTPException) as exc:
                await ensure_refs_in_model(
                    db, ModelColumn, ref_ids=[f.column_a, f.column_b],
                    model_id=f.model_a, project_id=f.project_a, field_name="column_ids",
                )

            assert exc.value.status_code == 422
            assert exc.value.detail["ids"] == [str(f.column_b)]


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_list_rejects_a_column_of_a_sibling_model():
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            with pytest.raises(HTTPException) as exc:
                await ensure_refs_in_model(
                    db, ModelColumn, ref_ids=[f.column_a, f.column_a2],
                    model_id=f.model_a, project_id=f.project_a,
                    field_name="column_ids",
                )

            assert exc.value.detail["ids"] == [str(f.column_a2)]


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_list_rejects_when_the_project_hop_does_not_match():
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            with pytest.raises(HTTPException):
                await ensure_refs_in_model(
                    db, ModelColumn, ref_ids=[f.column_a],
                    model_id=f.model_a, project_id=f.project_b,
                    field_name="column_ids",
                )


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_single_ref_walks_a_declared_parent_chain():
    """HierarchyLevel -> HierarchyDefinition -> Model -> project. The chain
    walker is generic, so an entity scoped through a DECLARED parent (not a
    hand-written branch) has to be proved against real SQL, not only
    compiled."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            got = await ensure_ref_in_model(
                db, HierarchyLevel, ref_id=f.level_a, model_id=f.model_a,
                project_id=f.project_a, field_name="level_id",
            )
            assert got.id == f.level_a

            with pytest.raises(HTTPException):
                await ensure_ref_in_model(
                    db, HierarchyLevel, ref_id=f.level_b, model_id=f.model_a,
                    project_id=f.project_a, field_name="level_id",
                )

            with pytest.raises(HTTPException):
                await ensure_ref_in_model(
                    db, HierarchyLevel, ref_id=f.level_a, model_id=f.model_a,
                    project_id=f.project_b, field_name="level_id",
                )


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_single_ref_walks_a_multi_hop_ownership_chain():
    """HierarchyLevelAttribute -> HierarchyLevel -> HierarchyDefinition ->
    Model -> project: three declared joins before the model predicate. A
    walker that stopped after one hop would either mis-scope or fail to
    resolve, and only a genuinely multi-hop entity catches that."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            got = await ensure_ref_in_model(
                db, HierarchyLevelAttribute, ref_id=f.level_attr_a,
                model_id=f.model_a, project_id=f.project_a,
                field_name="level_attribute_id",
            )
            assert got.id == f.level_attr_a

            with pytest.raises(HTTPException):
                await ensure_ref_in_model(
                    db, HierarchyLevelAttribute, ref_id=f.level_attr_b,
                    model_id=f.model_a, project_id=f.project_a,
                    field_name="level_attribute_id",
                )


# ---------------------------------------------------------------------------
# ensure_target_in_model
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_polymorphic_target_accepts_own_and_rejects_foreign():
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            for target_type, own, foreign in (
                ("dimension", f.dimension_a, f.dimension_b),
                ("measure", f.measure_a, f.measure_b),
                ("column", f.column_a, f.column_b),
            ):
                got = await ensure_target_in_model(
                    db, target_type=target_type, target_id=own,
                    model_id=f.model_a, project_id=f.project_a,
                    allowed_targets=_GLOSSARY_TARGETS,
                )
                assert got.id == own

                with pytest.raises(HTTPException) as exc:
                    await ensure_target_in_model(
                        db, target_type=target_type, target_id=foreign,
                        model_id=f.model_a, project_id=f.project_a,
                        allowed_targets=_GLOSSARY_TARGETS,
                    )
                assert exc.value.detail["error_code"] == "TARGET_NOT_IN_MODEL"


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_polymorphic_target_cannot_be_cross_typed():
    """A dimension id declared as a measure must not resolve, even though both
    rows live in the caller's own model."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            with pytest.raises(HTTPException):
                await ensure_target_in_model(
                    db, target_type="measure", target_id=f.dimension_a,
                    model_id=f.model_a, project_id=f.project_a,
                    allowed_targets=_GLOSSARY_TARGETS,
                )


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_polymorphic_target_rejects_a_sibling_model_in_the_same_project():
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            for target_type, sibling in (
                ("dimension", f.dimension_a2),
                ("measure", f.measure_a2),
                ("column", f.column_a2),
            ):
                with pytest.raises(HTTPException):
                    await ensure_target_in_model(
                        db, target_type=target_type, target_id=sibling,
                        model_id=f.model_a, project_id=f.project_a,
                        allowed_targets=_GLOSSARY_TARGETS,
                    )


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_polymorphic_target_rejects_when_the_project_hop_does_not_match():
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            with pytest.raises(HTTPException):
                await ensure_target_in_model(
                    db, target_type="dimension", target_id=f.dimension_a,
                    model_id=f.model_a, project_id=f.project_b,
                    allowed_targets=_GLOSSARY_TARGETS,
                )


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_polymorphic_target_reports_foreign_and_nonexistent_identically():
    """Anti-oracle at the DB level for the polymorphic helper — the unit-level
    version cannot distinguish an oracle from its absence, because the fake
    session returns no rows for both branches regardless."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            with pytest.raises(HTTPException) as foreign:
                await ensure_target_in_model(
                    db, target_type="dimension", target_id=f.dimension_b,
                    model_id=f.model_a, project_id=f.project_a,
                    allowed_targets=_GLOSSARY_TARGETS,
                )
            with pytest.raises(HTTPException) as unknown:
                await ensure_target_in_model(
                    db, target_type="dimension", target_id=uuid.uuid4(),
                    model_id=f.model_a, project_id=f.project_a,
                    allowed_targets=_GLOSSARY_TARGETS,
                )

            assert foreign.value.status_code == unknown.value.status_code
            a, b = dict(foreign.value.detail), dict(unknown.value.detail)
            a.pop("ids"), b.pop("ids")
            a.pop("message"), b.pop("message")
            assert a == b


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_list_reports_foreign_and_nonexistent_identically():
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            with pytest.raises(HTTPException) as foreign:
                await ensure_refs_in_model(
                    db, ModelColumn, ref_ids=[f.column_b],
                    model_id=f.model_a, project_id=f.project_a,
                    field_name="column_ids",
                )
            with pytest.raises(HTTPException) as unknown:
                await ensure_refs_in_model(
                    db, ModelColumn, ref_ids=[uuid.uuid4()],
                    model_id=f.model_a, project_id=f.project_a,
                    field_name="column_ids",
                )

            assert foreign.value.status_code == unknown.value.status_code
            a, b = dict(foreign.value.detail), dict(unknown.value.detail)
            a.pop("ids"), b.pop("ids")
            a.pop("message"), b.pop("message")
            assert a == b


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_list_batches_a_collection_larger_than_one_query_can_bind():
    """An oversized column_ids array must come back as a 422 naming the
    offenders, never as a driver-level 500 from an over-long IN clause."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            padding = [uuid.uuid4() for _ in range(2500)]

            rows = await ensure_refs_in_model(
                db, ModelColumn, ref_ids=[f.column_a, f.column_a_other],
                model_id=f.model_a, project_id=f.project_a,
                field_name="column_ids",
            )
            assert {r.id for r in rows} == {f.column_a, f.column_a_other}

            with pytest.raises(HTTPException) as exc:
                await ensure_refs_in_model(
                    db, ModelColumn, ref_ids=[f.column_a] + padding,
                    model_id=f.model_a, project_id=f.project_a,
                    field_name="column_ids",
                )
            assert exc.value.status_code == 422
            assert len(exc.value.detail["ids"]) == 20


# ---------------------------------------------------------------------------
# ensure_calendar_table_in_model — the two-hop variant
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_calendar_accepts_a_calendar_reached_through_its_own_source():
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            got = await ensure_calendar_table_in_model(
                db, calendar_table_id=f.calendar_a, model_id=f.model_a,
                project_id=f.project_a,
            )

            assert got.id == f.calendar_a


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_calendar_rejects_a_calendar_of_a_sibling_model():
    """Hop 2: the calendar's DataSource belongs to another model in the SAME
    project. A guard that only checked the project would let this through."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            with pytest.raises(HTTPException) as exc:
                await ensure_calendar_table_in_model(
                    db, calendar_table_id=f.calendar_a2, model_id=f.model_a,
                    project_id=f.project_a,
                )

            assert exc.value.detail["error_code"] == "CALENDAR_TABLE_NOT_IN_MODEL"


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_calendar_rejects_a_calendar_of_another_project():
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            with pytest.raises(HTTPException):
                await ensure_calendar_table_in_model(
                    db, calendar_table_id=f.calendar_b, model_id=f.model_a,
                    project_id=f.project_a,
                )


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_calendar_rejects_when_the_project_hop_does_not_match():
    """Hop 3: the calendar and the model line up, but the model does not sit in
    the project the caller claims. A caller that skipped
    ensure_model_in_project must not get an accept out of this helper."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            with pytest.raises(HTTPException):
                await ensure_calendar_table_in_model(
                    db, calendar_table_id=f.calendar_a, model_id=f.model_a,
                    project_id=f.project_b,
                )


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_calendar_reports_foreign_and_nonexistent_identically():
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            with pytest.raises(HTTPException) as foreign:
                await ensure_calendar_table_in_model(
                    db, calendar_table_id=f.calendar_b, model_id=f.model_a,
                    project_id=f.project_a,
                )
            with pytest.raises(HTTPException) as unknown:
                await ensure_calendar_table_in_model(
                    db, calendar_table_id=uuid.uuid4(), model_id=f.model_a,
                    project_id=f.project_a,
                )

            assert foreign.value.status_code == unknown.value.status_code
            a, b = dict(foreign.value.detail), dict(unknown.value.detail)
            a.pop("ids"), b.pop("ids")
            a.pop("message"), b.pop("message")
            assert a == b


# ---------------------------------------------------------------------------
# ensure_ref_in_project
#
# LLMProviderConfig has no model_id: it is owned by the PROJECT and carries a
# Fernet-encrypted provider API key plus a base_url. A foreign reference binds
# one project's scheduled AI work to ANOTHER project's provider account.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_project_ref_accepts_own_and_rejects_foreign_llm_config():
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            got = await ensure_ref_in_project(
                db, LLMProviderConfig, ref_id=f.llm_a,
                project_id=f.project_a, field_name="llm_config_id",
            )
            assert got.id == f.llm_a

            with pytest.raises(HTTPException) as exc:
                await ensure_ref_in_project(
                    db, LLMProviderConfig, ref_id=f.llm_b,
                    project_id=f.project_a, field_name="llm_config_id",
                )
            assert exc.value.status_code == 422
            assert exc.value.detail["error_code"] == "REF_NOT_IN_PROJECT"
            assert exc.value.detail["ids"] == [str(f.llm_b)]


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_project_ref_is_project_scoped_not_model_scoped():
    """The documented LIMIT, pinned by a test rather than left in prose: this
    guard proves PROJECT containment only, so one config serves every model in
    the project. A call site that needs model containment must use
    ensure_ref_in_model instead. If this assertion ever has to change, that is
    a contract change and not a test fix."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            # model_a and model_a2 are two different models in project_a. The
            # helper takes no model argument at all — that is the point.
            assert f.model_a != f.model_a2
            got = await ensure_ref_in_project(
                db, LLMProviderConfig, ref_id=f.llm_a,
                project_id=f.project_a, field_name="llm_config_id",
            )
            assert got.id == f.llm_a


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_project_ref_reports_foreign_and_nonexistent_identically():
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            with pytest.raises(HTTPException) as foreign:
                await ensure_ref_in_project(
                    db, LLMProviderConfig, ref_id=f.llm_b,
                    project_id=f.project_a, field_name="llm_config_id",
                )
            with pytest.raises(HTTPException) as unknown:
                await ensure_ref_in_project(
                    db, LLMProviderConfig, ref_id=uuid.uuid4(),
                    project_id=f.project_a, field_name="llm_config_id",
                )

            assert foreign.value.status_code == unknown.value.status_code
            a, b = dict(foreign.value.detail), dict(unknown.value.detail)
            a.pop("ids"), b.pop("ids")
            a.pop("message"), b.pop("message")
            assert a == b
