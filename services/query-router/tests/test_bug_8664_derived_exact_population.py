"""Bug-8664 on the DERIVED-EXACT aggregate route (``router._try_derived_exact_route``).

Round-1 deep review found the row-population gate wired into
``find_best_aggregate`` only, while ``_try_derived_exact_route`` is a THIRD
producer of ``RouteDecision(route_type="aggregate")`` and runs BEFORE the matcher
is ever called. Its kill switch (``query.derived_expression_serving_enabled``)
defaults to True and stays enabled even when the setting read errors, so the
route is on by default.

Worked example on the shape below — ``fact(2026-01-05, 100, 'A')``,
``fact(2026-01-09, 250, NULL)``, ``fact(2026-01-20, 40, 'A')``, ``dim('A')``:

* the aggregate CTAS joined ``fact INNER JOIN dim`` and grouped, so it holds
  ``2026-01-01 | 140`` — the NULL-key row was dropped;
* the query's own elided source plan is the bare fact scan: ``2026-01-01 | 390``;
* the derived-exact serve returned the aggregate's 140.

140 vs 390, permanently, with no staleness signal. These two tests pin the gate
onto that route and prove the refusal is the GATE and not the fixture.
"""
from __future__ import annotations

import pytest

from tests.test_derived_exact_serving import (  # noqa: F401
    _FakeAgg,
    _FakeAggColumn,
    _derived_bound_query,
)

pytestmark = pytest.mark.unit

_FACT = "t-fact"
_DIM = "t-dim"


def _graph(*, join_type: str, dim_key_is_pk: bool):
    from src.routing.pocket_population import JoinEdge, ModelJoinGraph

    return ModelJoinGraph(
        table_ids=frozenset({_FACT, _DIM}),
        edges=(JoinEdge(_FACT, _DIM, "c-fact-k", "c-dim-k", join_type),),
        pk_column_ids=frozenset({"c-dim-k"}) if dim_key_is_pk else frozenset(),
        table_id_by_column_id={
            "c-fact-k": _FACT, "c-dim-k": _DIM,
            "c-ts": _FACT, "c-amt": _FACT, "c-lab": _DIM,
        },
        anchor_table_id=_FACT,
    )


async def _route(monkeypatch, *, join_type: str, dim_key_is_pk: bool):
    from src.routing import aggregate_population as AP
    from src.routing import pocket_matcher as PM
    from src.routing import router as R
    from src.routing.aggregate_population import AggregateObjectIndex

    async def _get_setting(key, **_kw):
        return True if key == "query.derived_expression_serving_enabled" else "v0"

    monkeypatch.setattr("shared.config.resolver.get_setting", _get_setting)

    agg = _FakeAgg(
        grain_keys=[{
            "expression_fingerprint": "fp-month",
            "physical_column": "order_month",
            "input_column_ids": ["col-ts"],
        }],
        columns=[_FakeAggColumn("revenue", "sum", "revenue__sum")],
    )
    agg.model_id = None
    agg.target_id = None
    # The aggregate's grain also carries a dimension owned by the EXTRA relation,
    # so its CTAS joined that relation while the query's own plan (the bare fact)
    # does not. That difference is the whole population question.
    agg.grain = ["product"]

    async def _load(_model_id, _db):
        return [agg]

    monkeypatch.setattr("src.semantic.binder.load_active_aggregates", _load)

    graph = _graph(join_type=join_type, dim_key_is_pk=dim_key_is_pk)

    async def _load_graph(_model, _db):
        return graph, {}

    monkeypatch.setattr(PM, "_load_model_join_graph", _load_graph)
    monkeypatch.setattr(PM, "_query_plan_table_ids", lambda *_a, **_k: {_FACT})

    # A real, resolvable object index, so a refusal below can only come from the
    # POPULATION rule and never from an unresolved model.
    async def _load_index(_model, _db, *, graph, table_id_by_uda_id):
        return AggregateObjectIndex(
            dimension_names=frozenset({"product"}),
            table_by_dimension_name={"product": _DIM},
            measure_id_by_name={"revenue": "m-revenue"},
            table_by_measure_id={"m-revenue": _FACT},
            expression_by_measure_id={},
        )

    monkeypatch.setattr(AP, "load_aggregate_object_index", _load_index)

    bq = _derived_bound_query()
    return await R._try_derived_exact_route(
        bq, db=object(), target_dialect="postgres",
    )


@pytest.mark.asyncio
async def test_derived_exact_route_refuses_when_population_unproven(monkeypatch):
    """The aggregate joined an extra INNER relation the query's plan does not,
    so every value it returns is understated. ``find_best_aggregate`` refuses
    this shape; the derived-exact route must refuse it identically."""
    out = await _route(monkeypatch, join_type="inner", dim_key_is_pk=True)
    assert out is None, (
        "derived-exact route served an aggregate with NO row-population proof"
    )


@pytest.mark.asyncio
async def test_derived_exact_route_still_serves_a_row_preserving_plan(monkeypatch):
    """Control. The same shape with a row-preserving, non-fanning edge is exact,
    so the route must keep accelerating — proving the refusal above comes from
    the gate and not from the fixture or the route's blanket except."""
    out = await _route(monkeypatch, join_type="left", dim_key_is_pk=True)
    assert out is not None
    assert out.route_type == "aggregate"
