"""Bug-8664 / Bug-8637 — the AGGREGATE route's row-population gate.

Bug-8664: ``find_best_aggregate`` could match and serve an aggregate whose
materialised row population differs from what the source route would return for
the same query. An aggregate CTAS joins ``{anchor} + grain tables + measure
tables`` and GROUPs; the query is compiled with join elision. When a relation
only the AGGREGATE joined drops rows (INNER) or fans them out (a non-unique far
key), every re-aggregated SUM/COUNT served from it is silently wrong. The pocket
route has refused this since Bug-8580; the aggregate route had nothing.

Bug-8637: on a CYCLIC join graph the FROM clause is a spanning-tree CHOICE, and
the served source SQL and the aggregate CTAS provably make different choices
(measured 192/300 random id assignments on one legal snowflake diamond) — same
question, different row population, permanently. The registry's narrower interim
fix is exactly this gate: an aggregate is not used on a cyclic join graph.

Every test drives the REAL ``find_best_aggregate`` against a real deployed
snapshot shape, so a gate that exists but is not wired fails here.
"""
from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from conftest import make_agg_col, make_bound_query, make_dimension, make_measure
from src.routing.aggregate_matcher import AggregateSkipReason, find_best_aggregate
from src.routing.aggregate_population import (
    AggregateObjectIndex,
    aggregate_needed_table_ids,
    invalidate_aggregate_population_cache,
)
from src.routing.pocket_matcher import (
    invalidate_model_join_graph_cache,
    invalidate_model_table_cache,
)

pytestmark = pytest.mark.unit

_FACT = "t-fact"
_DIM_A = "t-dim-a"
_DIM_B = "t-dim-b"
_SHARED = "t-dim-shared"

_C_AMOUNT = "c-amount"
_C_A_LABEL = "c-a-label"
_C_B_LABEL = "c-b-label"
_C_SHARED_LABEL = "c-shared-label"
_C_FACT_A_FK = "c-fact-a-fk"
_C_A_PK = "c-a-pk"
_C_FACT_B_FK = "c-fact-b-fk"
_C_B_PK = "c-b-pk"
_C_A_SH_FK = "c-a-sh-fk"
_C_SH_PK = "c-sh-pk"
_C_B_SH_FK = "c-b-sh-fk"
_C_SH_PK2 = "c-sh-pk2"


@pytest.fixture(autouse=True)
def _clear_caches():
    invalidate_model_table_cache()
    invalidate_model_join_graph_cache()
    invalidate_aggregate_population_cache()
    yield
    invalidate_model_table_cache()
    invalidate_model_join_graph_cache()
    invalidate_aggregate_population_cache()


def _col(cid, table, name, *, pk=False):
    return {
        "id": cid, "model_table_id": table, "column_name": name,
        "is_primary_key": pk,
    }


def _table(tid, name, table_type="dim_detail"):
    return {"id": tid, "physical_name": f"public.{name}", "alias": name,
            "table_type": table_type}


def _dim(name, column_id):
    return types.SimpleNamespace(
        id=f"d-{name}", name=name, source_column_id=column_id,
        user_defined_attribute_id=None, hierarchy_id=None,
    )


def _measure(name, column_id, *, expression=None):
    return types.SimpleNamespace(
        id=f"m-{name}", name=name, source_column_id=column_id,
        expression=expression,
    )


def _star_shape(*, a_join: str, b_join: str = "left", a_pk: bool = True):
    """fact --a_join--> dim_a, fact --b_join--> dim_b. A plain two-arm star."""
    return types.SimpleNamespace(
        tables_by_id={
            _FACT: _table(_FACT, "fact_sales", "fact"),
            _DIM_A: _table(_DIM_A, "dim_a"),
            _DIM_B: _table(_DIM_B, "dim_b"),
        },
        columns_by_id={
            _C_AMOUNT: _col(_C_AMOUNT, _FACT, "amount"),
            _C_FACT_A_FK: _col(_C_FACT_A_FK, _FACT, "a_id"),
            _C_FACT_B_FK: _col(_C_FACT_B_FK, _FACT, "b_id"),
            _C_A_PK: _col(_C_A_PK, _DIM_A, "id", pk=a_pk),
            _C_A_LABEL: _col(_C_A_LABEL, _DIM_A, "a_label"),
            _C_B_PK: _col(_C_B_PK, _DIM_B, "id", pk=True),
            _C_B_LABEL: _col(_C_B_LABEL, _DIM_B, "b_label"),
        },
        join_rows=[
            {"left_table_id": _FACT, "right_table_id": _DIM_A,
             "left_column_id": _C_FACT_A_FK, "right_column_id": _C_A_PK,
             "join_type": a_join},
            {"left_table_id": _FACT, "right_table_id": _DIM_B,
             "left_column_id": _C_FACT_B_FK, "right_column_id": _C_B_PK,
             "join_type": b_join},
        ],
        user_defined_attribute_rows=[],
        dimensions=[_dim("a_label", _C_A_LABEL), _dim("b_label", _C_B_LABEL)],
        measures=[_measure("amount", _C_AMOUNT)],
        hierarchy_rows=[],
    )


def _diamond_shape():
    """The Bug-8637 shape, verbatim: two equally short fact->dim_shared paths.

    ``fact -(inner)-> dim_a -(inner)-> dim_shared`` and
    ``fact -(left)-> dim_b -(left)-> dim_shared``. Both builders can reach
    ``dim_shared``, they pick different spanning trees, and the aggregate then
    groups on a different key than source computes.
    """
    return types.SimpleNamespace(
        tables_by_id={
            _FACT: _table(_FACT, "fact_sales", "fact"),
            _DIM_A: _table(_DIM_A, "dim_a"),
            _DIM_B: _table(_DIM_B, "dim_b"),
            _SHARED: _table(_SHARED, "dim_shared"),
        },
        columns_by_id={
            _C_AMOUNT: _col(_C_AMOUNT, _FACT, "amount"),
            _C_FACT_A_FK: _col(_C_FACT_A_FK, _FACT, "a_id"),
            _C_FACT_B_FK: _col(_C_FACT_B_FK, _FACT, "b_id"),
            _C_A_PK: _col(_C_A_PK, _DIM_A, "id", pk=True),
            _C_A_SH_FK: _col(_C_A_SH_FK, _DIM_A, "sh_id"),
            _C_B_PK: _col(_C_B_PK, _DIM_B, "id", pk=True),
            _C_B_SH_FK: _col(_C_B_SH_FK, _DIM_B, "sh_id"),
            _C_SH_PK: _col(_C_SH_PK, _SHARED, "id", pk=True),
            _C_SH_PK2: _col(_C_SH_PK2, _SHARED, "id2"),
            _C_SHARED_LABEL: _col(_C_SHARED_LABEL, _SHARED, "shared_key"),
        },
        join_rows=[
            {"left_table_id": _FACT, "right_table_id": _DIM_A,
             "left_column_id": _C_FACT_A_FK, "right_column_id": _C_A_PK,
             "join_type": "left"},
            {"left_table_id": _FACT, "right_table_id": _DIM_B,
             "left_column_id": _C_FACT_B_FK, "right_column_id": _C_B_PK,
             "join_type": "left"},
            {"left_table_id": _DIM_A, "right_table_id": _SHARED,
             "left_column_id": _C_A_SH_FK, "right_column_id": _C_SH_PK,
             "join_type": "left"},
            {"left_table_id": _DIM_B, "right_table_id": _SHARED,
             "left_column_id": _C_B_SH_FK, "right_column_id": _C_SH_PK2,
             "join_type": "left"},
        ],
        user_defined_attribute_rows=[],
        dimensions=[
            _dim("shared_key", _C_SHARED_LABEL),
            _dim("a_key", _C_A_PK),
        ],
        measures=[_measure("amount", _C_AMOUNT)],
        hierarchy_rows=[],
    )


def _aggregate(grain, measures, *, agg_id="agg-8664"):
    return types.SimpleNamespace(
        id=agg_id,
        grain=list(grain),
        columns=[make_agg_col(m) for m in measures],
        status="active",
        last_refreshed_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        physical_table_name="agg_table",
        target_schema="aggregates",
        persona_id=None,
        built_for_version_id="v1",
        built_for_epoch=0,
        is_stale=False,
    )


async def _match(bound_query, shape, aggregate):
    with patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        new=AsyncMock(return_value=shape),
    ), patch(
        "src.routing.aggregate_matcher.load_active_aggregates",
        new=AsyncMock(return_value=[aggregate]),
    ):
        return await find_best_aggregate(bound_query, AsyncMock())


def _query_over_a_label_only():
    """``SELECT a_label, SUM(amount) ... GROUP BY a_label`` — dim_b is elided."""
    dim = make_dimension("a_label")
    dim.source_column_id = _C_A_LABEL
    measure = make_measure("amount")
    measure.source_column_id = _C_AMOUNT
    bq = make_bound_query(
        [dim], [measure],
        raw_sql="SELECT a_label, SUM(amount) FROM test_model GROUP BY a_label",
    )
    return bq, measure


# ---------------------------------------------------------------------------
# Bug-8664 — the defect
# ---------------------------------------------------------------------------


async def test_inner_joined_extra_grain_relation_refuses_the_aggregate():
    """The reproduction. The aggregate's grain covers ``a_label`` AND
    ``b_label``, so its CTAS joined ``dim_b`` too. That join is INNER, so the
    aggregate holds only fact rows with a ``dim_b`` partner. The query groups by
    ``a_label`` alone — its own plan never joins ``dim_b`` and keeps every fact
    row. Serving the aggregate understates the SUM."""
    bq, measure = _query_over_a_label_only()
    shape = _star_shape(a_join="left", b_join="inner")
    agg = _aggregate(["a_label", "b_label"], [measure])

    result = await _match(bq, shape, agg)

    assert result.aggregate is None
    assert AggregateSkipReason.JOIN_POPULATION_MISMATCH in (result.skip_reasons or [])


async def test_row_preserving_extra_grain_relation_still_serves():
    """Over-correction guard. The same rollup with a LEFT join onto ``dim_b``'s
    sole declared primary key drops and duplicates nothing, so the aggregate is
    exact and MUST keep accelerating. Without this the gate would be an
    acceleration outage rather than a correctness fix."""
    bq, measure = _query_over_a_label_only()
    shape = _star_shape(a_join="left", b_join="left")
    agg = _aggregate(["a_label", "b_label"], [measure])

    result = await _match(bq, shape, agg)

    assert result.aggregate is agg
    assert AggregateSkipReason.JOIN_POPULATION_MISMATCH not in (
        result.skip_reasons or []
    )


async def test_fanning_extra_grain_relation_refuses_the_aggregate():
    """A LEFT join whose far key is NOT a declared primary key can match many
    rows per fact row, inflating every SUM/COUNT — the Bug-8580 defect in the
    opposite direction. Here the ELIDED relation is ``dim_a``."""
    dim = make_dimension("b_label")
    dim.source_column_id = _C_B_LABEL
    measure = make_measure("amount")
    measure.source_column_id = _C_AMOUNT
    bq = make_bound_query(
        [dim], [measure],
        raw_sql="SELECT b_label, SUM(amount) FROM test_model GROUP BY b_label",
    )
    shape = _star_shape(a_join="left", b_join="left", a_pk=False)
    agg = _aggregate(["a_label", "b_label"], [measure])

    result = await _match(bq, shape, agg)

    assert result.aggregate is None
    assert AggregateSkipReason.JOIN_POPULATION_MISMATCH in (result.skip_reasons or [])


async def test_exact_grain_query_over_an_inner_star_still_serves():
    """When the query references every relation the aggregate joined there is no
    elided relation at all, so an INNER edge is shared by both plans and the
    populations are identical. The gate must not refuse this."""
    dim_a = make_dimension("a_label")
    dim_a.source_column_id = _C_A_LABEL
    dim_b = make_dimension("b_label")
    dim_b.source_column_id = _C_B_LABEL
    measure = make_measure("amount")
    measure.source_column_id = _C_AMOUNT
    bq = make_bound_query(
        [dim_a, dim_b], [measure],
        raw_sql=(
            "SELECT a_label, b_label, SUM(amount) FROM test_model "
            "GROUP BY a_label, b_label"
        ),
    )
    shape = _star_shape(a_join="left", b_join="inner")
    agg = _aggregate(["a_label", "b_label"], [measure])

    result = await _match(bq, shape, agg)

    assert result.aggregate is agg


async def test_legacy_join_token_inside_the_plan_refuses_the_aggregate():
    """A legacy ``many_to_one`` token does not say which relation it preserves,
    and the two plans start from different base relations, so the same edge can
    preserve opposite sides in each. Unproven."""
    bq, measure = _query_over_a_label_only()
    shape = _star_shape(a_join="many_to_one", b_join="left")
    agg = _aggregate(["a_label", "b_label"], [measure])

    result = await _match(bq, shape, agg)

    assert result.aggregate is None
    assert AggregateSkipReason.JOIN_POPULATION_MISMATCH in (result.skip_reasons or [])


@pytest.mark.real_population_resolvers
async def test_unresolvable_snapshot_refuses_every_aggregate():
    """Fail closed. When the model's join graph cannot be resolved the gate has
    no basis for a verdict and must refuse, never admit."""
    bq, measure = _query_over_a_label_only()
    agg = _aggregate(["a_label"], [measure])

    result = await _match(bq, None, agg)

    assert result.aggregate is None
    assert AggregateSkipReason.JOIN_POPULATION_MISMATCH in (result.skip_reasons or [])


# ---------------------------------------------------------------------------
# Bug-8637 — cyclic join graph
# ---------------------------------------------------------------------------


async def test_cyclic_join_graph_refuses_the_aggregate():
    """Bug-8637's proven shape. Two equally short fact->dim_shared paths make the
    FROM clause a spanning-tree CHOICE; the source route and the aggregate CTAS
    provably choose differently (192/300 random id assignments), so the same
    ``GROUP BY shared_key`` returns ``S1 -> 100`` from source and
    ``(null) -> 100`` from the aggregate. Refuse the aggregate on a cyclic
    graph."""
    dim = make_dimension("shared_key")
    dim.source_column_id = _C_SHARED_LABEL
    measure = make_measure("amount")
    measure.source_column_id = _C_AMOUNT
    bq = make_bound_query(
        [dim], [measure],
        raw_sql=(
            "SELECT shared_key, SUM(amount) FROM test_model GROUP BY shared_key"
        ),
    )
    agg = _aggregate(["shared_key"], [measure])

    result = await _match(bq, _diamond_shape(), agg)

    assert result.aggregate is None
    assert AggregateSkipReason.JOIN_POPULATION_MISMATCH in (result.skip_reasons or [])


async def test_a_cycle_OUTSIDE_the_plan_bound_still_refuses_the_aggregate():
    """The leg only ``component_is_acyclic`` can catch, and the reason the check
    is component-wide rather than plan-wide.

    The aggregate's grain is ``a_key`` alone, so its plan bound is
    ``{fact, dim_a}`` — a two-node tree that ``population_proven``'s own
    per-component tree test accepts. The second fact->dim_shared path lives
    entirely OUTSIDE that bound, and the source route's traversal can still
    reach into it (``_build_joined_from_clause``'s intermediates leg picks the
    first adjacent unjoined relation, not one restricted to the aggregate's
    plan). Bug-8637's remedy is stated as "an aggregate is simply not used on a
    cyclic join graph", so the refusal is component-wide.

    This is deliberately CONSERVATIVE: for this particular query the two plans
    would in fact agree. The cost is bounded — every model in the shipped seed
    is a forest — and the alternative is leaving the divergence open under the
    new gate's nose.
    """
    dim = make_dimension("a_key")
    dim.source_column_id = _C_A_PK
    measure = make_measure("amount")
    measure.source_column_id = _C_AMOUNT
    bq = make_bound_query(
        [dim], [measure],
        raw_sql="SELECT a_key, SUM(amount) FROM test_model GROUP BY a_key",
    )
    agg = _aggregate(["a_key"], [measure])

    result = await _match(bq, _diamond_shape(), agg)

    assert result.aggregate is None
    assert AggregateSkipReason.JOIN_POPULATION_MISMATCH in (result.skip_reasons or [])


async def test_a_parallel_edge_pair_is_a_cycle_too():
    """Two ``Join`` rows over the SAME pair of relations are two edges the FROM
    clause must choose between, even though an adjacency SET would collapse them
    into one. Counted as a multiset so the choice is caught."""
    from shared.semantic.aggregate_plan_bound import component_is_acyclic

    assert component_is_acyclic(
        seeds={_FACT}, table_ids={_FACT, _DIM_A},
        edges=[(_FACT, _DIM_A)],
    ) is True
    assert component_is_acyclic(
        seeds={_FACT}, table_ids={_FACT, _DIM_A},
        edges=[(_FACT, _DIM_A), (_FACT, _DIM_A)],
    ) is False


# ---------------------------------------------------------------------------
# The needed-relation resolver's fail-closed contract
# ---------------------------------------------------------------------------


def _index():
    return AggregateObjectIndex(
        dimension_names=frozenset({"a_label", "b_label", "unbound"}),
        table_by_dimension_name={"a_label": _DIM_A, "b_label": _DIM_B},
        measure_id_by_name={"amount": "m-amount", "calc": "m-calc"},
        table_by_measure_id={"m-amount": _FACT},
        expression_by_measure_id={"m-calc": 'measure("amount") * 2'},
    )


def test_needed_tables_are_the_grain_and_measure_relations():
    agg = types.SimpleNamespace(
        grain=["a_label"],
        columns=[types.SimpleNamespace(measure=_measure("amount", _C_AMOUNT))],
    )
    assert aggregate_needed_table_ids(agg, _index()) == frozenset({_DIM_A, _FACT})


def test_an_undeclared_grain_name_is_unproven():
    """``resolve_aggregate_layout`` RAISES on an unknown grain name, so such an
    aggregate cannot even be refreshed and its plan is unknowable here."""
    agg = types.SimpleNamespace(grain=["ghost"], columns=[])
    assert aggregate_needed_table_ids(agg, _index()) is None


def test_an_undeclared_measure_is_unproven():
    agg = types.SimpleNamespace(
        grain=[],
        columns=[types.SimpleNamespace(measure=_measure("ghost", None))],
    )
    assert aggregate_needed_table_ids(agg, _index()) is None


def test_a_measure_free_column_contributes_nothing():
    """``__row_count`` binds no relation; ``full_refresh`` skips it identically."""
    agg = types.SimpleNamespace(
        grain=["a_label"], columns=[types.SimpleNamespace(measure=None)],
    )
    assert aggregate_needed_table_ids(agg, _index()) == frozenset({_DIM_A})


def test_a_grain_dimension_with_no_relation_contributes_nothing():
    """A declared dimension that resolves to no physical relation contributes
    nothing to the builders' ``needed`` set either — it must not be mistaken for
    a resolution failure, or every UDA-backed grain would stop serving."""
    agg = types.SimpleNamespace(grain=["unbound"], columns=[])
    assert aggregate_needed_table_ids(agg, _index()) == frozenset()


def test_a_calculated_measure_pulls_in_its_references_relations():
    index = _index()
    index.table_by_measure_id["m-calc"] = None  # expression measure, no column
    agg = types.SimpleNamespace(
        grain=[], columns=[types.SimpleNamespace(measure=_measure("calc", None))],
    )
    # The expression references ``amount``, which lives on the fact relation.
    assert aggregate_needed_table_ids(agg, index) == frozenset({_FACT})


def test_a_grain_dimension_whose_declared_column_is_unknown_is_unproven():
    """R1 finding 4. A dimension that DECLARES a source column the graph cannot
    resolve is NOT the same as one that declares no binding: both builders
    resolve that column from live rows and DO join its relation, so treating it
    as "contributes nothing" would under-estimate the plan and let a lossy join
    through. Must fail closed."""
    from src.routing.aggregate_population import UNRESOLVED

    index = _index()
    index.table_by_dimension_name["a_label"] = UNRESOLVED
    agg = types.SimpleNamespace(grain=["a_label"], columns=[])
    assert aggregate_needed_table_ids(agg, index) is None


def test_a_measure_whose_declared_column_is_unknown_is_unproven():
    from src.routing.aggregate_population import UNRESOLVED

    index = _index()
    index.table_by_measure_id["m-amount"] = UNRESOLVED
    agg = types.SimpleNamespace(
        grain=[],
        columns=[types.SimpleNamespace(measure=_measure("amount", _C_AMOUNT))],
    )
    assert aggregate_needed_table_ids(agg, index) is None


def test_an_unresolvable_declared_binding_survives_the_index_builder():
    """The sentinel must be produced by the real builder, not only injected by a
    test: a dimension binding a column id the graph does not carry."""
    from src.routing.aggregate_population import UNRESOLVED, _index_from_rows

    index = _index_from_rows(
        [_dim("ghost_bound", "c-not-in-graph")], [],
        table_by_column_id={_C_A_LABEL: _DIM_A},
        table_by_uda_id={},
    )
    assert index.table_by_dimension_name["ghost_bound"] is UNRESOLVED
    agg = types.SimpleNamespace(grain=["ghost_bound"], columns=[])
    assert aggregate_needed_table_ids(agg, index) is None


def test_a_hierarchy_level_grain_name_resolves_to_its_relation():
    """R1 finding 5. The optimizer's grain vocabulary includes hierarchy LEVELS
    (``calendar_month``, ``date_hierarchy.Year``), not only flat dimensions.
    Reading flat dimensions alone refused every such aggregate forever."""
    from src.routing.aggregate_population import _index_from_rows

    level = types.SimpleNamespace(
        name="date_hierarchy.Year", source_column_id=_C_A_LABEL,
        user_defined_attribute_id=None,
    )
    index = _index_from_rows(
        [level], [], table_by_column_id={_C_A_LABEL: _DIM_A}, table_by_uda_id={},
    )
    agg = types.SimpleNamespace(grain=["date_hierarchy.Year"], columns=[])
    assert aggregate_needed_table_ids(agg, index) == frozenset({_DIM_A})


def test_a_name_shared_by_a_flat_dim_and_a_level_on_another_relation_is_unproven():
    """The two builders disagree about which wins a name collision — the
    optimizer appends levels last so a level wins its dict comprehension, the
    scheduler passes flat dimensions only. Neither answer can be asserted here,
    so refuse."""
    from src.routing.aggregate_population import UNRESOLVED, _index_from_rows

    flat = _dim("Year", _C_A_LABEL)
    level = types.SimpleNamespace(
        name="Year", source_column_id=_C_B_LABEL, user_defined_attribute_id=None,
    )
    index = _index_from_rows(
        [flat, level], [],
        table_by_column_id={_C_A_LABEL: _DIM_A, _C_B_LABEL: _DIM_B},
        table_by_uda_id={},
    )
    assert index.table_by_dimension_name["Year"] is UNRESOLVED
    agg = types.SimpleNamespace(grain=["Year"], columns=[])
    assert aggregate_needed_table_ids(agg, index) is None


def test_a_non_calculated_measure_carrying_an_expression_is_not_parsed():
    """R1 finding 9. Both builders chase references only for
    ``measure_type == "calculated"``. Keying off "has an expression" refused a
    standard measure that merely carries one."""
    from src.routing.aggregate_population import _index_from_rows

    standard = types.SimpleNamespace(
        id="m-plain", name="plain", source_column_id=_C_AMOUNT,
        expression="not parseable as a measure reference",
        measure_type="standard",
    )
    index = _index_from_rows(
        [], [standard],
        table_by_column_id={_C_AMOUNT: _FACT}, table_by_uda_id={},
    )
    assert index.expression_by_measure_id == {}
    agg = types.SimpleNamespace(
        grain=[], columns=[types.SimpleNamespace(measure=standard)],
    )
    assert aggregate_needed_table_ids(agg, index) == frozenset({_FACT})


def test_a_calculated_measure_with_an_empty_expression_is_unproven():
    from src.routing.aggregate_population import _index_from_rows

    calc = types.SimpleNamespace(
        id="m-calc", name="calc", source_column_id=None, expression="",
        measure_type="calculated",
    )
    index = _index_from_rows(
        [], [calc], table_by_column_id={}, table_by_uda_id={},
    )
    agg = types.SimpleNamespace(
        grain=[], columns=[types.SimpleNamespace(measure=calc)],
    )
    assert aggregate_needed_table_ids(agg, index) is None


def test_a_calculated_measure_referencing_another_calculated_measure_is_unproven():
    """External gate finding, 2026-08-05. This resolver follows ONE hop, which
    is exactly what both builders do. Nested calculation is unsupported at
    materialise time today, so ignoring the chain is harmless NOW — but if it is
    ever supported, the CTAS would join the far measure's relation and a one-hop
    resolver would return a plan bound that OMITS it. An under-estimated bound is
    the one direction that yields a wrong number, so refuse."""
    from src.routing.aggregate_population import _index_from_rows

    outer = types.SimpleNamespace(
        id="m-outer", name="outer", source_column_id=None,
        expression='measure("inner_calc") + 1', measure_type="calculated",
    )
    inner = types.SimpleNamespace(
        id="m-inner", name="inner_calc", source_column_id=None,
        expression='measure("amount") * 2', measure_type="calculated",
    )
    base = types.SimpleNamespace(
        id="m-amount", name="amount", source_column_id=_C_AMOUNT,
        expression=None, measure_type="standard",
    )
    index = _index_from_rows(
        [], [outer, inner, base],
        table_by_column_id={_C_AMOUNT: _FACT}, table_by_uda_id={},
    )
    agg = types.SimpleNamespace(
        grain=[], columns=[types.SimpleNamespace(measure=outer)],
    )
    assert aggregate_needed_table_ids(agg, index) is None

    # Control: the SAME index resolves a single-hop calculated measure, so the
    # refusal above is the nesting and not the fixture.
    single_hop = types.SimpleNamespace(
        grain=[], columns=[types.SimpleNamespace(measure=inner)],
    )
    assert aggregate_needed_table_ids(single_hop, index) == frozenset({_FACT})


def test_a_calculated_measure_referencing_an_unknown_measure_is_unproven():
    index = AggregateObjectIndex(
        dimension_names=frozenset(),
        table_by_dimension_name={},
        measure_id_by_name={"calc": "m-calc"},
        table_by_measure_id={},
        expression_by_measure_id={"m-calc": 'measure("ghost_measure") * 2'},
    )
    agg = types.SimpleNamespace(
        grain=[], columns=[types.SimpleNamespace(measure=_measure("calc", None))],
    )
    assert aggregate_needed_table_ids(agg, index) is None


# ---------------------------------------------------------------------------
# Deploy-eviction wiring (round-2 review). The pocket half of this proof has had
# ``test_deploy_eviction_hook_clears_the_join_graph_cache`` since Bug-8580; the
# aggregate half's object index had no equivalent, so deleting the
# ``invalidate_aggregate_population_cache`` call from ``evict_model_cache`` left
# the whole suite green.
# ---------------------------------------------------------------------------


async def test_deploy_eviction_hook_clears_the_aggregate_object_index_cache():
    """Producer/consumer wiring: model-service calls ``evict_model_cache`` after
    a deploy, and the aggregate row-population object index must go with it.

    A DEPLOYED model's index key carries ``(id, deployed_version_id, epoch)`` and
    self-invalidates. An UNDEPLOYED model keys on ``(id, "", 0)``, so re-binding a
    measure or a grain dimension to a column on ANOTHER relation would otherwise
    keep an under-estimated plan bound — and therefore an unearned population
    proof — cached for the full 300s TTL, serving numbers computed over a row
    population the query's own plan does not have.
    """
    from src.api.routes import evict_model_cache
    from src.routing import aggregate_population as _ap

    invalidate_aggregate_population_cache()
    key = ("model-agg-evict", "", 0)
    _ap._CONTEXT_CACHE[key] = (
        _ap._time.monotonic() + 300,
        AggregateObjectIndex(
            dimension_names=frozenset({"a_label"}),
            table_by_dimension_name={"a_label": _DIM_A},
            measure_id_by_name={"amount": "m-amount"},
            table_by_measure_id={"m-amount": _FACT},
            expression_by_measure_id={},
        ),
    )
    assert key in _ap._CONTEXT_CACHE

    await evict_model_cache("model-agg-evict")

    assert key not in _ap._CONTEXT_CACHE, (
        "evict_model_cache must clear the aggregate row-population object index, "
        "or a draft re-binding keeps serving on the previous plan bound"
    )


# ---------------------------------------------------------------------------
# The population refusal is a property of THIS CANDIDATE'S plan, not of the
# query. Bug-8779 review (2026-08-05).
# ---------------------------------------------------------------------------


def test_a_narrower_aggregate_at_the_query_grain_is_proven_where_the_broad_one_is_not():
    """``join_population_mismatch`` does NOT mean "this query can never be
    accelerated".

    The proof compares the CANDIDATE's plan (``{anchor} + its own grain tables +
    its own measure tables``) against the QUERY's compiled table set. The
    dominant refusal is ``extra = plan - kept`` — relations the AGGREGATE joined
    and the query did not. That set shrinks with the aggregate's grain, so an
    aggregate built at exactly the query's grain can be PROVEN on the very same
    model where a broader candidate is refused.

    This is why the miss-reason taxonomy must not treat the code as terminally
    INELIGIBLE: doing so removes the only build evidence for a shape a new,
    narrower aggregate would serve correctly — silent acceleration starvation,
    the Bug-8466 failure class.
    """
    from src.routing.aggregate_population import aggregate_population_proven
    from src.routing.pocket_population import JoinEdge, ModelJoinGraph

    # fact --LEFT(preserves fact)--> dim_a        (row-preserving)
    # fact --INNER----------------> dim_b        (drops unmatched fact rows)
    graph = ModelJoinGraph(
        table_ids=frozenset({_FACT, _DIM_A, _DIM_B}),
        edges=(
            JoinEdge(_FACT, _DIM_A, _C_FACT_A_FK, _C_A_PK, "left"),
            JoinEdge(_FACT, _DIM_B, _C_FACT_B_FK, _C_B_PK, "inner"),
        ),
        pk_column_ids=frozenset({_C_A_PK, _C_B_PK}),
        table_id_by_column_id={
            _C_FACT_A_FK: _FACT, _C_FACT_B_FK: _FACT,
            _C_A_PK: _DIM_A, _C_B_PK: _DIM_B,
        },
        anchor_table_id=_FACT,
    )
    index = AggregateObjectIndex(
        dimension_names=frozenset({"a_label", "b_label"}),
        table_by_dimension_name={"a_label": _DIM_A, "b_label": _DIM_B},
        measure_id_by_name={"amount": "m-amount"},
        table_by_measure_id={"m-amount": _FACT},
        expression_by_measure_id={},
    )

    def _agg(grain):
        return types.SimpleNamespace(
            id="agg-" + "-".join(grain),
            grain=list(grain),
            columns=[types.SimpleNamespace(
                measure=types.SimpleNamespace(name="amount"),
            )],
        )

    # SELECT a_label, SUM(amount) ... GROUP BY a_label  -> plan is {fact, dim_a}
    query_table_ids = {_FACT, _DIM_A}

    broad = _agg(["a_label", "b_label"])
    assert aggregate_needed_table_ids(broad, index) == frozenset(
        {_FACT, _DIM_A, _DIM_B}
    )
    assert aggregate_population_proven(
        aggregate=broad, graph=graph, index=index,
        query_table_ids=query_table_ids,
    ) == (False, "join_population_mismatch"), (
        "the broad candidate joins dim_b over an INNER edge the query elides, "
        "so its SUM is understated — it must be refused"
    )

    narrow = _agg(["a_label"])
    assert aggregate_needed_table_ids(narrow, index) == frozenset({_FACT, _DIM_A})
    assert aggregate_population_proven(
        aggregate=narrow, graph=graph, index=index,
        query_table_ids=query_table_ids,
    ) == (True, None), (
        "an aggregate built at the QUERY's own grain joins exactly the query's "
        "relations, so it is provable — building one is a real remediation for "
        "a join_population_mismatch miss"
    )


# ---------------------------------------------------------------------------
# Bug-8792 — the gate-order premise the optimizer's coverage refutation rests on
# ---------------------------------------------------------------------------


async def test_a_grain_uncovering_candidate_reports_grain_missing_not_population():
    """Bug-8792 criterion A, pinned behaviourally rather than by comment.

    The optimizer now treats a recorded ``join_population_mismatch`` as proof
    that a grain-COVERING artifact was evaluated and refused, and on that basis
    discounts STRICTLY BROADER artifacts from its coverage set
    (``shared/miss_reason_taxonomy._COVERAGE_REFUTING_REASONS`` ->
    ``optimizer/src/advisor/miss_analyzer._is_served``). That is sound only
    because this matcher emits the population token strictly AFTER the
    grain-coverage gate, which is what guarantees the refused artifact's grain is
    a superset of the miss row's ``requested_grain``.

    Nothing pinned that ordering. If a candidate whose grain does NOT cover the
    query ever reported the population token, the optimizer would discount an
    artifact that never made a coverage claim at all, and could spend a CTAS on
    it — with every taxonomy and analyzer test still green.

    The candidate here fails BOTH gates: its grain omits ``shared_key``, and the
    diamond shape makes its population unprovable component-wide (Bug-8637) no
    matter which grain it carries. Only the grain code may be reported.
    """
    dim_shared = make_dimension("shared_key")
    dim_shared.source_column_id = _C_SHARED_LABEL
    dim_a = make_dimension("a_key")
    dim_a.source_column_id = _C_A_PK
    measure = make_measure("amount")
    measure.source_column_id = _C_AMOUNT
    bq = make_bound_query(
        [dim_shared, dim_a], [measure],
        raw_sql=(
            "SELECT shared_key, a_key, SUM(amount) FROM test_model "
            "GROUP BY shared_key, a_key"
        ),
    )
    # Grain covers a_key only — shared_key is missing, so Rule 1 refuses first.
    agg = _aggregate(["a_key"], [measure])

    result = await _match(bq, _diamond_shape(), agg)

    assert result.aggregate is None
    assert AggregateSkipReason.GRAIN_MISSING in (result.skip_reasons or [])
    assert AggregateSkipReason.JOIN_POPULATION_MISMATCH not in (
        result.skip_reasons or []
    ), (
        "the population token was emitted before the grain-coverage gate — "
        "Bug-8792's 'the refused artifact is necessarily a grain superset' "
        "premise no longer holds and the optimizer coverage refutation is "
        "unsound (shared/miss_reason_taxonomy._COVERAGE_REFUTING_REASONS)"
    )


async def test_period_variant_grain_uncovering_candidate_reports_grain_missing_not_population():
    """Bug-8811: the period-variant branch applies the same gate ordering as the
    ordinary branch — grain must be checked before population, so a candidate
    that fails both reports GRAIN_MISSING (never JOIN_POPULATION_MISMATCH).

    The Bug-8792 criterion-A guard only pinned the ordinary matcher branch.
    The period-variant loop reads the same materialised rows and takes the
    same gates, but the ordering was not guarded — a reorder there would
    silently unsound the optimizer's coverage refutation just as it would on
    the ordinary branch.
    """
    from src.rewrite.period_variant_aggregate import PeriodVariantPlan

    dim_shared = make_dimension("shared_key")
    dim_shared.source_column_id = _C_SHARED_LABEL
    dim_a = make_dimension("a_key")
    dim_a.source_column_id = _C_A_PK
    measure = make_measure("amount")
    measure.source_column_id = _C_AMOUNT
    bq = make_bound_query(
        [dim_shared, dim_a], [measure],
        raw_sql=(
            "SELECT shared_key, a_key, SUM(amount) FROM test_model "
            "GROUP BY shared_key, a_key"
        ),
    )
    # Grain covers a_key only — shared_key is missing, so Rule 1 refuses first.
    agg = _aggregate(["a_key"], [measure])
    agg.persona_id = None
    agg.built_for_version_id = "v1"
    agg.built_for_epoch = 0

    _ctx = types.SimpleNamespace(
        anchor_dim_name="sale_month",
        base_sum_measures={measure.name},
        resolved_variants=[],
    )
    with patch(
        "src.routing.aggregate_matcher._resolve_period_variant_context",
        new=AsyncMock(return_value=(_ctx, None)),
    ), patch(
        "src.routing.aggregate_matcher._build_period_variant_plan_for_agg",
        return_value=PeriodVariantPlan(
            items=[], anchor_phys_col="month_c", anchor_data_type="date",
            partition_logical_to_phys={},
            time_grain_unit="month", time_dim_logical="sale_month",
            calendar_type="standard", fiscal_year_start_month=None,
            base_sum_cols=(),
        ),
    ):
        result = await _match(bq, _diamond_shape(), agg)

    assert result.aggregate is None
    assert AggregateSkipReason.GRAIN_MISSING in (result.skip_reasons or [])
    assert AggregateSkipReason.JOIN_POPULATION_MISMATCH not in (
        result.skip_reasons or []
    ), (
        "period-variant branch: the population token was emitted before the "
        "grain-coverage gate — Bug-8811: the Bug-8792 gate-order premise is "
        "unguarded on the period-variant branch"
    )


# ---------------------------------------------------------------------------
# Bug-8683 — missing-measure candidates must report MEASURE_MISSING, not
# JOIN_POPULATION_MISMATCH
# ---------------------------------------------------------------------------


async def test_missing_measure_reports_measure_missing_not_population_mismatch():
    """Bug-8683: a candidate that simply lacks the query's measure must report
    MEASURE_MISSING, not JOIN_POPULATION_MISMATCH.

    Without the gate-order fix, the population gate fires before the measure
    gate and trips on ``kept ⊄ plan`` (the missing measure's source relation is
    in the query's plan but absent from the aggregate's), mislabeling the
    refusal as a population mismatch.

    After the fix the population gate runs AFTER the measure-coverage block,
    so a missing-measure candidate correctly reports MEASURE_MISSING.
    """
    bq, _measure = _query_over_a_label_only()
    shape = _star_shape(a_join="left", b_join="left")
    # Build an aggregate that carries a different measure ("other_amount") —
    # one that is NOT in the shape at all. The measure gate must refuse it
    # before the population gate ever runs.
    other = make_measure("other_amount")
    other.source_column_id = _C_B_LABEL
    agg = _aggregate(["a_label"], [other])

    result = await _match(bq, shape, agg)

    assert result.aggregate is None
    assert AggregateSkipReason.MEASURE_MISSING in (result.skip_reasons or []), (
        "a candidate lacking the query's measure must report MEASURE_MISSING"
    )
    assert AggregateSkipReason.JOIN_POPULATION_MISMATCH not in (
        result.skip_reasons or []
    ), (
        "Bug-8683: the measure gate should fire before the population gate — "
        "mislabeling this as join_population_mismatch breaks /explain triage"
    )


# ---------------------------------------------------------------------------
# Bug-8664 POPULATION-PROOF ADMISSION TESTS (A1 integration, 2026-08-10)
#
# WHAT THESE PROVE, PRECISELY -- do not overstate them.
#
# The row-population equivalence proof was MOVED from BEFORE the measure-
# coverage gates to AFTER them, to improve miss-reason attribution.
#
# These tests prove that the population proof GATES ADMISSION and that an
# aggregate whose materialised rows are not the query's own is refused -- with
# the concrete numbers spelled out below. Removing the gate admits it and the
# user gets 200 where the truth is 500.
#
# These tests DO NOT discriminate between the OLD and NEW gate order on
# ADMISSION, and no test can: both orders reject the treatment arm and admit the
# control arm, because every gate in the moved span exits with `continue`, so
# the admitted SET is an INTERSECTION of per-candidate predicates and
# intersection is commutative. The reorder's admission-neutrality is therefore a
# STRUCTURAL property, pinned by the AST guards at the end of this file, not by
# these fixtures. Calling these a "gate-order discrimination test" would claim
# evidence they do not carry.
#
# Be precise about the scope of that claim. `skip_reasons` is NOT commutative --
# it is append-only, order-dependent, observable on `AggregateMatchResult`, and
# it feeds shared/miss_reason_taxonomy -> the optimizer's BUILD/INELIGIBLE
# classification. Changing it is the reorder's whole point. So the gate order IS
# behaviourally observable, and
# `test_missing_measure_reports_measure_missing_not_population_mismatch`
# (Bug-8683, earlier in this file) already discriminates it: under the OLD order
# the population proof runs first, trips on `kept not-subset-of plan`, appends
# JOIN_POPULATION_MISMATCH and continues, so MEASURE_MISSING is never appended
# and both of its assertions fail. (Verified: that fixture's population proof
# returns `(False, 'join_population_mismatch')`.)
#
# The accurate statement is therefore: no test can discriminate the ADMITTED
# SET; the REASON attribution is discriminated, and is already guarded.
# ---------------------------------------------------------------------------


# The concrete rows behind the fixture below. `_star_shape` is
# fact_sales -> dim_a and fact_sales -> dim_b; the query groups by `a_label`
# alone and never joins dim_b, so its own plan keeps EVERY fact row:
#
#   fact_sales:  (a_id=1, b_id=1,    amount=200)
#                (a_id=1, b_id=NULL, amount=300)   <- no dim_b partner
#   dim_a:       (id=1, a_label='A1')
#   dim_b:       (id=1, b_label='B1')
#
# The QUERY  "SELECT a_label, SUM(amount) GROUP BY a_label"  joins dim_a only:
#     -> A1, 500                                   (the TRUE answer)
#
# The AGGREGATE is built at grain (a_label, b_label), so its CTAS joins dim_b
# as well. With an INNER edge onto dim_b the second fact row has no partner and
# is dropped from the materialised rows:
#     agg rows -> (A1, B1, 200)
# Rolling that up to the query's grain yields:
#     -> A1, 200                                   (UNDERSTATED by 300)
#
# 200 != 500. Admitting this aggregate is a wrong number on a user's screen,
# which is why the population proof must gate ADMISSION and not merely
# annotate the miss reason.
_TRUE_SUM_OVER_A_LABEL = 500
_AGGREGATE_SUM_OVER_A_LABEL = 200


async def test_population_mismatched_candidate_that_passes_every_other_gate_is_still_refused():
    """Bug-8664 numbers proof, treatment arm.

    The candidate here passes EVERY gate the population proof was moved
    behind -- persona, version, grain coverage, select-dim coverage, measure
    presence, stat-type compatibility, staleness, freshness. The control arm
    below proves that by admitting the identical candidate once the population
    defect (and ONLY the population defect) is removed from the model shape.

    Therefore the population proof is the sole remaining discriminator between
    admission and refusal for this candidate: if it were removed, or moved past
    the admission point, this assertion would fail and the user would receive
    200 instead of 500 (see the row data above).

    This does NOT prove the gate ORDER is safe -- see the header note.
    """
    bq, measure = _query_over_a_label_only()
    # INNER onto the elided relation -> the aggregate's CTAS drops fact rows
    # with no dim_b partner -> understated SUM.
    shape = _star_shape(a_join="left", b_join="inner")
    agg = _aggregate(["a_label", "b_label"], [measure])

    result = await _match(bq, shape, agg)

    assert result.aggregate is None, (
        "WRONG NUMBERS: a population-mismatched aggregate was ADMITTED. "
        f"Serving it returns {_AGGREGATE_SUM_OVER_A_LABEL} where the query's "
        f"own plan returns {_TRUE_SUM_OVER_A_LABEL}. The Bug-8664 row-"
        "population proof must gate admission, not merely label the miss."
    )
    assert AggregateSkipReason.JOIN_POPULATION_MISMATCH in (result.skip_reasons or [])


async def test_control_arm_the_same_candidate_is_admitted_when_only_the_population_defect_is_removed():
    """Bug-8664 numbers proof, control arm.

    Identical query, identical aggregate, identical grain -- the ONLY change is
    the join type on the elided relation (INNER -> LEFT onto dim_b's declared
    primary key), which makes the aggregate's materialised population equal to
    the query's own. The candidate is admitted.

    This is what makes the treatment arm above a genuine discriminator rather
    than a candidate that was going to be refused by some earlier gate anyway:
    the two arms differ in exactly one property, and that property is the one
    the moved gate tests.
    """
    bq, measure = _query_over_a_label_only()
    shape = _star_shape(a_join="left", b_join="left")
    agg = _aggregate(["a_label", "b_label"], [measure])

    result = await _match(bq, shape, agg)

    assert result.aggregate is agg, (
        "control arm must ADMIT -- if this refuses, the treatment arm proves "
        "nothing about the population gate because some other gate is refusing "
        f"first. skip_reasons={result.skip_reasons!r}"
    )


@pytest.mark.parametrize("mode", ["legacy", "split"])
async def test_admission_is_unchanged_under_both_population_reason_modes(mode):
    """Bug-8789 must not perturb Bug-8664's admission decision.

    ``query.population_mismatch_reason_mode`` changes WHICH reason token a
    population refusal reports. It must never change WHETHER the candidate is
    refused -- a setting that can admit a wrong-numbers aggregate would be a
    remote-controlled correctness switch.
    """
    bq, measure = _query_over_a_label_only()
    shape = _star_shape(a_join="left", b_join="inner")
    agg = _aggregate(["a_label", "b_label"], [measure])

    with patch(
        "shared.config.resolver.get_setting",
        new=AsyncMock(return_value=mode),
    ):
        result = await _match(bq, shape, agg)

    assert result.aggregate is None, (
        f"population_mismatch_reason_mode={mode!r} changed the ADMISSION "
        "decision -- the mode may only change the reason token"
    )
    _expected = (
        AggregateSkipReason.POPULATION_PLAN_MISMATCH if mode == "split"
        else AggregateSkipReason.JOIN_POPULATION_MISMATCH
    )
    assert _expected in (result.skip_reasons or []), (
        f"expected {_expected!r} with mode={mode!r}, got {result.skip_reasons!r}"
    )


@pytest.mark.parametrize("stored", [None, "", "legacy", "LEGACY", "true", "1",
                                    "yes", "on", "splitting", object()])
async def test_population_reason_mode_fails_safe_to_legacy_on_anything_unrecognised(stored):
    """Bug-8789 flag-resolution guard -- the coverage whose absence let the
    original implementation ship with the wrong session kwarg.

    The original read the setting inside the proof primitive as
    ``get_setting(..., system_session=<TENANT session>)`` on a ``bool`` key.
    Nothing tested the resolution itself, so two defects shipped together: the
    system-level key was unreadable from the query-router (permanently stuck at
    its default in production), while ``bool()`` coercion of a stub session's
    row read TRUE in tests and silently flipped the vocabulary.

    Only the exact token 'split' may enable the sub-codes; everything else --
    including truthy-looking values -- must resolve to 'legacy'.
    """
    from src.routing.aggregate_matcher import _resolve_population_reason_split

    with patch(
        "shared.config.resolver.get_setting",
        new=AsyncMock(return_value=stored),
    ):
        assert await _resolve_population_reason_split(AsyncMock()) is False, (
            f"stored value {stored!r} enabled the split vocabulary; only the "
            "exact token 'split' may do that"
        )


@pytest.mark.parametrize("stored", ["split", "Split", "SPLIT"])
async def test_population_reason_mode_enables_split_only_on_the_exact_token(stored):
    """Positive arm of the guard above: the 'split' token does enable the
    sub-codes (case-insensitively, as ``_resolve_quantile_enforce`` treats its
    own token), so the fail-safe is not simply a hard-coded False."""
    from src.routing.aggregate_matcher import _resolve_population_reason_split

    with patch(
        "shared.config.resolver.get_setting", new=AsyncMock(return_value=stored),
    ):
        assert await _resolve_population_reason_split(AsyncMock()) is True


async def test_population_reason_mode_resolution_error_falls_back_to_legacy():
    """A settings-resolution failure must not change the miss vocabulary."""
    from src.routing.aggregate_matcher import _resolve_population_reason_split

    with patch(
        "shared.config.resolver.get_setting",
        new=AsyncMock(side_effect=RuntimeError("settings backend down")),
    ):
        assert await _resolve_population_reason_split(AsyncMock()) is False


async def test_default_matcher_run_uses_legacy_codes_with_a_stub_session():
    """The regression that six Bug-8664 tests caught the hard way.

    With no setting configured, a matcher run against a stub session must
    report the LEGACY code. The original implementation reported
    ``population_plan_mismatch`` here because it read a system-level key
    through a tenant session and the stub answered truthy.
    """
    bq, measure = _query_over_a_label_only()
    shape = _star_shape(a_join="left", b_join="inner")
    agg = _aggregate(["a_label", "b_label"], [measure])

    result = await _match(bq, shape, agg)

    assert AggregateSkipReason.JOIN_POPULATION_MISMATCH in (result.skip_reasons or [])
    assert AggregateSkipReason.POPULATION_PLAN_MISMATCH not in (
        result.skip_reasons or []
    )


async def test_gate_reorder_measure_override_bail_between_the_two_gate_positions_still_fails_closed():
    """The one control-flow difference the reorder actually introduces.

    Between the population proof's OLD position and its NEW position sits a
    candidate-INDEPENDENT ``return`` (the ``measure_agg_overrides`` bail, which
    reads only the logical query). Under the old order a population-mismatched
    candidate hit ``continue`` and never reached it; under the new order it
    does. The reachability of that early return therefore changed.

    The outcome must still be fail-closed (no aggregate -> source, correct
    numbers). This pins that: it is the only ordering-induced behaviour change
    in the reorder, and it must never resolve to an admission.
    """
    bq, measure = _query_over_a_label_only()
    bq.logical_query.measure_agg_overrides = {measure.name: "count"}
    shape = _star_shape(a_join="left", b_join="inner")
    agg = _aggregate(["a_label", "b_label"], [measure])

    result = await _match(bq, shape, agg)

    assert result.aggregate is None, (
        "the measure-override bail became reachable for population-mismatched "
        "candidates when the population gate moved; it must still fail closed"
    )


# ---------------------------------------------------------------------------
# Bug-8664 GATE-ORDER STRUCTURAL INVARIANT
#
# Why this is an AST test and not a behavioural one.
#
# The Bug-8664 reorder moved the row-population proof from before the
# measure-coverage gates to after them. The reorder is safe because the ADMITTED
# SET is provably unchanged: every gate in the moved span exits with `continue`,
# so the set of candidates reaching `candidates.append(...)` is an INTERSECTION
# of per-candidate predicates, and intersection is commutative -- reordering the
# conjuncts cannot change the result.
#
# That means NO behavioural test can discriminate between the two gate orders:
# both orders produce identical admissions for every input, by construction. The
# tests above prove the population proof is load-bearing (removing it admits a
# wrong-numbers aggregate); they do NOT and CANNOT prove its POSITION is safe,
# because its position genuinely does not affect admission.
#
# The safety of the reorder therefore rests on a STRUCTURAL property, and this
# test pins that property so it cannot silently rot: the commutativity argument
# holds only while every candidate-dependent exit in the loop is a `continue`.
# A future gate added between the grain gates and the population proof that
# `return`s based on the CURRENT candidate would abort the whole scan on one
# candidate's failure -- and THEN the gate order would decide which aggregate is
# admitted, i.e. which numbers users see.
# ---------------------------------------------------------------------------


def _agg_loops_in_find_best_aggregate():
    """Every `for agg in <name>:` loop in find_best_aggregate, with a flag for
    whether it can ADMIT a candidate (i.e. appends to a candidate list).

    Discovery is deliberately NOT keyed on the iterable's name. An earlier
    version matched only `for agg in aggregates:` and was therefore structurally
    blind to `for agg in inactive:` (line ~2090) -- the exact enumeration
    blind-spot shape CLAUDE.md calls out. Matching every `agg` loop and then
    CLASSIFYING it means a newly added loop is surfaced rather than skipped.

    FAILS CLOSED: if the module, the function, or any loop cannot be located,
    the caller's assertions fail rather than passing vacuously.
    """
    import ast
    import inspect
    from src.routing import aggregate_matcher

    tree = ast.parse(inspect.getsource(aggregate_matcher))
    fn = next(
        (
            n for n in ast.walk(tree)
            if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
            and n.name == "find_best_aggregate"
        ),
        None,
    )
    assert fn is not None, (
        "find_best_aggregate not found in aggregate_matcher -- this structural "
        "guard can no longer see the code it protects (fail closed)"
    )

    loops = [
        node for node in ast.walk(fn)
        if isinstance(node, (ast.For, ast.AsyncFor))
        and isinstance(node.target, ast.Name)
        and node.target.id == "agg"
    ]
    assert loops, (
        "no `for agg in ...:` candidate loop found in find_best_aggregate -- "
        "the guard's discovery mechanism is blind (fail closed)"
    )

    out = []
    for loop in loops:
        appends = [
            n.lineno for n in ast.walk(loop)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "append"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id in ("candidates", "_pv_candidates")
        ]
        out.append((loop, appends))
    return out


def test_the_guard_sees_every_candidate_loop_and_at_least_one_can_admit():
    """Guard-the-guard: pin the discovered scope itself.

    If a future refactor adds a fourth `agg` loop, or renames the candidate
    lists so no loop looks admitting any more, this fires -- rather than the two
    guards below silently protecting nothing.
    """
    loops = _agg_loops_in_find_best_aggregate()
    admitting = [(l, a) for l, a in loops if a]
    assert len(loops) == 3, (
        f"expected 3 `for agg in ...` loops in find_best_aggregate "
        f"(ordinary, period-variant, inactive-diagnostic), found {len(loops)} "
        f"at lines {[l.lineno for l, _ in loops]}. A new loop must be classified "
        "as admitting or non-admitting and this guard updated deliberately."
    )
    assert len(admitting) == 2, (
        "expected exactly 2 ADMITTING candidate loops (ordinary + period-variant), "
        f"found {len(admitting)} at lines {[l.lineno for l, _ in admitting]}"
    )


def test_every_candidate_dependent_exit_in_the_matcher_loop_is_a_continue():
    """Bug-8664 gate-order safety: the commutativity precondition.

    The reorder is admission-neutral ONLY because every candidate-dependent gate
    exits with `continue` (skip THIS candidate) rather than an exit that abandons
    the whole scan. Under `continue`-only gates the admitted set is an
    intersection of per-candidate predicates and is therefore order-independent.

    THREE statement kinds abandon the scan and all three are checked -- `return`,
    `break` and `raise`. Checking only `return` would be an enumeration blind
    spot: a candidate-dependent `break` skips every LATER candidate exactly as a
    `return` does, so it makes gate order decide which aggregate is admitted,
    i.e. which numbers the user sees. A `break` belonging to a NESTED loop is not
    an exit from the candidate scan and is correctly ignored (the loop at :1575
    legitimately contains several).

    Query-scoped exits (reading only ``bound_query``) are permitted: they resolve
    identically for every candidate, so they are order-independent. The
    ``measure_agg_overrides`` bail is exactly such a return.

    Candidate dependence is tracked through DERIVED LOCALS, not just the literal
    name ``agg``: a gate written against ``agg_measure_stats`` (built from
    ``agg.columns``) is candidate-dependent and is caught.

    THIS IS A TRIPWIRE, NOT A PROOF. Say so plainly rather than letting the
    green tick read as soundness. It is syntactic, intra-procedural and
    intra-loop, so it cannot decide the semantic property (order-independence)
    it stands for. Specifically it does NOT catch:

    * cross-iteration SIDE EFFECTS -- an accumulator shared between candidates
      makes admission order-dependent with no exit involved at all. That
      property currently holds only because every accumulator in the loop is
      reassigned to a fresh literal per iteration; it is not machine-checked.
    * candidate values laundered through a module-level or imported helper
      (no inter-procedural analysis).
    * a novel exit mechanism (``sys.exit``, an exception raised inside a
      helper, ``match``/``case`` bindings).

    What it DOES catch, each verified by mutation: a candidate-dependent
    ``return``, ``break`` or ``raise``, including dependence laundered through a
    derived local, a walrus binding, a comprehension, or a container keyed by
    the candidate. Those are the realistic regressions. Treat a green result as
    "the known regression shapes are absent", never as "the reorder is proven
    safe" -- the reorder's safety ultimately rests on the structural argument in
    the header block, which a future author must understand before adding a gate
    here.
    """
    import ast

    offenders: list[str] = []

    for loop, appends in _agg_loops_in_find_best_aggregate():
        if not appends:
            # Non-admitting diagnostic loop (`for agg in inactive:`): it cannot
            # change which aggregate is served, only which miss reason is
            # reported, so scan-abandoning exits are harmless there.
            continue

        parents: dict[int, ast.AST] = {}
        for node in ast.walk(loop):
            for child in ast.iter_child_nodes(node):
                parents[id(child)] = node

        # Names ASSIGNED inside the loop from an expression that mentions the
        # candidate are themselves candidate-dependent. Without this, a gate
        # written against a derived local -- e.g. ``agg_measure_stats``, built
        # from ``agg.columns`` at :1675 -- would be semantically
        # candidate-dependent while containing no literal ``agg``, and would
        # slip past a purely lexical check. Iterated to a fixed point so a
        # chain (agg -> x -> y) is tainted end to end.
        _tainted: set[str] = {"agg"}

        def _taints(node) -> bool:
            return node is not None and any(
                isinstance(x, ast.Name) and x.id in _tainted
                for x in ast.walk(node)
            )

        def _bind(target) -> bool:
            grew = False
            for nm in ast.walk(target):
                if isinstance(nm, ast.Name) and nm.id not in _tainted:
                    _tainted.add(nm.id)
                    grew = True
            return grew

        for _ in range(12):
            grew = False
            for n in ast.walk(loop):
                # (a) assignment from a candidate-derived expression
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        if _taints(n.value):
                            grew |= _bind(t)
                        elif (
                            isinstance(t, ast.Subscript)
                            and _taints(t.slice)
                            and isinstance(t.value, ast.Name)
                            and t.value.id not in _tainted
                        ):
                            # ``stats[agg_derived_key] = 1`` -- the VALUE is
                            # clean but the container is now keyed by the
                            # candidate, so anything read back out of it is
                            # candidate-dependent.
                            _tainted.add(t.value.id)
                            grew = True
                elif isinstance(n, (ast.AnnAssign, ast.AugAssign)) and _taints(n.value):
                    grew |= _bind(n.target)
                # (b) loop over a candidate-derived iterable taints its target
                #     (``for col in agg.columns:`` taints ``col``)
                elif isinstance(n, (ast.For, ast.AsyncFor)) and _taints(n.iter):
                    grew |= _bind(n.target)
                # (c) a candidate-derived value MUTATED into a container taints
                #     the container. ``agg_measure_stats`` is seeded empty
                #     (``= set()``, untainted) and filled by
                #     ``agg_measure_stats.add(<derived from agg>)``, so
                #     assignment-only tracking would miss it entirely.
                elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                    recv = n.func.value
                    if (
                        isinstance(recv, ast.Name)
                        and recv.id not in _tainted
                        and n.func.attr in (
                            "add", "append", "extend", "update",
                            "setdefault", "__setitem__",
                        )
                        and (
                            any(_taints(a) for a in n.args)
                            or any(_taints(k.value) for k in n.keywords)
                        )
                    ):
                        _tainted.add(recv.id)
                        grew = True
                # (d) walrus: ``(_x := <derived from agg>)`` binds a name
                #     outside any Assign node, so rule (a) never sees it.
                elif isinstance(n, ast.NamedExpr) and _taints(n.value):
                    grew |= _bind(n.target)
                # (e) comprehension target over a candidate-derived iterable
                elif isinstance(n, ast.comprehension) and _taints(n.iter):
                    grew |= _bind(n.target)
            if not grew:
                break

        def _mentions_agg(node: ast.AST) -> bool:
            return any(
                isinstance(n, ast.Name) and n.id in _tainted
                for n in ast.walk(node)
            )

        def _in_nested_scope(node: ast.AST, *, loops_too: bool) -> bool:
            cur = node
            while id(cur) in parents:
                cur = parents[id(cur)]
                if cur is loop:
                    return False
                if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    return True
                if loops_too and isinstance(cur, (ast.For, ast.AsyncFor, ast.While)):
                    return True
            return False

        for node in ast.walk(loop):
            if isinstance(node, ast.Return):
                kind, scoped_by_loops = "return", False
            elif isinstance(node, ast.Break):
                kind, scoped_by_loops = "break", True
            elif isinstance(node, ast.Raise):
                kind, scoped_by_loops = "raise", False
            else:
                continue
            if _in_nested_scope(node, loops_too=scoped_by_loops):
                continue

            # ``ast.Raise._fields == ('exc', 'cause')`` -- it has NO
            # ``.value``. Reading ``node.value`` on a Raise raises
            # AttributeError, which would make this guard CRASH on any
            # raise instead of classifying it: red either way, but for the
            # wrong reason, and unable to tell a permitted query-scoped
            # raise from an offending candidate-dependent one.
            if isinstance(node, ast.Raise):
                payload = node.exc
            elif isinstance(node, ast.Return):
                payload = node.value
            else:
                payload = None
            reason = None
            if payload is not None and _mentions_agg(payload):
                reason = f"`{kind}` whose expression references the candidate `agg`"
            else:
                cur = node
                while id(cur) in parents:
                    parent = parents[id(cur)]
                    if parent is loop:
                        break
                    if isinstance(parent, ast.If) and _mentions_agg(parent.test):
                        reason = f"`{kind}` guarded by an `if` that tests the candidate `agg`"
                        break
                    cur = parent
            if reason is not None:
                offenders.append(f"line {node.lineno}: {reason}")

    assert not offenders, (
        "candidate-DEPENDENT scan-abandoning exit found inside "
        "find_best_aggregate's candidate loop:\n  " + "\n  ".join(offenders) + "\n\n"
        "This breaks the precondition that makes the Bug-8664 gate reorder "
        "admission-neutral. `return`, `break` and `raise` all abandon the scan, so "
        "one candidate's failure stops later aggregates from ever being evaluated "
        "-- and then gate ORDER decides which aggregate is admitted, which changes "
        "the numbers users see. Either use `continue`, or re-prove the reorder's "
        "admission-neutrality and update this guard deliberately."
    )


def test_the_population_proof_runs_before_any_candidate_is_admitted():
    """Bug-8664: wherever the population proof sits in the gate order, EVERY
    proof call must precede EVERY admission point in the same loop.

    The comparison is ``max(proof) < min(append)``, not ``min(proof) <
    max(append)``. The weaker form fails OPEN: with one proof call and an
    admission point added ABOVE it, the weaker comparison still holds while a
    candidate is admitted unproven. Requiring the LAST proof to precede the
    FIRST admission is what actually rules that out.

    KNOWN LIMIT: this is textual (line) order, which is not the same as
    control-flow dominance. It catches the realistic refactor -- moving the
    proof below an append, or adding an append above the proof -- and is
    complemented by the behavioural admission tests earlier in this file.
    """
    import ast

    checked = 0
    for loop, appends in _agg_loops_in_find_best_aggregate():
        if not appends:
            continue
        checked += 1
        pop_lines = [
            n.lineno for n in ast.walk(loop)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_population_proven_for"
        ]
        assert pop_lines, (
            f"admitting candidate loop at line {loop.lineno} performs no "
            "row-population proof -- an aggregate can be admitted without "
            "proving its materialised rows are the query's own (wrong numbers)"
        )
        assert max(pop_lines) < min(appends), (
            f"candidate loop at line {loop.lineno}: a candidate is admitted at "
            f"line {min(appends)} before the population proof at line "
            f"{max(pop_lines)} has run. An aggregate whose materialised row "
            "population is not the query's own can now be served -- this is the "
            "wrong-numbers defect Bug-8664 exists to prevent."
        )
    assert checked == 2, (
        f"expected to check 2 admitting candidate loops, checked {checked} "
        "(fail closed)"
    )
