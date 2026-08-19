"""Bug-7784 — calc-expansion base measures must resolve from the DEPLOYED
SNAPSHOT, not the live draft ORM.

find_best_aggregate's Bug-7178 calc expansion resolves the base measures a
calculated measure references to decide (a) whether the calc is expandable over
an aggregate's stored stat columns and (b) which physical column to read. The
binder and the percentile exactness gate pin measure semantics to the immutable
deployed snapshot; resolving these base measures from live draft ORM rows split
the authority, so a draft edit made AFTER deploy could steer aggregate matching.

These tests prove the routing DECISION follows the deployed snapshot:

* When the deployed snapshot says the base measure is a plain additive SUM, the
  calc is expandable and the aggregate is matched — EVEN IF a live draft edit
  has since flipped that base measure to semi-additive (which would block
  expansion). The draft must not steer the route.

Run from tessallite/services/query-router/:
    pytest tests/test_bug_7784_calc_expansion_snapshot_authority.py -v
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

import pytest

from conftest import (
    make_aggregate,
    make_agg_col,
    make_bound_query,
    make_dimension,
    make_measure,
)

from src.routing.aggregate_matcher import find_best_aggregate

pytestmark = pytest.mark.integration


def _db_returning(*aggregates):
    db = AsyncMock()

    async def _load_active(model_id, db_):
        return list(aggregates)

    async def _load_inactive(model_id, db_):
        return []

    return db, _load_active, _load_inactive


def _shape_with(measures):
    return types.SimpleNamespace(measures=list(measures))


@pytest.mark.asyncio
async def test_deployed_snapshot_base_measure_steers_expansion_not_draft():
    """The deployed snapshot has ``revenue``/``cost`` as plain additive SUM, so
    the calc ``profit_margin`` IS expandable and the aggregate matches — even
    though the LIVE ORM would return ``revenue`` as semi-additive (which would
    block expansion). The route must follow the deployed snapshot."""
    # Calc measure references revenue + cost (neither is in resolved_measures,
    # so they are loaded via the base-measure resolution path under test).
    profit_margin = make_measure(
        "profit_margin",
        measure_type="calculated",
        expression='safe_div(measure("revenue"), measure("cost"))',
        calc_agg_mode="expression_as_written",
        is_additive=False,
    )
    region = make_dimension("region")
    bq = make_bound_query(
        dimensions=[region],
        measures=[profit_margin],
        grain=["region"],
    )

    # Deployed snapshot: clean additive SUM bases (expansion allowed).
    snapshot_revenue = make_measure("revenue", "sum")
    snapshot_cost = make_measure("cost", "sum")
    deployed_shape = _shape_with([profit_margin, snapshot_revenue, snapshot_cost])

    # Live DRAFT ORM: revenue flipped to semi-additive (would BLOCK expansion).
    draft_revenue = make_measure(
        "revenue", "sum", semi_additive_behavior="last_child",
    )
    draft_cost = make_measure("cost", "sum")

    # The aggregate holds the base stat columns but no precomputed calc column.
    agg = make_aggregate(
        grain=["region"],
        columns=[
            make_agg_col(snapshot_revenue, "sum"),
            make_agg_col(snapshot_cost, "sum"),
        ],
    )

    db, load_active, load_inactive = _db_returning(agg)

    # If the code ever falls back to the live ORM load, this AsyncMock returns
    # the DRAFT (semi-additive) rows, which would block expansion -> no match.
    draft_result = types.SimpleNamespace()
    draft_result.scalars = lambda: types.SimpleNamespace(
        all=lambda: [draft_revenue, draft_cost]
    )
    db.execute = AsyncMock(return_value=draft_result)

    with (
        patch("src.routing.aggregate_matcher.load_active_aggregates", load_active),
        patch("src.routing.aggregate_matcher.load_inactive_aggregates", load_inactive),
        patch("src.routing.aggregate_matcher._get_canonical_dims_cached", return_value=[]),
        patch(
            "src.semantic.snapshot_resolver.resolve_deployed_shape",
            AsyncMock(return_value=deployed_shape),
        ),
    ):
        result = await find_best_aggregate(bq, db)

    assert result.aggregate is not None, (
        "Deployed snapshot base measures are additive SUM -> calc expandable -> "
        "aggregate must match, regardless of the live draft edit."
    )
    assert "profit_margin" in result.calc_expandable_measures
    pairs = result.calc_expandable_measures["profit_margin"]
    assert {name for name, _ in pairs} == {"revenue", "cost"}
    assert all(stat == "sum" for _, stat in pairs)


@pytest.mark.asyncio
async def test_deployed_model_snapshot_exception_fails_closed_no_live_fallback():
    """Codex R1 finding 4: a DEPLOYED model whose snapshot resolution RAISES
    (transient DB error / cache eviction) must NOT fall back to the live draft
    ORM — that would recreate the split authority. It fails closed: base
    measures stay unresolved -> no calc expansion -> no aggregate match. The
    live ORM (which would return draft rows) must never be queried."""
    profit_margin = make_measure(
        "profit_margin",
        measure_type="calculated",
        expression='safe_div(measure("revenue"), measure("cost"))',
        calc_agg_mode="expression_as_written",
        is_additive=False,
    )
    region = make_dimension("region")
    bq = make_bound_query(
        dimensions=[region],
        measures=[profit_margin],
        grain=["region"],
    )
    # make_bound_query stamps deployed_version_id="v1" -> a deployed model.
    assert bq.model.deployed_version_id is not None

    draft_revenue = make_measure("revenue", "sum")
    draft_cost = make_measure("cost", "sum")
    agg = make_aggregate(
        grain=["region"],
        columns=[
            make_agg_col(draft_revenue, "sum"),
            make_agg_col(draft_cost, "sum"),
        ],
    )

    db, load_active, load_inactive = _db_returning(agg)
    # If the code ever queries the live ORM, this returns the draft rows that
    # WOULD enable expansion — the test asserts this path is NOT taken.
    live_result = types.SimpleNamespace()
    live_result.scalars = lambda: types.SimpleNamespace(
        all=lambda: [draft_revenue, draft_cost]
    )
    db.execute = AsyncMock(return_value=live_result)

    async def _boom(*a, **k):
        raise RuntimeError("transient snapshot resolution failure")

    with (
        patch("src.routing.aggregate_matcher.load_active_aggregates", load_active),
        patch("src.routing.aggregate_matcher.load_inactive_aggregates", load_inactive),
        patch("src.routing.aggregate_matcher._get_canonical_dims_cached", return_value=[]),
        patch(
            "src.semantic.snapshot_resolver.resolve_deployed_shape",
            side_effect=_boom,
        ),
    ):
        result = await find_best_aggregate(bq, db)

    # Fail closed: calc not expandable (base measures unresolved) -> no match.
    assert result.aggregate is None
    assert "profit_margin" not in (result.calc_expandable_measures or {})


@pytest.mark.asyncio
async def test_falls_back_to_live_orm_when_no_deployed_shape():
    """When the model has no usable deployed shape (resolve_deployed_shape
    returns None), the base measures load from the live ORM — the historical
    behaviour is preserved, not silently disabled."""
    profit_margin = make_measure(
        "profit_margin",
        measure_type="calculated",
        expression='safe_div(measure("revenue"), measure("cost"))',
        calc_agg_mode="expression_as_written",
        is_additive=False,
    )
    region = make_dimension("region")
    bq = make_bound_query(
        dimensions=[region],
        measures=[profit_margin],
        grain=["region"],
    )
    # UNDEPLOYED model: no deploy pointer -> the live tables ARE the authority,
    # so the live ORM fallback is the correct path (not fail-closed).
    bq.model.deployed_version_id = None

    live_revenue = make_measure("revenue", "sum")
    live_cost = make_measure("cost", "sum")

    agg = make_aggregate(
        grain=["region"],
        columns=[
            make_agg_col(live_revenue, "sum"),
            make_agg_col(live_cost, "sum"),
        ],
    )

    db, load_active, load_inactive = _db_returning(agg)
    live_result = types.SimpleNamespace()
    live_result.scalars = lambda: types.SimpleNamespace(
        all=lambda: [live_revenue, live_cost]
    )
    db.execute = AsyncMock(return_value=live_result)

    with (
        patch("src.routing.aggregate_matcher.load_active_aggregates", load_active),
        patch("src.routing.aggregate_matcher.load_inactive_aggregates", load_inactive),
        patch("src.routing.aggregate_matcher._get_canonical_dims_cached", return_value=[]),
        patch(
            "src.semantic.snapshot_resolver.resolve_deployed_shape",
            AsyncMock(return_value=None),
        ),
    ):
        result = await find_best_aggregate(bq, db)

    assert result.aggregate is not None
    assert "profit_margin" in result.calc_expandable_measures
