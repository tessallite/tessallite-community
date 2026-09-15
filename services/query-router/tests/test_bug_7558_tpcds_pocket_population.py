"""Bug-7558 — the hosted TPC-DS pocket must prove its extra joins lossless."""
from __future__ import annotations

from src.routing.pocket_population import JoinEdge, ModelJoinGraph, population_proven


FACT = "store_sales"
DIMENSIONS = ("date_dim", "item", "store", "customer", "promotion")
DIMENSION_KEYS = {
    "date_dim": "d_date_sk",
    "item": "i_item_sk",
    "store": "s_store_sk",
    "customer": "c_customer_sk",
    "promotion": "p_promo_sk",
}
FACT_KEYS = {
    "date_dim": "ss_sold_date_sk",
    "item": "ss_item_sk",
    "store": "ss_store_sk",
    "customer": "ss_customer_sk",
    "promotion": "ss_promo_sk",
}


def _tpcds_graph(primary_keys: set[str]) -> ModelJoinGraph:
    edges = tuple(
        JoinEdge(
            left_table_id=FACT,
            right_table_id=dimension,
            left_column_id=FACT_KEYS[dimension],
            right_column_id=DIMENSION_KEYS[dimension],
            join_type="left",
        )
        for dimension in DIMENSIONS
    )
    column_owners = {
        **{column: FACT for column in FACT_KEYS.values()},
        **{column: table for table, column in DIMENSION_KEYS.items()},
    }
    return ModelJoinGraph(
        table_ids=frozenset({FACT, *DIMENSIONS}),
        edges=edges,
        pk_column_ids=frozenset(primary_keys),
        table_id_by_column_id=column_owners,
    )


def test_bug7558_tpcds_pocket_is_rejected_without_dimension_key_metadata():
    query_tables = {FACT, "date_dim", "item"}

    assert population_proven(
        graph=_tpcds_graph(set()), query_table_ids=query_tables,
    ) is False


def test_bug7558_tpcds_pocket_is_proven_with_exact_five_sole_dimension_keys():
    query_tables = {FACT, "date_dim", "item"}
    all_keys = set(DIMENSION_KEYS.values())

    assert population_proven(
        graph=_tpcds_graph(all_keys), query_table_ids=query_tables,
    ) is True
    assert population_proven(
        graph=_tpcds_graph(all_keys - {DIMENSION_KEYS["promotion"]}),
        query_table_ids=query_tables,
    ) is False
