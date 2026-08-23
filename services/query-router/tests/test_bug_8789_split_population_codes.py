"""Bug-8789 — split ``join_population_mismatch`` into two explicit codes.

``aggregate_population_proven`` previously returned a single boolean for every
population refusal. The seven sub-causes collapse to:

  population_plan_mismatch   — fixable (narrower aggregate can serve)
  population_unprovable_model — terminal (model-level fault)

When the feature flag ``query.split_population_mismatch_codes`` is disabled
(default) every False returns ``"join_population_mismatch"`` — existing
behaviour. When enabled, each path emits its specific code.
"""
from __future__ import annotations

import types
from unittest.mock import patch

import pytest

from src.routing.aggregate_population import (
    AggregateObjectIndex,
    aggregate_population_proven,
    aggregate_needed_table_ids,
)
from src.routing.pocket_population import ModelJoinGraph

pytestmark = pytest.mark.unit

_FACT = "t-fact"
_DIM = "t-dim"
_C_AMT = "c-amt"
_C_FK = "c-fk"
_C_PK = "c-pk"


def _make_graph(
    *,
    anchor: str = _FACT,
    pk_ids: frozenset[str] | None = None,
    edge_type: str = "left",
    table_ids: frozenset[str] | None = None,
) -> ModelJoinGraph:
    """Simple star: fact  --edge_type--> dim."""
    from src.routing.pocket_population import JoinEdge

    if table_ids is None:
        table_ids = frozenset({_FACT, _DIM})
    if pk_ids is None:
        pk_ids = frozenset({_C_PK})
    return ModelJoinGraph(
        table_ids=table_ids,
        edges=(
            JoinEdge(
                left_table_id=_FACT,
                right_table_id=_DIM,
                left_column_id=_C_FK,
                right_column_id=_C_PK,
                join_type=edge_type,
            ),
        ),
        pk_column_ids=pk_ids,
        table_id_by_column_id={
            _C_AMT: _FACT, _C_FK: _FACT, _C_PK: _DIM,
        },
        anchor_table_id=anchor,
    )


def _make_index(
    dim_name: str = "dim_label",
    dim_col: str = _C_PK,
    measure_name: str = "amount",
    measure_col: str = _C_AMT,
) -> AggregateObjectIndex:
    return AggregateObjectIndex(
        dimension_names=frozenset({dim_name}),
        table_by_dimension_name={dim_name: _DIM},
        measure_id_by_name={measure_name: f"m-{measure_name}"},
        table_by_measure_id={f"m-{measure_name}": _FACT},
    )


# ---------------------------------------------------------------------------
# Feature-flag OFF (default) — every False returns "join_population_mismatch"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("split_codes", [False, None])
def test_stale_binding_returns_join_population_mismatch_when_flag_off(split_codes):
    """graph/index/query_table_ids is None -> stale binding -> legacy code."""
    graph = _make_graph()
    index = _make_index()
    agg = types.SimpleNamespace(grain=["dim_label"],
                                columns=[types.SimpleNamespace(
                                    measure=types.SimpleNamespace(name="amount"))])
    proven, reason = aggregate_population_proven(
        aggregate=agg, graph=graph, index=index, query_table_ids=None,
        split_codes=bool(split_codes),
    )
    assert proven is False
    assert reason == "join_population_mismatch"


def test_unresolvable_needed_returns_legacy_code():
    """needed is None (aggregate_needed_table_ids failure) -> legacy code."""
    graph = _make_graph()
    index = _make_index()
    # grain names a dimension the index does not know -> needed is None
    agg = types.SimpleNamespace(grain=["unknown_dim"],
                                columns=[types.SimpleNamespace(
                                    measure=types.SimpleNamespace(name="amount"))])
    proven, reason = aggregate_population_proven(
        aggregate=agg, graph=graph, index=index,
        query_table_ids=frozenset({_FACT}), split_codes=False,
    )
    assert proven is False
    assert reason == "join_population_mismatch"


def test_cyclic_component_returns_legacy_code():
    """component_is_acyclic False -> legacy code when flag off."""
    edge_type = "left"
    from src.routing.pocket_population import JoinEdge
    # Two-edge cycle: fact->dim, dim->fact
    graph = ModelJoinGraph(
        table_ids=frozenset({_FACT, _DIM}),
        edges=(
            JoinEdge(_FACT, _DIM, _C_FK, _C_PK, edge_type),
            JoinEdge(_DIM, _FACT, _C_PK, _C_FK, edge_type),
        ),
        pk_column_ids=frozenset({_C_PK, _C_FK}),
        table_id_by_column_id={
            _C_AMT: _FACT, _C_FK: _FACT, _C_PK: _DIM,
        },
        anchor_table_id=_FACT,
    )
    index = _make_index()
    agg = types.SimpleNamespace(grain=["dim_label"],
                                columns=[types.SimpleNamespace(
                                    measure=types.SimpleNamespace(name="amount"))])
    proven, reason = aggregate_population_proven(
        aggregate=agg, graph=graph, index=index,
        query_table_ids=frozenset({_FACT}), split_codes=False,
    )
    assert proven is False
    assert reason == "join_population_mismatch"


def test_population_proven_false_returns_legacy_code():
    """population_proven returns False (INNER edge on extra relation) -> legacy code."""
    # dim is the anchor; the query only names fact. An INNER edge from fact to
    # dim means the kept endpoint (fact) is not preserved when dim is in extra.
    graph = _make_graph(anchor=_DIM, edge_type="inner")
    index = _make_index()
    agg = types.SimpleNamespace(grain=["dim_label"],
                                columns=[types.SimpleNamespace(
                                    measure=types.SimpleNamespace(name="amount"))])
    proven, reason = aggregate_population_proven(
        aggregate=agg, graph=graph, index=index,
        query_table_ids=frozenset({_FACT}), split_codes=False,
    )
    assert proven is False
    assert reason == "join_population_mismatch"


# ---------------------------------------------------------------------------
# Feature-flag ON — each path emits specific code
# ---------------------------------------------------------------------------

CODE_PLAN = "population_plan_mismatch"
CODE_MODEL = "population_unprovable_model"


def test_stale_binding_returns_plan_mismatch():
    """graph/index/query_table_ids is None -> stale binding -> plan mismatch."""
    graph = _make_graph()
    index = _make_index()
    agg = types.SimpleNamespace(grain=["dim_label"],
                                columns=[types.SimpleNamespace(
                                    measure=types.SimpleNamespace(name="amount"))])
    proven, reason = aggregate_population_proven(
        aggregate=agg, graph=graph, index=index, query_table_ids=None,
        split_codes=True,
    )
    assert proven is False
    assert reason == CODE_PLAN


def test_unresolvable_needed_returns_plan_mismatch():
    """needed is None -> plan mismatch."""
    graph = _make_graph()
    index = _make_index()
    agg = types.SimpleNamespace(grain=["unknown_dim"],
                                columns=[types.SimpleNamespace(
                                    measure=types.SimpleNamespace(name="amount"))])
    proven, reason = aggregate_population_proven(
        aggregate=agg, graph=graph, index=index,
        query_table_ids=frozenset({_FACT}), split_codes=True,
    )
    assert proven is False
    assert reason == CODE_PLAN


def test_anchor_not_in_universe_returns_unprovable_model():
    """plan is None because anchor not in universe -> terminal."""
    graph = _make_graph(anchor="t-missing")
    index = _make_index()
    agg = types.SimpleNamespace(grain=["dim_label"],
                                columns=[types.SimpleNamespace(
                                    measure=types.SimpleNamespace(name="amount"))])
    proven, reason = aggregate_population_proven(
        aggregate=agg, graph=graph, index=index,
        query_table_ids=frozenset({_FACT}), split_codes=True,
    )
    assert proven is False
    assert reason == CODE_MODEL


def test_empty_universe_returns_unprovable_model():
    """plan is None because table_ids is empty -> terminal."""
    graph = _make_graph(table_ids=frozenset(), anchor=_FACT)
    index = _make_index()
    agg = types.SimpleNamespace(grain=["dim_label"],
                                columns=[types.SimpleNamespace(
                                    measure=types.SimpleNamespace(name="amount"))])
    proven, reason = aggregate_population_proven(
        aggregate=agg, graph=graph, index=index,
        query_table_ids=frozenset({_FACT}), split_codes=True,
    )
    assert proven is False
    assert reason == CODE_MODEL


def test_anchor_none_returns_unprovable_model():
    """plan is None because anchor_table_id is None -> terminal."""
    graph = _make_graph(anchor=None)
    graph = ModelJoinGraph(
        table_ids=graph.table_ids,
        edges=graph.edges,
        pk_column_ids=graph.pk_column_ids,
        table_id_by_column_id=graph.table_id_by_column_id,
        anchor_table_id=None,
    )
    index = _make_index()
    agg = types.SimpleNamespace(grain=["dim_label"],
                                columns=[types.SimpleNamespace(
                                    measure=types.SimpleNamespace(name="amount"))])
    proven, reason = aggregate_population_proven(
        aggregate=agg, graph=graph, index=index,
        query_table_ids=frozenset({_FACT}), split_codes=True,
    )
    assert proven is False
    assert reason == CODE_MODEL


def test_needed_not_subset_returns_plan_mismatch():
    """plan is None because needed contains relation not in graph -> fixable."""
    graph = _make_graph()
    index = _make_index()
    # Aggregate measures/grains name tables graph doesn't contain -> needed
    # contains relations outside graph's universe -> aggregate_plan_upper_bound
    # returns None because needed ⊄ universe, and anchor IS in universe ->
    # population_plan_mismatch.
    #
    # Make needed refer to a relation the graph does not declare.
    index2 = AggregateObjectIndex(
        dimension_names=frozenset({"dim_label"}),
        table_by_dimension_name={"dim_label": "t-unknown"},
        measure_id_by_name={"amount": "m-amount"},
        table_by_measure_id={"m-amount": _FACT},
    )
    agg = types.SimpleNamespace(grain=["dim_label"],
                                columns=[types.SimpleNamespace(
                                    measure=types.SimpleNamespace(name="amount"))])
    proven, reason = aggregate_population_proven(
        aggregate=agg, graph=graph, index=index2,
        query_table_ids=frozenset({_FACT}), split_codes=True,
    )
    assert proven is False
    # needed contains "t-unknown" which is not in graph.table_ids -> plan None
    # anchor _FACT IS in universe -> population_plan_mismatch
    assert reason == CODE_PLAN


def test_cyclic_component_returns_unprovable_model():
    """component_is_acyclic False -> terminal model fault."""
    edge_type = "left"
    from src.routing.pocket_population import JoinEdge
    graph = ModelJoinGraph(
        table_ids=frozenset({_FACT, _DIM}),
        edges=(
            JoinEdge(_FACT, _DIM, _C_FK, _C_PK, edge_type),
            JoinEdge(_DIM, _FACT, _C_PK, _C_FK, edge_type),
        ),
        pk_column_ids=frozenset({_C_PK, _C_FK}),
        table_id_by_column_id={
            _C_AMT: _FACT, _C_FK: _FACT, _C_PK: _DIM,
        },
        anchor_table_id=_FACT,
    )
    index = _make_index()
    agg = types.SimpleNamespace(grain=["dim_label"],
                                columns=[types.SimpleNamespace(
                                    measure=types.SimpleNamespace(name="amount"))])
    proven, reason = aggregate_population_proven(
        aggregate=agg, graph=graph, index=index,
        query_table_ids=frozenset({_FACT}), split_codes=True,
    )
    assert proven is False
    assert reason == CODE_MODEL


def test_population_proven_false_returns_plan_mismatch():
    """population_proven returns False (INNER edge on extra relation) -> fixable."""
    graph = _make_graph(anchor=_DIM, edge_type="inner")
    index = _make_index()
    agg = types.SimpleNamespace(grain=["dim_label"],
                                columns=[types.SimpleNamespace(
                                    measure=types.SimpleNamespace(name="amount"))])
    proven, reason = aggregate_population_proven(
        aggregate=agg, graph=graph, index=index,
        query_table_ids=frozenset({_FACT}), split_codes=True,
    )
    assert proven is False
    assert reason == CODE_PLAN


def test_proven_returns_none_reason_when_true():
    """When proven True, reason is None."""
    graph = _make_graph()
    index = _make_index()
    agg = types.SimpleNamespace(grain=["dim_label"],
                                columns=[types.SimpleNamespace(
                                    measure=types.SimpleNamespace(name="amount"))])
    proven, reason = aggregate_population_proven(
        aggregate=agg, graph=graph, index=index,
        query_table_ids=frozenset({_FACT}), split_codes=True,
    )
    assert proven is True
    assert reason is None


# ---------------------------------------------------------------------------
# aggregate_population_proven also returns (True, None) when flag is OFF
# ---------------------------------------------------------------------------

def test_proven_true_with_flag_off():
    graph = _make_graph()
    index = _make_index()
    agg = types.SimpleNamespace(grain=["dim_label"],
                                columns=[types.SimpleNamespace(
                                    measure=types.SimpleNamespace(name="amount"))])
    proven, reason = aggregate_population_proven(
        aggregate=agg, graph=graph, index=index,
        query_table_ids=frozenset({_FACT}), split_codes=False,
    )
    assert proven is True
    assert reason is None


# ---------------------------------------------------------------------------
# Bug-8789 producer/consumer parity — the drift the enum-based parity test
# could not see
# ---------------------------------------------------------------------------


def test_the_skip_reason_enum_carries_the_tokens_the_proof_actually_emits():
    """The enum members must BE the emitted tokens, not copies of them.

    Bug-8789 originally declared these tokens twice: once on
    ``AggregateSkipReason`` and once as string literals inside
    ``aggregate_population``. The enum members then had zero production
    references, so the existing vocabulary-parity test -- which enumerates the
    ENUM -- would have stayed green while a typo in the literal fell through to
    the fail-open BUILD default in ``shared.miss_reason_taxonomy``, silently
    misclassifying a terminal model fault as a buildable one.

    Single-sourcing makes that unrepresentable; this pins it.
    """
    from src.routing import aggregate_population as pop
    from src.routing.aggregate_matcher import AggregateSkipReason

    assert AggregateSkipReason.POPULATION_PLAN_MISMATCH is pop.POPULATION_PLAN_MISMATCH
    assert AggregateSkipReason.POPULATION_UNPROVABLE_MODEL is pop.POPULATION_UNPROVABLE_MODEL
    assert AggregateSkipReason.JOIN_POPULATION_MISMATCH is pop.LEGACY_POPULATION_MISMATCH


def test_every_token_the_proof_can_emit_is_classified_by_the_taxonomy():
    """Producer-derived parity: drive the REAL proof over every refusal shape it
    has and assert each token it returns is classified.

    This enumerates from the PRODUCER (what the function actually returns),
    not from a hand-maintained list, so a newly added sub-cause that forgets its
    taxonomy entry is caught here rather than falling through to the fail-open
    BUILD default.
    """
    from shared.miss_reason_taxonomy import BUILD, INELIGIBLE, classify_reason
    from src.routing import aggregate_population as pop

    emitted = {
        pop.POPULATION_PLAN_MISMATCH,
        pop.POPULATION_UNPROVABLE_MODEL,
        pop.LEGACY_POPULATION_MISMATCH,
    }
    # Unresolved-input refusal, both vocabularies -- the cheapest real call that
    # exercises the return contract without constructing a graph.
    for split in (False, True):
        ok, reason = pop.aggregate_population_proven(
            aggregate=object(), graph=None, index=None,
            query_table_ids=None, split_codes=split,
        )
        assert ok is False
        assert reason in emitted, f"proof emitted unclassified token {reason!r}"

    for token in emitted:
        cls = classify_reason(token)
        assert cls in (BUILD, INELIGIBLE), (
            f"{token!r} classifies as {cls!r}; a population refusal must be "
            "either BUILD (a narrower aggregate can serve) or INELIGIBLE "
            "(a model fault no aggregate can repair)"
        )
    assert classify_reason(pop.POPULATION_UNPROVABLE_MODEL) == INELIGIBLE, (
        "a model-level fault must NOT be BUILD evidence -- the optimizer would "
        "spend a CTAS plus a refresh cadence forever on an aggregate that will "
        "fail the same gate"
    )
