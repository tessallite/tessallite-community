"""Stage-4 relabel binder tests (spec §7.2 resolver, §7.3 alignment).

Pure tests over the dimension->relationship resolver and stable-leaf binder in
``src.semantic.derived_relabel_binder``. They pin the fail-closed rules: a bare
detail dimension resolves ONLY to exactly one enabled BIJECTION relationship with
all stable ids; disabled/N:1/null/ambiguous/cross-relation cases resolve to None
(route to source). No DB, no live snapshot — a hand-built DeployedShape stands in
for the pinned snapshot.
"""
from __future__ import annotations

import types
import uuid

import pytest

from src.semantic.derived_relabel_binder import (
    bind_expression_leaves,
    resolve_dimension_relabel,
    resolve_leaf_column_ids,
)
from src.semantic.snapshot_resolver import DeployedShape
from shared.semantic.derived_expression import CanonicalLeaf

pytestmark = pytest.mark.unit


def _shape(**over):
    base = dict(
        measures=[], dimensions=[], hidden_column_ids=set(),
        physical_columns_all=set(), physical_columns_visible=set(),
    )
    base.update(over)
    return DeployedShape(**base)


# Stable ids used across the fixture model.
KEY_COL = "col-country-id"
DETAIL_COL = "col-country-name"
TABLE = "tbl-dim-country"
OWN_DIM = "dim-country"
QUERY_DIM = "dim-country-name"
REL = "rel-1"


def _bijection_shape(**rel_over):
    rel = dict(
        id=REL, dimension_id=OWN_DIM, key_column_id=KEY_COL,
        detail_column_id=DETAIL_COL, cardinality="BIJECTION",
        null_policy="REJECT_NULL", enabled=True, declaration_hash="dh1",
    )
    rel.update(rel_over)
    owning_dim = types.SimpleNamespace(id=OWN_DIM, source_column_id=KEY_COL)
    return _shape(
        attribute_relationships=[rel],
        dimensions_by_id={OWN_DIM: owning_dim},
        columns_by_id={
            KEY_COL: {"id": KEY_COL, "model_table_id": TABLE, "column_name": "country_id"},
            DETAIL_COL: {"id": DETAIL_COL, "model_table_id": TABLE, "column_name": "country_name"},
        },
    )


def _query_dim():
    return types.SimpleNamespace(id=QUERY_DIM, source_column_id=DETAIL_COL)


def _resolve(shape, **over):
    kwargs = dict(
        query_dimension=_query_dim(), shape=shape, group_ordinal=0,
        select_ordinals=[0], requested_name="country_name", output_alias=None,
    )
    kwargs.update(over)
    return resolve_dimension_relabel(**kwargs)


# ---------------------------------------------------------------------------
# §7.2 resolver — positive + fail-closed cases.
# ---------------------------------------------------------------------------


def test_bijection_detail_resolves_with_all_stable_ids():
    r = _resolve(_bijection_shape())
    assert r is not None
    assert r.relationship_id == REL
    assert r.attribute_key == f"attr:{REL}"
    assert r.owning_dimension_id == OWN_DIM
    assert r.key_column_id == KEY_COL
    assert r.detail_column_id == DETAIL_COL
    assert r.cardinality == "BIJECTION"
    assert r.declaration_hash == "dh1"


def test_disabled_relationship_fails_closed():
    assert _resolve(_bijection_shape(enabled=False)) is None


def test_n_to_1_is_not_stage4():
    assert _resolve(_bijection_shape(cardinality="FUNCTIONAL_N_TO_1")) is None


def test_non_reject_null_policy_fails_closed():
    assert _resolve(_bijection_shape(null_policy="ALLOW_NULL")) is None


def test_owning_dimension_key_mismatch_fails_closed():
    shape = _bijection_shape()
    # The owning dimension's source column no longer equals the relationship key.
    shape.dimensions_by_id[OWN_DIM] = types.SimpleNamespace(id=OWN_DIM, source_column_id="other")
    assert _resolve(shape) is None


def test_key_and_detail_in_different_relations_fails_closed():
    shape = _bijection_shape()
    shape.columns_by_id[DETAIL_COL]["model_table_id"] = "other-table"  # cross-relation
    assert _resolve(shape) is None


def test_two_eligible_relationships_are_ambiguous():
    shape = _bijection_shape()
    # A second enabled relationship on the same detail column -> ambiguous -> None.
    shape.attribute_relationships.append(dict(
        id="rel-2", dimension_id=OWN_DIM, key_column_id=KEY_COL,
        detail_column_id=DETAIL_COL, cardinality="BIJECTION",
        null_policy="REJECT_NULL", enabled=True, declaration_hash="dh2",
    ))
    assert _resolve(shape) is None


def test_dimension_with_no_source_column_fails_closed():
    r = resolve_dimension_relabel(
        query_dimension=types.SimpleNamespace(id=QUERY_DIM, source_column_id=None),
        shape=_bijection_shape(), group_ordinal=0, select_ordinals=[0],
        requested_name="x", output_alias=None,
    )
    assert r is None


# ---------------------------------------------------------------------------
# §7.3 stable-leaf binding — qualified vs unqualified + all-or-nothing.
# ---------------------------------------------------------------------------


def test_qualified_leaf_binds_through_from_scope_and_qualified_index():
    shape = _shape(
        qualified_column_ids={(TABLE, "created_at"): "col-x"},
        columns_by_id={"col-x": {"id": "col-x", "model_table_id": TABLE, "column_name": "created_at"}},
    )
    alias_map = {"o": TABLE}
    out = resolve_leaf_column_ids(
        [CanonicalLeaf(qualifier="o", name="created_at")], shape=shape, alias_map=alias_map,
    )
    assert out == [(TABLE, "col-x")]


def test_qualifier_absent_from_scope_fails_closed():
    shape = _shape(qualified_column_ids={(TABLE, "created_at"): "col-x"})
    out = resolve_leaf_column_ids(
        [CanonicalLeaf(qualifier="unknown", name="created_at")], shape=shape, alias_map={},
    )
    assert out is None  # unqualified alias not in FROM scope


def test_unqualified_ambiguous_name_fails_closed():
    # physical_column_ids poisons ambiguous names by omission, so a leaf whose name
    # is not present binds nothing -> all-or-nothing withholds the whole tuple.
    shape = _shape(physical_column_ids={})  # 'amt' omitted (ambiguous)
    out = bind_expression_leaves(
        [CanonicalLeaf(qualifier=None, name="amt")],
        model_id="m", shape=shape, alias_map={},
    )
    assert out is None


def test_one_missing_leaf_withholds_all_lineage():
    shape = _shape(
        physical_column_ids={"a": "col-a"},
        columns_by_id={"col-a": {"id": "col-a", "model_table_id": TABLE, "column_name": "a"}},
    )
    # 'a' resolves but 'b' does not -> the whole tuple is withheld.
    out = bind_expression_leaves(
        [CanonicalLeaf(qualifier=None, name="a"), CanonicalLeaf(qualifier=None, name="b")],
        model_id="m", shape=shape, alias_map={},
    )
    assert out is None


# ---------------------------------------------------------------------------
# Fable R2 #1: a SELECT ordinal claimed by two group keys poisons the binding.
# ---------------------------------------------------------------------------


def test_multiply_matched_select_ordinal_poisons_binding():
    """§3.4: two grain dimensions whose name sets overlap both claim the same SELECT
    ordinal — the binding must be POISONED (no relabels/projections) so the query
    falls through to source, never a silently mislabelled serve."""
    import types as _t
    from src.semantic.binder import _bind_attribute_relabels

    # Two dimensions: A named "customer" (source col "customer_name"), B named
    # "customer_name". A SELECT item "customer_name" is claimed by BOTH (A via its
    # source-column name, B via its dimension name).
    dim_a = _t.SimpleNamespace(id="dimA", name="customer", source_column_id="colA")
    dim_b = _t.SimpleNamespace(id="dimB", name="customer_name", source_column_id="colB")
    shape = _shape(
        attribute_relationships=[{"id": "x", "enabled": True, "detail_column_id": "none",
                                  "cardinality": "BIJECTION"}],  # non-empty -> not skipped
        columns_by_id={
            "colA": {"id": "colA", "model_table_id": "t", "column_name": "customer_name"},
            "colB": {"id": "colB", "model_table_id": "t", "column_name": "customer_name"},
        },
    )

    class _LQ:
        grain = ["customer", "customer_name"]
        select_expressions = [
            _t.SimpleNamespace(classification="passthrough", raw_text="customer_name",
                               inner_column="customer_name", alias=None),
            _t.SimpleNamespace(classification="analytical", raw_text="SUM(rev)",
                               inner_column="rev", agg_function="sum", alias="r"),
        ]

    relabels, projections = _bind_attribute_relabels(
        query=_LQ(), deployed_shape=shape,
        dimension_map={"customer": dim_a, "customer_name": dim_b},
        dimension_map_lower={"customer": dim_a, "customer_name": dim_b},
    )
    # Both grain keys claim SELECT ordinal 0 -> poisoned -> empty binding.
    assert relabels == []
    assert projections == []


# ---------------------------------------------------------------------------
# Bug-7873b: build_from_scope_alias_map poisons on ANY conflicting alias reuse,
# including a reuse where one side is an UNKNOWN deployed table.
# ---------------------------------------------------------------------------


def test_alias_map_poisons_conflict_with_unknown_table():
    from src.semantic.derived_relabel_binder import build_from_scope_alias_map

    # ``u`` is bound once to an unknown table (not in the deployed index) and once to
    # a known table ``orders``. The reuse is ambiguous, so ``u`` must be POISONED —
    # a qualified leaf ``u.col`` then fails closed rather than binding to orders.
    shape = _shape(table_name_ids={"orders": "tbl-orders"})
    q = types.SimpleNamespace(
        raw_query="SELECT 1 FROM unknown_t u JOIN orders u ON true",
        input_dialect="postgres",
    )
    amap = build_from_scope_alias_map(q, shape)
    assert "u" not in amap


def test_alias_map_keeps_unambiguous_known_token():
    from src.semantic.derived_relabel_binder import build_from_scope_alias_map

    shape = _shape(table_name_ids={"orders": "tbl-orders"})
    q = types.SimpleNamespace(
        raw_query="SELECT 1 FROM orders", input_dialect="postgres",
    )
    amap = build_from_scope_alias_map(q, shape)
    assert amap.get("orders") == "tbl-orders"
