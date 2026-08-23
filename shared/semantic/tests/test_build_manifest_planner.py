"""Producer-derived contract + Gap-6 build tests (spec §7.1, §7.5).

The MANDATORY §7.1 producer-derived contract: the build-side canonicaliser (via the
build planner) and the query-side parser->canonicaliser yield IDENTICAL ordered leaf
tuples for identical expressions. This is the one guard that keeps the producer's
grain-key ``input_column_ids`` byte-aligned with the query binder's leaf ids, so an
exact-identity serve can never bind a query over relation A to relation B's key.
"""
from __future__ import annotations

import types
import uuid

import pytest

from shared.semantic.build_manifest_planner import build_grain_key_manifest
from shared.semantic.derived_expression import (
    canonicalise_sql,
    enumerate_canonical_leaves,
)
from shared.semantic.grain_resolver import ResolvedAggregateLayout, ResolvedGrainCol

pytestmark = pytest.mark.unit


def _grain(expr=None, phys="c", dim_id=None, col_name=None):
    return ResolvedGrainCol(
        logical_name="g",
        dimension_id=dim_id or uuid.uuid4(),
        source_table_id=None if expr else uuid.uuid4(),
        source_column_name=col_name,
        physical_col_name=phys,
        source_expression=expr,
    )


# ---------------------------------------------------------------------------
# §7.1 producer-derived contract — identical ordered leaf tuples.
# ---------------------------------------------------------------------------


def test_build_and_query_yield_identical_ordered_leaf_ids():
    # The build planner resolves expression-key lineage via ce.input_columns; the
    # query binder resolves via enumerate_canonical_leaves on the same expression.
    # Both must produce the SAME ordered column-id tuple.
    expr = "DATE_TRUNC('month', order_date)"
    col_id = str(uuid.uuid4())
    col_id_by_name = {"order_date": col_id}

    # Build side: the grain-key manifest.
    layout = ResolvedAggregateLayout(
        grain_cols=[_grain(expr=expr, phys="order_month")], measure_cols=[]
    )
    keys = build_grain_key_manifest(
        layout=layout, dimension_by_id={}, column_id_by_name=col_id_by_name,
    )
    build_lineage = keys[0].input_column_ids

    # Query side: the ordered leaves resolved to the SAME id map.
    ce = canonicalise_sql(expr, input_dialect="postgres")
    query_lineage = [col_id_by_name[lf.name] for lf in ce.leaves]

    assert build_lineage == query_lineage == [col_id]
    # And the fingerprint the producer stores equals the query-side fingerprint.
    assert keys[0].expression_fingerprint == ce.fingerprint


def test_repeated_leaves_deduped_first_seen_and_producer_consumer_agree():
    # A repeated leaf appears ONCE (first-seen dedup), and the SAME shared
    # enumerator drives both the build planner lineage and the query binder — so
    # whatever the walk order is, both sides produce the IDENTICAL ordered tuple.
    import sqlglot
    expr = "(order_date + order_date) - ship_date"
    node = sqlglot.parse_one(expr, read="postgres")
    leaves = enumerate_canonical_leaves(node)
    names = [lf.name for lf in leaves]
    # Deduped to the two distinct columns (order_date once, ship_date once).
    assert sorted(names) == ["order_date", "ship_date"]
    assert len(names) == 2
    # Producer (canonicalise_sql -> ce.leaves) and this direct enumeration agree.
    ce = canonicalise_sql(expr, input_dialect="postgres")
    assert [lf.name for lf in ce.leaves] == names


def test_same_named_leaves_different_qualifiers_stay_distinct():
    # orders.created_at and returns.created_at are the SAME name but DISTINCT leaves
    # (different qualifiers) — the ordered tuple keeps both, in order.
    expr = "COALESCE(orders.created_at, returns.created_at)"
    leaves = enumerate_canonical_leaves(
        __import__("sqlglot").parse_one(expr, read="postgres")
    )
    assert leaves[0].qualifier == "orders" and leaves[0].name == "created_at"
    assert leaves[1].qualifier == "returns" and leaves[1].name == "created_at"
    assert len(leaves) == 2


# ---------------------------------------------------------------------------
# §7.5 Gap-6 build tests — grain-key manifest correctness.
# ---------------------------------------------------------------------------


def test_physical_key_lineage_from_deployed_dimension_source_column():
    dim_id = str(uuid.uuid4())
    src_col = str(uuid.uuid4())
    dim = types.SimpleNamespace(id=dim_id, source_column_id=src_col)
    layout = ResolvedAggregateLayout(
        grain_cols=[_grain(dim_id=uuid.UUID(dim_id), col_name="country", phys="country")],
        measure_cols=[],
    )
    key = build_grain_key_manifest(
        layout=layout, dimension_by_id={dim_id: dim}, column_id_by_name={},
    )[0]
    assert key.key_id == f"dim:{dim_id}"
    assert key.kind == "PHYSICAL_COLUMN"
    assert key.input_column_ids == [src_col]
    assert key.source_dimension_id == dim_id


def test_physical_key_without_source_column_is_non_servable():
    # A dimension with no source_column_id yields a key with EMPTY lineage so the
    # query-side exact gate fails closed (never a wrong serve). The key is still
    # emitted (grain manifest completeness -> never grain_keys=[]).
    dim_id = str(uuid.uuid4())
    dim = types.SimpleNamespace(id=dim_id, source_column_id=None)
    layout = ResolvedAggregateLayout(
        grain_cols=[_grain(dim_id=uuid.UUID(dim_id), col_name="c", phys="c")],
        measure_cols=[],
    )
    key = build_grain_key_manifest(
        layout=layout, dimension_by_id={dim_id: dim}, column_id_by_name={},
    )[0]
    assert key.input_column_ids == []
    assert key.key_id == f"dim:{dim_id}"


def test_expression_key_unresolved_leaf_poisons_lineage_all_or_nothing():
    # If ANY leaf name is unresolved in the id map, the WHOLE lineage is withheld
    # (empty) — never a partial lineage the consumer could mis-match.
    expr = "DATE_TRUNC('month', order_date)"
    layout = ResolvedAggregateLayout(
        grain_cols=[_grain(expr=expr, phys="order_month")], measure_cols=[]
    )
    key = build_grain_key_manifest(
        layout=layout, dimension_by_id={}, column_id_by_name={},  # empty -> unresolved
    )[0]
    assert key.input_column_ids == []
    assert key.key_id.startswith("expr:")


def test_qualified_expression_leaf_poisons_lineage_symmetrically():
    # Fable R1 #3: a build-side artifact expression with a QUALIFIED leaf cannot be
    # resolved by the model-wide bare-name map (no build FROM scope in v1), so its
    # lineage is withheld (empty) -> fail closed. This is SYMMETRIC with the query
    # side: both refuse, so no silent producer/consumer mismatch (never a wrong
    # serve; a qualified artifact expression simply routes to source in v1).
    expr = "COALESCE(orders.amount, returns.amount)"
    layout = ResolvedAggregateLayout(
        grain_cols=[_grain(expr=expr, phys="c")], measure_cols=[]
    )
    key = build_grain_key_manifest(
        layout=layout, dimension_by_id={},
        column_id_by_name={"amount": "col-amt"},  # bare name present, but leaf is qualified
    )[0]
    assert key.input_column_ids == []  # withheld -> fail closed
    assert key.key_id.startswith("expr:")
