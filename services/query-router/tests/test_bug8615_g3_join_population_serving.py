"""G3/Bug-8615 serving and aggregate-proof regression guards.

The tests use the pure graph contracts at the route boundary.  They are
mutation-sensitive: reverting mandatory-edge augmentation to projection-only
causes the explicit artifact-plan refusal to fail.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace
import asyncio

from shared.semantic.aggregate_plan_bound import aggregate_plan_upper_bound
from shared.semantic.join_population_serving import (
    POPULATION_PARTICIPATION_POPULATION_DEFINING,
    augment_required_table_ids,
)
from src.routing.aggregate_population import (
    AggregateObjectIndex,
    aggregate_population_proven,
)
from src.routing.pocket_matcher import _edges_from_rows
from src.routing.pocket_population import JoinEdge, ModelJoinGraph, population_proven
from src.rewrite.joins import _build_joined_from_clause
from src.rewrite import source_sql


def _graph(participation=POPULATION_PARTICIPATION_POPULATION_DEFINING):
    edge = JoinEdge(
        left_table_id="fact",
        right_table_id="dim",
        left_column_id="fact-key",
        right_column_id="dim-key",
        join_type="inner",
        population_participation=participation,
    )
    return ModelJoinGraph(
        table_ids=frozenset({"fact", "dim"}),
        edges=(edge,),
        pk_column_ids=frozenset({"dim-key"}),
        table_id_by_column_id={"fact-key": "fact", "dim-key": "dim"},
        anchor_table_id="fact",
    )


def test_population_defining_edge_is_required_even_when_query_projects_fact_only():
    graph = _graph()
    assert population_proven(
        graph=graph,
        query_table_ids={"fact"},
        plan_table_ids={"fact"},
    ) is False


def test_population_defining_edge_is_included_in_aggregate_plan_bound():
    graph = _graph()
    result = aggregate_plan_upper_bound(
        table_ids=graph.table_ids,
        edges=[(edge.left_table_id, edge.right_table_id) for edge in graph.edges],
        anchor_table_id=graph.anchor_table_id,
        needed_table_ids={"fact"},
        population_defining_table_ids={"fact", "dim"},
    )
    assert result == frozenset({"fact", "dim"})


def test_projection_only_states_remain_elidable():
    graph = _graph("preserve_base_rows")
    assert population_proven(
        graph=graph,
        query_table_ids={"fact"},
        plan_table_ids={"fact"},
    ) is True


def test_unknown_participation_is_not_treated_as_mandatory():
    graph = _graph("future-token")
    assert population_proven(
        graph=graph,
        query_table_ids={"fact"},
        plan_table_ids={"fact"},
    ) is True


def test_deployed_row_adapter_preserves_population_role_for_both_proofs():
    """Bug-8615: snapshot rows retain G3 metadata through both proof consumers."""
    rows = [{
        "left_table_id": "fact",
        "right_table_id": "dim",
        "left_column_id": "fact-key",
        "right_column_id": "dim-key",
        "join_type": "inner",
        "population_participation": "population_defining",
    }]
    edges = _edges_from_rows(rows)
    assert edges is not None and len(edges) == 1
    assert edges[0].population_participation == "population_defining"
    graph = ModelJoinGraph(
        table_ids=frozenset({"fact", "dim"}),
        edges=edges,
        pk_column_ids=frozenset({"dim-key"}),
        table_id_by_column_id={"fact-key": "fact", "dim-key": "dim"},
        anchor_table_id="fact",
    )
    # A fact-only query cannot prove equivalence against a pocket when the
    # deployed edge defines the model population.
    assert population_proven(
        graph=graph,
        query_table_ids={"fact"},
        plan_table_ids={"fact"},
    ) is False
    aggregate_ok, reason = aggregate_population_proven(
        aggregate=SimpleNamespace(grain=[], columns=[]),
        graph=graph,
        index=AggregateObjectIndex(),
        query_table_ids={"fact"},
        split_codes=True,
    )
    assert aggregate_ok is True, reason


def test_deployed_row_adapter_coerces_unknown_role_conservatively():
    rows = [{
        "left_table_id": "fact",
        "right_table_id": "dim",
        "left_column_id": "fact-key",
        "right_column_id": "dim-key",
        "join_type": "inner",
        "population_participation": "future-role",
    }]
    edges = _edges_from_rows(rows)
    assert edges is not None and edges[0].population_participation == "undeclared"


def test_source_join_builder_receives_the_same_mandatory_endpoint_set():
    fact = SimpleNamespace(id="fact", physical_name="source.fact", alias="fact")
    dim = SimpleNamespace(id="dim", physical_name="source.dim", alias="dim")
    fact_col = SimpleNamespace(
        id="fact-key", model_table_id="fact", column_name="dim_id", data_type="integer",
    )
    dim_col = SimpleNamespace(
        id="dim-key", model_table_id="dim", column_name="id", data_type="integer",
    )
    join = SimpleNamespace(
        id="join", left_table_id="fact", right_table_id="dim",
        left_column_id="fact-key", right_column_id="dim-key",
        join_type="inner", population_participation="population_defining",
    )
    required = augment_required_table_ids(
        {"fact"}, [join], table_ids={"fact", "dim"},
    )
    assert required == {"fact", "dim"}
    sql = _build_joined_from_clause(
        base_table_id="fact",
        required_table_ids=required,
        joins=[join],
        tables_by_id={"fact": fact, "dim": dim},
        columns_by_id={"fact-key": fact_col, "dim-key": dim_col},
        alias_by_table_id={"fact": "fact", "dim": "dim"},
        connector="postgresql",
    )
    assert '"source"."dim"' in sql


def test_source_count_route_includes_population_defining_edge(monkeypatch):
    fact = SimpleNamespace(id="fact", physical_name="source.fact", alias="fact")
    dim = SimpleNamespace(id="dim", physical_name="source.dim", alias="dim")
    fact_col = SimpleNamespace(
        id="fact-key", model_table_id="fact", column_name="dim_id", data_type="integer",
    )
    dim_col = SimpleNamespace(
        id="dim-key", model_table_id="dim", column_name="id", data_type="integer",
    )
    join = SimpleNamespace(
        id="join", left_table_id="fact", right_table_id="dim",
        left_column_id="fact-key", right_column_id="dim-key",
        join_type="inner", population_participation="population_defining",
    )
    monkeypatch.setattr(
        source_sql,
        "_load_model_graph",
        lambda *args, **kwargs: _async_result(
            ({"fact": fact, "dim": dim}, [join],
             {"fact-key": fact_col, "dim-key": dim_col}, {})
        ),
    )
    from src.rewrite import snapshot_graph_resolvers
    monkeypatch.setattr(
        snapshot_graph_resolvers,
        "resolve_base_table",
        lambda *args, **kwargs: _async_result(fact),
    )
    logical = SimpleNamespace(
        from_tables=["model"], raw_query="SELECT COUNT(*) FROM model",
        input_dialect="postgres", limit=None, offset=None,
        select_expressions=[SimpleNamespace(classification="literal", agg_function="count", alias="n")],
    )
    bound = SimpleNamespace(
        logical_query=logical,
        model=SimpleNamespace(id="model"),
        deployed_shape=None,
        resolved_measures=[SimpleNamespace(name="__row_count")],
    )
    sql = asyncio.run(source_sql._build_no_columns_sql(bound, object(), "postgres"))
    assert '"source"."dim"' in sql
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("ATTACH DATABASE ':memory:' AS source")
        connection.execute("CREATE TABLE source.fact (dim_id INTEGER)")
        connection.execute("CREATE TABLE source.dim (id INTEGER)")
        connection.executemany(
            "INSERT INTO source.fact VALUES (?)", [(10,), (10,), (20,)]
        )
        connection.execute("INSERT INTO source.dim VALUES (10)")
        assert connection.execute(sql).fetchone()[0] == 2
    finally:
        connection.close()


def test_source_constant_route_uses_population_defining_from_clause(monkeypatch):
    fact = SimpleNamespace(id="fact", physical_name="source.fact", alias="fact")
    dim = SimpleNamespace(id="dim", physical_name="source.dim", alias="dim")
    fact_col = SimpleNamespace(
        id="fact-key", model_table_id="fact", column_name="dim_id", data_type="integer",
    )
    dim_col = SimpleNamespace(
        id="dim-key", model_table_id="dim", column_name="id", data_type="integer",
    )
    join = SimpleNamespace(
        id="join", left_table_id="fact", right_table_id="dim",
        left_column_id="fact-key", right_column_id="dim-key",
        join_type="inner", population_participation="population_defining",
    )
    monkeypatch.setattr(
        source_sql,
        "_load_model_graph",
        lambda *args, **kwargs: _async_result(
            ({"fact": fact, "dim": dim}, [join],
             {"fact-key": fact_col, "dim-key": dim_col}, {})
        ),
    )
    from src.rewrite import snapshot_graph_resolvers
    monkeypatch.setattr(
        snapshot_graph_resolvers,
        "resolve_base_table",
        lambda *args, **kwargs: _async_result(fact),
    )
    logical = SimpleNamespace(
        from_tables=["model"], raw_query="SELECT TRUE FROM model",
        input_dialect="postgres", limit=None, offset=None,
        select_expressions=[],
    )
    bound = SimpleNamespace(
        logical_query=logical,
        model=SimpleNamespace(id="model"),
        deployed_shape=None,
        resolved_measures=[],
    )
    sql = asyncio.run(source_sql._build_no_columns_sql(bound, object(), "postgres"))
    assert '"source"."dim"' in sql


def _async_result(value):
    async def _result():
        return value
    return _result()
