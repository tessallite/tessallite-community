"""Bug-7901 / Bug-8250 / Bug-8443 — the REAL deployed-definition drift loader.

Exercises ``check_deployed_definition_drift`` end to end against genuine ORM
instances, so what is proven is the whole path a refresh takes: which live rows
get loaded, how they are normalised, and how they diff against a real
``snapshot_json`` written by the real serialiser.

The fake session dispatches on the ENTITY of the SELECT rather than on call
position. Positional harnesses were how the predecessor tests worked, and they
are why extending the guard to load more rows broke every one of them — a
harness that has to be renumbered whenever the query count changes is a harness
that discourages widening the very coverage this bug is about.

Coverage carried over from the deleted scheduler-local tests (they tested a
module-private function that no longer exists):
  * measure property drift (``source_column_id``)
  * transitive calculated-measure dependency drift
  * physical column repoint (same UUID, renamed column)
and the coverage the Codex gate said was MISSING:
  * grain-dimension binding drift
  * join-graph drift
"""
from __future__ import annotations

import uuid

import pytest

from shared.db.models import (
    CalendarTable,
    DataSource,
    Dimension,
    DimensionAttributeRelationship,
    HierarchyDefinition,
    HierarchyLevel,
    HierarchyLevelAttribute,
    Join,
    Measure,
    ModelColumn,
    ModelTable,
    UserDefinedAttribute,
    UserDefinedAttributeColumnRef,
)
from shared.deployed_definition_drift import check_deployed_definition_drift
from shared.model_snapshot.serialiser import row_to_snapshot_dict

pytestmark = pytest.mark.asyncio

MODEL_ID = uuid.uuid4()
VERSION_ID = uuid.uuid4()
TABLE_ID = uuid.uuid4()
TABLE2_ID = uuid.uuid4()
COL_AMOUNT = uuid.uuid4()
COL_COUNTRY = uuid.uuid4()
COL_OTHER = uuid.uuid4()
COL_JOIN_R = uuid.uuid4()
CAL_TABLE_ID = uuid.uuid4()
CAL_MODEL_TABLE_ID = uuid.uuid4()
DATA_SOURCE_ID = uuid.uuid4()
CONNECTION_ID = uuid.uuid4()
REL_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Live-graph builder
# ---------------------------------------------------------------------------

def build_live_graph():
    """A small but complete live model graph, as real ORM instances."""
    tables = [
        ModelTable(id=TABLE_ID, model_id=MODEL_ID, source_id=uuid.uuid4(),
                   table_type="fact", physical_name="fact_sales"),
        ModelTable(id=TABLE2_ID, model_id=MODEL_ID, source_id=uuid.uuid4(),
                   table_type="dimension", physical_name="dim_country"),
        # A calendar-backed table: both refresh writers substitute the TARGET
        # calendar name derived from (calendar_type, fiscal_year_start_month)
        # into the FROM clause for this table.
        ModelTable(id=CAL_MODEL_TABLE_ID, model_id=MODEL_ID,
                   source_id=DATA_SOURCE_ID, table_type="dimension",
                   physical_name="dim_calendar",
                   calendar_table_id=CAL_TABLE_ID),
    ]
    columns = [
        ModelColumn(id=COL_AMOUNT, model_table_id=TABLE_ID,
                    column_name="amount", data_type="numeric"),
        ModelColumn(id=COL_COUNTRY, model_table_id=TABLE_ID,
                    column_name="country_code", data_type="text"),
        ModelColumn(id=COL_OTHER, model_table_id=TABLE_ID,
                    column_name="cost_amount", data_type="numeric"),
        ModelColumn(id=COL_JOIN_R, model_table_id=TABLE2_ID,
                    column_name="code", data_type="text"),
    ]
    measures = [
        Measure(id=uuid.uuid4(), model_id=MODEL_ID, name="revenue",
                source_column_id=COL_AMOUNT, default_agg="sum"),
        Measure(id=uuid.uuid4(), model_id=MODEL_ID, name="cost",
                source_column_id=COL_OTHER, default_agg="sum"),
        Measure(id=uuid.uuid4(), model_id=MODEL_ID, name="profit",
                source_column_id=None, default_agg="sum",
                expression='measure("revenue") - measure("cost")'),
    ]
    dimensions = [
        Dimension(id=uuid.uuid4(), model_id=MODEL_ID, name="country",
                  source_column_id=COL_COUNTRY),
    ]
    joins = [
        Join(id=uuid.uuid4(), model_id=MODEL_ID, left_table_id=TABLE_ID,
             right_table_id=TABLE2_ID, join_type="inner",
             left_column_id=COL_COUNTRY, right_column_id=COL_JOIN_R),
    ]
    calendars = [
        CalendarTable(
            id=CAL_TABLE_ID, data_source_id=DATA_SOURCE_ID,
            table_name="dim_calendar", dialect="postgresql",
            calendar_type="fiscal", fiscal_year_start_month=1,
            autocreated=False,
        ),
    ]
    relationships = [
        DimensionAttributeRelationship(
            id=REL_ID, model_id=MODEL_ID, dimension_id=dimensions[0].id,
            key_column_id=COL_COUNTRY, detail_column_id=COL_JOIN_R,
            cardinality="BIJECTION", null_policy="EXCLUDE", enabled=True,
            declaration_hash="h1",
        ),
    ]
    data_sources = [
        DataSource(
            id=DATA_SOURCE_ID, model_id=MODEL_ID,
            project_connection_id=CONNECTION_ID, source_type="postgresql",
            default_schema="public",
        ),
    ]
    return {
        ModelTable: tables,
        CalendarTable: calendars,
        DimensionAttributeRelationship: relationships,
        DataSource: data_sources,
        ModelColumn: columns,
        Measure: measures,
        Dimension: dimensions,
        Join: joins,
        UserDefinedAttribute: [],
        UserDefinedAttributeColumnRef: [],
        HierarchyDefinition: [],
        HierarchyLevel: [],
        HierarchyLevelAttribute: [],
    }


def snapshot_from(graph) -> dict:
    """A deployed snapshot written the way the real serialiser writes one."""
    def rows(cls, exclude=("created_at", "updated_at")):
        return [row_to_snapshot_dict(r, exclude=exclude) for r in graph[cls]]

    hierarchies = []
    for h in graph[HierarchyDefinition]:
        h_dict = row_to_snapshot_dict(h, exclude=("created_at", "updated_at"))
        h_dict["levels"] = [
            dict(
                row_to_snapshot_dict(lv, exclude=("created_at", "updated_at")),
                attributes=[
                    row_to_snapshot_dict(a)
                    for a in graph[HierarchyLevelAttribute]
                    if a.level_id == lv.id
                ],
            )
            for lv in graph[HierarchyLevel]
            if lv.hierarchy_id == h.id
        ]
        hierarchies.append(h_dict)

    return {
        "measures": rows(Measure),
        "dimensions": rows(Dimension),
        "tables": rows(ModelTable),
        "columns": rows(ModelColumn, exclude=("created_at",)),
        "joins": rows(Join, exclude=("created_at",)),
        "user_defined_attributes": rows(UserDefinedAttribute),
        "uda_column_refs": [row_to_snapshot_dict(r) for r in graph[UserDefinedAttributeColumnRef]],
        "hierarchies": hierarchies,
        "calendar_tables": rows(CalendarTable),
        "attribute_relationships": rows(DimensionAttributeRelationship),
        "data_sources": rows(DataSource),
    }


# ---------------------------------------------------------------------------
# Entity-dispatching fake session
# ---------------------------------------------------------------------------

class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)


class FakeSession:
    """Answers ``select(Entity)`` from an in-memory graph, keyed on the entity."""

    def __init__(self, graph, version_row):
        self.graph = graph
        self.version_row = version_row
        self.entities_queried: list[str] = []

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        self.entities_queried.append(entity.__name__)
        return _Result(self.graph.get(entity, []))

    async def get(self, cls, obj_id):
        if self.version_row is not None and str(obj_id) == str(self.version_row.id):
            return self.version_row
        return None


class _Version:
    def __init__(self, snapshot):
        self.id = VERSION_ID
        self.snapshot_json = snapshot


async def _check(graph, snapshot, *, measures=("revenue",), grain=("country",)):
    session = FakeSession(graph, _Version(snapshot))
    return await check_deployed_definition_drift(
        session,
        model_id=MODEL_ID,
        deployed_version_id=VERSION_ID,
        measure_names=list(measures),
        grain_names=list(grain),
    )


# ---------------------------------------------------------------------------
# No drift
# ---------------------------------------------------------------------------

async def test_matching_live_and_deployed_reports_no_drift():
    graph = build_live_graph()
    result = await _check(graph, snapshot_from(graph))
    assert result.reasons == (), result.reasons
    assert result.checked is True
    assert result.live_digest


async def test_undeployed_model_is_allowed_without_checking():
    graph = build_live_graph()
    session = FakeSession(graph, None)
    result = await check_deployed_definition_drift(
        session, model_id=MODEL_ID, deployed_version_id=None,
        measure_names=["revenue"], grain_names=["country"],
    )
    assert result.reasons == ()
    assert result.checked is False
    assert session.entities_queried == [], "no reads for an undeployed model"


# ---------------------------------------------------------------------------
# Carried over from the deleted scheduler-local drift tests
# ---------------------------------------------------------------------------

async def test_measure_source_column_drift_is_detected():
    graph = build_live_graph()
    snapshot = snapshot_from(graph)
    graph[Measure][0].source_column_id = COL_OTHER  # live edit, undeployed
    result = await _check(graph, snapshot)
    assert any("source_column_id changed" in r for r in result.reasons), result.reasons


async def test_referenced_calculated_dependency_drift_is_detected():
    """'profit' is unchanged, but the 'revenue' it references drifted.

    The closure must follow calculated-measure references or a dependency edit
    silently changes every value 'profit' produces.
    """
    graph = build_live_graph()
    snapshot = snapshot_from(graph)
    graph[Measure][0].source_column_id = COL_OTHER  # 'revenue'
    result = await _check(graph, snapshot, measures=("profit",))
    assert any(
        "'revenue'" in r and "source_column_id changed" in r for r in result.reasons
    ), result.reasons


async def test_autonomous_drift_sweep_flag_is_not_treated_as_definition_drift():
    """``ModelColumn.drift_removed`` is written by the SCHEDULED schema-drift
    sweep, not by a modeller, and no builder reads it.

    Comparing it meant an unattended nightly job refused every refresh on the
    model until a human redeployed — an availability and cost regression with no
    correctness gain. Queries stayed correct (they fall back to source), which
    is precisely why it would have gone unnoticed. Round-3 review found it by
    execution.
    """
    graph = build_live_graph()
    snapshot = snapshot_from(graph)
    graph[ModelColumn][0].drift_removed = True
    result = await _check(graph, snapshot)
    assert not any("drift_removed" in r for r in result.reasons), result.reasons


async def test_physical_column_repoint_is_detected():
    """Same source_column_id UUID, renamed underlying column."""
    graph = build_live_graph()
    snapshot = snapshot_from(graph)
    graph[ModelColumn][0].column_name = "amount_net"
    result = await _check(graph, snapshot)
    assert any("column_name changed" in r for r in result.reasons), result.reasons


# ---------------------------------------------------------------------------
# The coverage the Codex gate reported MISSING
# ---------------------------------------------------------------------------

async def test_grain_dimension_rebound_is_detected():
    """CRITICAL finding 2: the predecessor guard compared measures only."""
    graph = build_live_graph()
    snapshot = snapshot_from(graph)
    graph[Dimension][0].source_column_id = COL_OTHER
    result = await _check(graph, snapshot)
    assert any(
        "dimension" in r and "source_column_id changed" in r for r in result.reasons
    ), result.reasons


async def test_grain_dimension_deleted_is_detected():
    graph = build_live_graph()
    snapshot = snapshot_from(graph)
    graph[Dimension] = []
    result = await _check(graph, snapshot)
    assert any("missing live" in r for r in result.reasons), result.reasons
    assert any("grain 'country'" in r for r in result.reasons), result.reasons


async def test_join_type_change_is_detected():
    """CRITICAL finding 2: inner -> left changes which fact rows survive."""
    graph = build_live_graph()
    snapshot = snapshot_from(graph)
    graph[Join][0].join_type = "left"
    result = await _check(graph, snapshot)
    assert any("join_type changed" in r for r in result.reasons), result.reasons


async def test_new_join_edge_is_detected():
    graph = build_live_graph()
    snapshot = snapshot_from(graph)
    graph[Join].append(
        Join(id=uuid.uuid4(), model_id=MODEL_ID, left_table_id=TABLE_ID,
             right_table_id=TABLE2_ID, join_type="left",
             left_column_id=COL_OTHER, right_column_id=COL_JOIN_R)
    )
    result = await _check(graph, snapshot)
    assert any(
        "join" in r and "not in the deployed snapshot" in r for r in result.reasons
    ), result.reasons


async def test_table_physical_repoint_is_detected():
    graph = build_live_graph()
    snapshot = snapshot_from(graph)
    graph[ModelTable][0].physical_name = "fact_sales_v2"
    result = await _check(graph, snapshot)
    assert any("physical_name changed" in r for r in result.reasons), result.reasons


# ---------------------------------------------------------------------------
# Round-1 review: entities a builder reads that the closure originally missed
# ---------------------------------------------------------------------------

async def test_calendar_fiscal_start_change_is_detected():
    """Proven wrong-number case from the round-1 review.

    ``ensure_target_calendar_table`` names the target calendar
    ``tess_cal_{calendar_type}_{fiscal_year_start_month}``, and the fiscal
    variants hold DIFFERENT ``year_no`` values for the same date. A live
    fiscal-start edit therefore re-points the CTAS at a different calendar and
    silently rebases every fiscal total, with no deploy and no error.
    """
    graph = build_live_graph()
    snapshot = snapshot_from(graph)
    graph[CalendarTable][0].fiscal_year_start_month = 4
    result = await _check(graph, snapshot)
    assert any(
        "calendar table" in r and "fiscal_year_start_month changed" in r
        for r in result.reasons
    ), result.reasons
    baseline = await _check(build_live_graph(), snapshot)
    assert result.live_digest != baseline.live_digest, (
        "the stamp-time re-check must see it too, not only the up-front guard"
    )


async def test_calendar_type_change_is_detected():
    graph = build_live_graph()
    snapshot = snapshot_from(graph)
    graph[CalendarTable][0].calendar_type = "iso_week"
    result = await _check(graph, snapshot)
    assert any(
        "calendar table" in r and "calendar_type changed" in r
        for r in result.reasons
    ), result.reasons


async def test_attribute_relationship_declaration_change_is_detected():
    """The passenger planner emits SELECT fragments from these declarations."""
    graph = build_live_graph()
    snapshot = snapshot_from(graph)
    graph[DimensionAttributeRelationship][0].null_policy = "INCLUDE"
    result = await _check(graph, snapshot)
    assert any(
        "attribute relationship" in r and "null_policy changed" in r
        for r in result.reasons
    ), result.reasons


async def test_data_source_repoint_is_detected():
    """A re-pointed source sends the SAME SQL at a DIFFERENT database."""
    graph = build_live_graph()
    snapshot = snapshot_from(graph)
    graph[DataSource][0].project_connection_id = uuid.uuid4()
    result = await _check(graph, snapshot)
    assert any(
        "data source" in r and "project_connection_id changed" in r
        for r in result.reasons
    ), result.reasons


async def test_unreferenced_calendar_is_not_compared():
    """No false refusal from a calendar no ModelTable points at.

    The snapshot carries every calendar of every data source the model touches;
    a CTAS can only reach the ones its tables reference.
    """
    graph = build_live_graph()
    graph[CalendarTable].append(
        CalendarTable(
            id=uuid.uuid4(), data_source_id=DATA_SOURCE_ID,
            table_name="dim_calendar_other", dialect="postgresql",
            calendar_type="fiscal", fiscal_year_start_month=7,
            autocreated=True,
        )
    )
    snapshot = snapshot_from(graph)
    graph[CalendarTable][1].fiscal_year_start_month = 10  # unreferenced -> inert
    result = await _check(graph, snapshot)
    assert result.reasons == (), result.reasons


# ---------------------------------------------------------------------------
# Table additions: the fact-anchor rule
# ---------------------------------------------------------------------------

async def test_added_fact_table_is_detected():
    """``build_from_clause`` anchors on the FIRST fact table."""
    graph = build_live_graph()
    snapshot = snapshot_from(graph)
    graph[ModelTable].append(
        ModelTable(id=uuid.uuid4(), model_id=MODEL_ID, source_id=DATA_SOURCE_ID,
                   table_type="fact", physical_name="fact_other")
    )
    result = await _check(graph, snapshot)
    assert any("FROM-clause anchor" in r for r in result.reasons), result.reasons


async def test_added_table_is_drift_when_the_model_has_no_fact_table():
    """The fact-anchor rule's premise fails when NO table is typed ``fact``.

    ``build_from_clause`` then anchors on ``next(iter(tables.values()))``, so ANY
    live-only table can become the FROM anchor. Round-2 review proved it by
    execution: adding an unjoined ``staging_customer`` to a zero-fact model
    turned ``FROM "dim_customer" AS base JOIN "dim_region" AS t1`` into
    ``FROM "staging_customer" AS base`` — the join gone entirely — while the
    comparison reported no drift at all. A staging copy carrying the same column
    names then serves as the deployed version's numbers.
    """
    graph = build_live_graph()
    for t in graph[ModelTable]:
        t.table_type = "dim_detail"  # a model with no fact table
    snapshot = snapshot_from(graph)
    graph[ModelTable].append(
        ModelTable(id=uuid.uuid4(), model_id=MODEL_ID, source_id=DATA_SOURCE_ID,
                   table_type="dim_detail", physical_name="staging_customer")
    )
    result = await _check(graph, snapshot)
    assert any("anchor" in r for r in result.reasons), result.reasons


async def test_added_non_fact_table_is_not_drift():
    """A modeller adding a dimension table to a draft must not stale the model.

    It cannot reach the FROM clause without a join, and joins are compared
    bidirectionally.
    """
    graph = build_live_graph()
    snapshot = snapshot_from(graph)
    graph[ModelTable].append(
        ModelTable(id=uuid.uuid4(), model_id=MODEL_ID, source_id=DATA_SOURCE_ID,
                   table_type="dimension", physical_name="dim_new")
    )
    result = await _check(graph, snapshot)
    assert result.reasons == (), result.reasons


# ---------------------------------------------------------------------------
# Fail-closed on an unreadable snapshot
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "snapshot,expected",
    [
        (None, "missing or not an object"),
        ({}, "missing or not an object"),
        ({"measures": []}, "no measures"),
        ({"measures": "nope"}, "no measures"),
    ],
)
async def test_unreadable_snapshot_fails_closed(snapshot, expected):
    """An unknown drift state must never be treated as a clean one."""
    graph = build_live_graph()
    session = FakeSession(graph, _Version(snapshot))
    result = await check_deployed_definition_drift(
        session, model_id=MODEL_ID, deployed_version_id=VERSION_ID,
        measure_names=["revenue"], grain_names=["country"],
    )
    assert result.has_drift
    assert expected in result.message()


async def test_missing_version_row_fails_closed():
    graph = build_live_graph()
    session = FakeSession(graph, None)
    result = await check_deployed_definition_drift(
        session, model_id=MODEL_ID, deployed_version_id=VERSION_ID,
        measure_names=["revenue"], grain_names=["country"],
    )
    assert result.has_drift
    assert "deployed version row missing" in result.message()


# ---------------------------------------------------------------------------
# Digest — what the mid-build re-check compares
# ---------------------------------------------------------------------------

async def test_digest_moves_when_a_grain_dimension_is_rebound():
    graph = build_live_graph()
    snapshot = snapshot_from(graph)
    before = await _check(graph, snapshot)
    graph[Dimension][0].source_column_id = COL_OTHER
    after = await _check(graph, snapshot)
    assert before.live_digest != after.live_digest


async def test_digest_is_stable_for_an_unchanged_graph():
    graph = build_live_graph()
    snapshot = snapshot_from(graph)
    first = await _check(graph, snapshot)
    second = await _check(graph, snapshot)
    assert first.live_digest == second.live_digest


async def test_reads_are_forced_past_the_identity_map():
    """Bug-8479: a cached instance would compare the wrong 'live' state.

    The scheduler sweep reuses one ``expire_on_commit=False`` session across many
    aggregates, so a plain SELECT can return rows loaded before a deploy landed.
    Every read here must carry ``populate_existing``.
    """
    graph = build_live_graph()
    snapshot = snapshot_from(graph)
    seen: list[bool] = []

    class _RecordingSession(FakeSession):
        async def execute(self, stmt):
            seen.append(
                bool(stmt.get_execution_options().get("populate_existing"))
            )
            return await super().execute(stmt)

    session = _RecordingSession(graph, _Version(snapshot))
    await check_deployed_definition_drift(
        session, model_id=MODEL_ID, deployed_version_id=VERSION_ID,
        measure_names=["revenue"], grain_names=["country"],
    )
    assert seen and all(seen), (
        "every definition read must bypass the identity map"
    )
