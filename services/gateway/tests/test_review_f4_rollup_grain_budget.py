"""Deep-review F4 (2026-09-04): the full CrossJoin lattice is bounded.

Bug-9845 made a native-All PivotTable request every grain of the CrossJoin
(2^N - 1 rollup queries for N one-level fields). That is the correct set, but
without a ceiling ten fields would plan 1023 source queries per refresh. The
planner now refuses past ``gateway.subtotal_grain_max_queries`` with a typed
error, and Execute turns it into a clear server fault. It never prunes back
to the legacy nested-prefix subset silently.
"""

from __future__ import annotations

import pytest

from src.dax import xmla_server
from src.dax.subtotal_engine import (
    RollupGrainBudgetExceeded,
    SubtotalHierarchy,
    SubtotalLevel,
    build_multi_subtotal_queries,
)
from tests.test_bug9789_9244_xmla_production_path import (
    _excel_slicer_statement,
    _execute_method,
    _patch_execute_environment,
)


def _flat(name: str) -> SubtotalHierarchy:
    return SubtotalHierarchy(
        hierarchy_name=name, mdx_dim_name=name, mdx_hier_name=name,
        levels=[SubtotalLevel(name=name, ordinal=0, dim_name=name)],
        axis=0, is_flat_attribute_rollup=True,
    )


def _plan(n_fields: int, budget: int | None):
    names = [f"f{i}" for i in range(n_fields)]
    return build_multi_subtotal_queries(
        mdx_dims=names, mdx_measures=["m"], where_sql_clauses=[],
        model_slug="m", measures_meta=[{"name": "m", "default_agg": "sum"}],
        hierarchies=[_flat(n) for n in names], measure_canonical={"m": "m"},
        max_grain_queries=budget,
    )


def test_f4_lattice_within_budget_is_planned_in_full() -> None:
    assert len(_plan(3, budget=7)) == 7  # 2^3 - 1


@pytest.mark.parametrize("n_fields", [4, 7])
def test_f4_lattice_over_budget_raises_instead_of_pruning(n_fields: int) -> None:
    with pytest.raises(RollupGrainBudgetExceeded) as info:
        _plan(n_fields, budget=7)
    assert info.value.planned == 2 ** n_fields - 1
    assert info.value.budget == 7
    assert info.value.hierarchies == n_fields


def test_f4_no_budget_means_unbounded_for_explicit_callers() -> None:
    assert len(_plan(4, budget=None)) == 15


@pytest.mark.asyncio
async def test_f4_execute_faults_clearly_when_the_budget_is_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production path: three flat fields need 7 grains; budget 3 -> fault
    naming the counts, and not one grain query executed."""
    executed: list[str] = []

    async def execute_query(*, sql: str = "", **_):
        executed.append(sql)
        return {"columns": ["a", "b", "c", "average_base_amount"], "rows": [
            {"a": "x", "b": "y", "c": "z", "average_base_amount": 1.0},
        ]}

    _patch_execute_environment(monkeypatch, ["a", "b", "c"], execute_query)
    monkeypatch.setattr(xmla_server, "_subtotal_grain_budget", lambda: 3)
    response = await xmla_server._handle_execute(
        _execute_method(
            _excel_slicer_statement(["a", "b", "c"], "average_base_amount"),
            "Microsoft Office Excel",
        ),
        tenant_slug="demo", jwt_token="token", session_id="review-f4",
    )
    body = response.body.decode("utf-8")
    assert "Fault" in body
    assert "7 subtotal queries" in body and "limit of 3" in body
    # Only the detail query ran; no grain fan-out started.
    assert len(executed) == 1


def test_b3_budget_is_measured_on_the_planned_lattice() -> None:
    """Deep-review B3: seven one-level fields are 127 grains (the all-detail
    combination excluded) and are refused at budget 64 with the planned
    count reported, never pruned silently."""
    with pytest.raises(RollupGrainBudgetExceeded) as info:
        _plan(7, budget=64)
    assert info.value.planned == 127
    assert info.value.budget == 64
