from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.db.models import LineageMapping

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    make_mock_db,
    make_model,
)

pytestmark = pytest.mark.unit


def _result(*, scalars=None, all_rows=None, scalar=None):
    result = MagicMock()
    result.scalars.return_value.all.return_value = scalars or []
    result.all.return_value = all_rows or []
    result.scalar.return_value = scalar
    return result


@pytest.mark.anyio
async def test_lineage_graph_emits_consumable_field_nodes_from_real_lineage_mapping(client):
    source_id = uuid.uuid4()
    table_id = uuid.uuid4()
    column_id = uuid.uuid4()
    lineage = LineageMapping(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        semantic_field_name="gross_revenue",
        semantic_field_type="measure",
        aggregate_col_id=None,
        source_column_id=column_id,
    )
    source = types.SimpleNamespace(
        id=source_id,
        display_name="Warehouse",
        source_type="postgres",
        config={"schema": "public"},
    )
    table = types.SimpleNamespace(
        id=table_id,
        model_id=TEST_MODEL_ID,
        source_id=source_id,
        physical_name="orders",
        alias="orders",
        display_name="Orders",
    )
    column = types.SimpleNamespace(
        id=column_id,
        model_table_id=table_id,
        column_name="revenue",
        display_name="Revenue",
        data_type="numeric",
        is_hidden=False,
    )

    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())
    # Bug-8073: the endpoint now also loads the live definitions it derives
    # lineage from (measures, dimensions, KPIs, UDAs) between the LineageMapping
    # read and the per-source table counts. This case deliberately supplies NONE
    # of them, so it still asserts exactly what it always did: that an explicit
    # LineageMapping row on its own produces a consumable column -> field edge.
    db.execute = AsyncMock(
        side_effect=[
            _result(scalars=[source]),      # sources
            _result(),                      # targets
            _result(),                      # aggregates
            _result(scalars=[lineage]),     # LineageMapping rows
            _result(),                      # measures
            _result(),                      # dimensions
            _result(),                      # kpis
            _result(),                      # user-defined attributes
            _result(all_rows=[(source_id, 1)]),   # per-source table counts
            _result(scalar=0),                    # downstream asset count
            _result(all_rows=[(column, table)]),  # model columns + tables
        ]
    )

    with patch("src.api.lineage.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/lineage"
        )

    assert resp.status_code == 200
    body = resp.json()
    nodes = {node["id"]: node for node in body["nodes"]}
    edges = {(edge["source"], edge["target"], edge["label"]) for edge in body["edges"]}

    column_node_id = f"col:{column_id}"
    field_node_id = "field:measure:gross_revenue"
    assert nodes[column_node_id]["type"] == "column"
    assert nodes[column_node_id]["label"] == "Revenue"
    assert nodes[field_node_id]["type"] == "field"
    assert nodes[field_node_id]["label"] == "gross_revenue"
    assert (column_node_id, field_node_id, "feeds") in edges
    assert (field_node_id, str(TEST_MODEL_ID), "defined by") in edges


# ---------------------------------------------------------------------------
# Bug-8073 - lineage must be DERIVED from the model's own definitions.
#
# Every field and column node used to come exclusively from LineageMapping rows.
# No production code path writes that table, so an ordinary model (add table,
# add measure, add dimension, add KPI) rendered sources/aggregates/targets and
# ZERO field or column nodes - a populated-looking graph missing exactly the
# dependencies a modeller needs before a breaking edit. A live modely read
# returned 225 nodes, none of them field or column.
# ---------------------------------------------------------------------------

from src.api.lineage_derive import build_semantic_lineage  # noqa: E402

_MODEL = "model-1"


def _col(col_id, table_id, name, display=None):
    return types.SimpleNamespace(
        id=col_id, model_table_id=table_id, column_name=name,
        display_name=display or name, data_type="numeric", is_hidden=False,
    )


def _tbl(table_id, name="orders"):
    return types.SimpleNamespace(
        id=table_id, physical_name=name, alias=name, display_name=name.title(),
    )


def _measure(mid, name, source_column_id=None, **kw):
    base = dict(
        id=mid, name=name, source_column_id=source_column_id,
        user_defined_attribute_id=None, semi_additive_account_column_id=None,
        measure_type="standard", default_agg="sum", expression=None,
        variant_of_measure_id=None, is_invalid=False, invalid_reason=None,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _dimension(did, name, source_column_id=None, **kw):
    base = dict(
        id=did, name=name, source_column_id=source_column_id,
        display_column_id=None, user_defined_attribute_id=None,
        is_time_dim=False, is_invalid=False, invalid_reason=None,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _kpi(kid, name, value_measure_id=None, **kw):
    base = dict(
        id=kid, name=name, value_measure_id=value_measure_id,
        goal_measure_id=None, target_measure_id=None, kpi_type="simple_measure",
        expression=None, target_expression=None,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _edge_set(edges):
    return {(e.source, e.target, e.label) for e in edges}


def test_lineage_is_derived_with_no_lineage_mapping_rows_at_all():
    """The reported defect in one assertion: an ordinary model with zero
    LineageMapping rows must still produce the column -> measure -> model chain."""
    table_id, col_id, mid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    nodes, edges = build_semantic_lineage(
        model_id=_MODEL,
        measures=[_measure(mid, "gross_revenue", source_column_id=col_id)],
        dimensions=[],
        kpis=[],
        udas=[],
        columns_by_id={col_id: (_col(col_id, table_id, "revenue", "Revenue"), _tbl(table_id))},
        lineage_rows=[],
    )

    by_id = {n.id: n for n in nodes}
    assert by_id["col:" + str(col_id)].type == "column"
    assert by_id["field:measure:gross_revenue"].type == "field"
    assert ("col:" + str(col_id), "field:measure:gross_revenue", "feeds") in _edge_set(edges)
    assert ("field:measure:gross_revenue", _MODEL, "defined by") in _edge_set(edges)


def test_kpi_chain_from_column_to_measure_to_kpi_is_complete():
    """The chain the external review asked to be assertable end to end without
    manually inserting lineage rows."""
    table_id, col_id, mid, kid = (uuid.uuid4() for _ in range(4))
    nodes, edges = build_semantic_lineage(
        model_id=_MODEL,
        measures=[_measure(mid, "revenue", source_column_id=col_id)],
        dimensions=[],
        kpis=[_kpi(kid, "Revenue Growth", value_measure_id=mid)],
        udas=[],
        columns_by_id={col_id: (_col(col_id, table_id, "amount"), _tbl(table_id))},
        lineage_rows=[],
    )

    e = _edge_set(edges)
    assert ("col:" + str(col_id), "field:measure:revenue", "feeds") in e
    assert ("field:measure:revenue", "field:kpi:Revenue Growth", "feeds") in e
    node_types = {n.id: n.meta.get("Field type") for n in nodes if n.type == "field"}
    assert node_types["field:kpi:Revenue Growth"] == "kpi"


def test_calculated_measure_depends_on_the_measures_it_references():
    """Editing a base measure changes every calc that reads it; a modeller
    cannot see that from the base measure alone without this edge."""
    base_id, calc_id = uuid.uuid4(), uuid.uuid4()
    nodes, edges = build_semantic_lineage(
        model_id=_MODEL,
        measures=[
            _measure(base_id, "cost"),
            _measure(calc_id, "margin", measure_type="calculated",
                     expression='measure("cost") * -1'),
        ],
        dimensions=[], kpis=[], udas=[], columns_by_id={}, lineage_rows=[],
    )

    assert ("field:measure:cost", "field:measure:margin", "feeds") in _edge_set(edges)


def test_expression_reference_matching_is_whole_token_not_substring():
    """A measure named `cost` must not be treated as a dependency of an
    expression that only mentions `cost_of_sales` - that would draw an edge that
    does not exist and misstate the blast radius."""
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _nodes, edges = build_semantic_lineage(
        model_id=_MODEL,
        measures=[
            _measure(a, "cost"),
            _measure(b, "cost_of_sales"),
            _measure(c, "margin", expression='measure("cost_of_sales") * -1'),
        ],
        dimensions=[], kpis=[], udas=[], columns_by_id={}, lineage_rows=[],
    )

    e = _edge_set(edges)
    assert ("field:measure:cost_of_sales", "field:measure:margin", "feeds") in e
    assert ("field:measure:cost", "field:measure:margin", "feeds") not in e


def test_kpi_expression_and_target_expression_dependencies_are_exact():
    """KPI formula and target references both contribute stable dependencies;
    names that merely appear as other tokens do not."""
    revenue_id, cost_id, target_id, helper_id = (uuid.uuid4() for _ in range(4))
    kpi_id = uuid.uuid4()
    nodes, edges = build_semantic_lineage(
        model_id=_MODEL,
        measures=[
            _measure(revenue_id, "revenue"),
            _measure(cost_id, "cost"),
            _measure(target_id, "revenue_target"),
            _measure(helper_id, "safe_div"),
        ],
        dimensions=[],
        kpis=[_kpi(
            kpi_id,
            "Margin Attainment",
            expression='safe_div(measure("revenue"), measure("cost"))',
            target_expression='measure("revenue_target")',
        )],
        udas=[], columns_by_id={}, lineage_rows=[],
    )

    e = _edge_set(edges)
    kpi_nid = "field:kpi:Margin Attainment"
    assert ("field:measure:revenue", kpi_nid, "feeds") in e
    assert ("field:measure:cost", kpi_nid, "feeds") in e
    assert ("field:measure:revenue_target", kpi_nid, "feeds") in e
    assert ("field:measure:safe_div", kpi_nid, "feeds") not in e
    by_id = {n.id: n for n in nodes}
    assert by_id[kpi_nid].meta["Object ID"] == str(kpi_id)


def test_kpi_formula_dependency_resolves_unique_name_case_insensitively():
    source_id, dependent_id = uuid.uuid4(), uuid.uuid4()
    _nodes, edges = build_semantic_lineage(
        model_id=_MODEL,
        measures=[],
        dimensions=[],
        kpis=[
            _kpi(source_id, "Revenue Growth"),
            _kpi(
                dependent_id,
                "Growth Score",
                expression='kpi("REVENUE GROWTH") + kpi("revenue growth")',
            ),
        ],
        udas=[], columns_by_id={}, lineage_rows=[],
    )

    edge = (
        "field:kpi:Revenue Growth",
        "field:kpi:Growth Score",
        "feeds",
    )
    assert edge in _edge_set(edges)
    matching_edges = [
        item for item in edges if (item.source, item.target, item.label) == edge
    ]
    assert len(matching_edges) == 1


def test_kpi_target_expression_dependency_resolves_forward_reference():
    dependent_id, target_id = uuid.uuid4(), uuid.uuid4()
    _nodes, edges = build_semantic_lineage(
        model_id=_MODEL,
        measures=[],
        dimensions=[],
        kpis=[
            _kpi(
                dependent_id,
                "Attainment",
                target_expression='kpi("TARGET KPI")',
            ),
            _kpi(target_id, "Target KPI"),
        ],
        udas=[], columns_by_id={}, lineage_rows=[],
    )

    assert (
        "field:kpi:Target KPI",
        "field:kpi:Attainment",
        "feeds",
    ) in _edge_set(edges)


def test_ambiguous_case_insensitive_kpi_name_emits_no_dependency_edge():
    lower_id, upper_id, dependent_id = (uuid.uuid4() for _ in range(3))
    _nodes, edges = build_semantic_lineage(
        model_id=_MODEL,
        measures=[],
        dimensions=[],
        kpis=[
            _kpi(lower_id, "Revenue"),
            _kpi(upper_id, "REVENUE"),
            _kpi(dependent_id, "Revenue Consumer", expression='kpi("revenue")'),
        ],
        udas=[], columns_by_id={}, lineage_rows=[],
    )

    edge_set = _edge_set(edges)
    assert ("field:kpi:Revenue", "field:kpi:Revenue Consumer", "feeds") not in edge_set
    assert ("field:kpi:REVENUE", "field:kpi:Revenue Consumer", "feeds") not in edge_set


def test_uda_backed_field_links_every_column_the_expression_reads():
    """A UDA is an expression over several physical columns; the field depends
    on all of them, not on a single source_column_id."""
    table_id = uuid.uuid4()
    c1, c2, uda_id, did = (uuid.uuid4() for _ in range(4))
    uda = types.SimpleNamespace(
        id=uda_id,
        column_refs=[
            types.SimpleNamespace(column_id=c1),
            types.SimpleNamespace(column_id=c2),
        ],
    )
    _nodes, edges = build_semantic_lineage(
        model_id=_MODEL,
        measures=[],
        dimensions=[_dimension(did, "region_band", user_defined_attribute_id=uda_id)],
        kpis=[], udas=[uda],
        columns_by_id={
            c1: (_col(c1, table_id, "region"), _tbl(table_id)),
            c2: (_col(c2, table_id, "band"), _tbl(table_id)),
        },
        lineage_rows=[],
    )

    e = _edge_set(edges)
    assert ("col:" + str(c1), "field:dimension:region_band", "feeds") in e
    assert ("col:" + str(c2), "field:dimension:region_band", "feeds") in e


def test_explicit_mapping_still_contributes_as_enrichment():
    """LineageMapping is downgraded to enrichment, not ignored: a row naming a
    column the definition does not reference must still produce its edge."""
    table_id, col_a, col_b, mid = (uuid.uuid4() for _ in range(4))
    mapping = types.SimpleNamespace(
        semantic_field_name="revenue", semantic_field_type="measure",
        source_column_id=col_b,
    )
    _nodes, edges = build_semantic_lineage(
        model_id=_MODEL,
        measures=[_measure(mid, "revenue", source_column_id=col_a)],
        dimensions=[], kpis=[], udas=[],
        columns_by_id={
            col_a: (_col(col_a, table_id, "amount"), _tbl(table_id)),
            col_b: (_col(col_b, table_id, "amount_legacy"), _tbl(table_id)),
        },
        lineage_rows=[mapping],
    )

    e = _edge_set(edges)
    assert ("col:" + str(col_a), "field:measure:revenue", "feeds") in e
    assert ("col:" + str(col_b), "field:measure:revenue", "feeds") in e


def test_reference_to_a_column_outside_this_model_is_skipped_not_dangled():
    """A nulled FK or another model's column must not become a node with no
    label - a dangling node reads to a user as a real dependency."""
    stale_col = uuid.uuid4()
    nodes, edges = build_semantic_lineage(
        model_id=_MODEL,
        measures=[_measure(uuid.uuid4(), "revenue", source_column_id=stale_col)],
        dimensions=[], kpis=[], udas=[], columns_by_id={}, lineage_rows=[],
    )

    assert all(not n.id.startswith("col:") for n in nodes)
    assert all("col:" not in edge.source for edge in edges)


def test_nodes_and_edges_are_deduplicated():
    """Two measures on the same column, and a mapping repeating a derived edge,
    must not multiply nodes or edges in the rendered graph."""
    table_id, col_id = uuid.uuid4(), uuid.uuid4()
    mapping = types.SimpleNamespace(
        semantic_field_name="revenue", semantic_field_type="measure",
        source_column_id=col_id,
    )
    nodes, edges = build_semantic_lineage(
        model_id=_MODEL,
        measures=[
            _measure(uuid.uuid4(), "revenue", source_column_id=col_id),
            _measure(uuid.uuid4(), "revenue_net", source_column_id=col_id),
        ],
        dimensions=[], kpis=[], udas=[],
        columns_by_id={col_id: (_col(col_id, table_id, "amount"), _tbl(table_id))},
        lineage_rows=[mapping],
    )

    assert len([n for n in nodes if n.id == "col:" + str(col_id)]) == 1
    assert len(edges) == len(_edge_set(edges))


@pytest.mark.anyio
async def test_lineage_route_derives_fields_with_no_lineage_mapping_rows(client):
    """Endpoint-level wiring guard for Bug-8073.

    The pure builder tests above prove the derivation is correct; this one
    proves the ROUTE actually loads the live definitions and feeds them in. A
    correct builder that the endpoint never supplies is the
    producer-fixed-consumer-unwired gap, and it is exactly what the previous
    implementation looked like from the outside: a 200 with a plausible graph
    and no field or column nodes in it.
    """
    source_id = uuid.uuid4()
    table_id = uuid.uuid4()
    column_id = uuid.uuid4()
    measure_id = uuid.uuid4()

    source = types.SimpleNamespace(
        id=source_id, display_name="Warehouse", source_type="postgres",
        config={"schema": "public"},
    )
    table = _tbl(table_id)
    column = _col(column_id, table_id, "revenue", "Revenue")
    measure = _measure(measure_id, "gross_revenue", source_column_id=column_id)
    kpi = _kpi(uuid.uuid4(), "Revenue Growth", value_measure_id=measure_id)

    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())
    db.execute = AsyncMock(
        side_effect=[
            _result(scalars=[source]),            # sources
            _result(),                            # targets
            _result(),                            # aggregates
            _result(),                            # LineageMapping rows: NONE
            _result(scalars=[measure]),           # measures
            _result(),                            # dimensions
            _result(scalars=[kpi]),               # kpis
            _result(),                            # user-defined attributes
            _result(all_rows=[(source_id, 1)]),   # per-source table counts
            _result(scalar=0),                    # downstream asset count
            _result(all_rows=[(column, table)]),  # model columns + tables
        ]
    )

    with patch("src.api.lineage.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/lineage"
        )

    assert resp.status_code == 200
    body = resp.json()
    node_ids = {node["id"] for node in body["nodes"]}
    edges = {(e["source"], e["target"], e["label"]) for e in body["edges"]}

    assert "col:" + str(column_id) in node_ids
    assert "field:measure:gross_revenue" in node_ids
    assert "field:kpi:Revenue Growth" in node_ids
    assert ("col:" + str(column_id), "field:measure:gross_revenue", "feeds") in edges
    assert ("field:measure:gross_revenue", "field:kpi:Revenue Growth", "feeds") in edges
