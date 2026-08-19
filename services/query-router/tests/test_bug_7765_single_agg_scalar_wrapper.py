"""Bug-7765 — single-aggregate inline DAX expressions must not silently drop
the surrounding scalar structure or the requested aggregate function.

The 6959/7595 gates reject multi-column and repeated-column ratios, but a
SINGLE aggregate over a SINGLE column wrapped in scalar structure still bound
to the bare column with the measure's default_agg — silently dropping BOTH the
scalar wrapper AND the requested function.  Probed wrong-number cases:

    SUM(Sales[Amount]) * 1.1          -> was served as SUM(Amount), *1.1 lost
    DIVIDE(SUM(Sales[Amount]), 100)   -> was served as SUM(Amount), /100 lost
    MIN(Sales[Amount])                -> was served with the measure default_agg
                                         (e.g. SUM), NOT MIN

The IR has no slot for a scalar wrapper or a per-reference aggregate override,
so the correct behaviour is to fail loud (UnsupportedSQL -> 422) and route to
source, never to bind a silently-wrong single aggregate.  A lone
``SUM(Sales[Amount])`` (the historically representable shape) still binds.

Run from tessallite/services/query-router/:
    pytest tests/test_bug_7765_single_agg_scalar_wrapper.py -v
"""
from __future__ import annotations

import pytest

from src.parsing.dax_normalizer import parse_dax_to_ir
from src.ir.logical_query import UnsupportedSQL


def _q(expr_body: str):
    return parse_dax_to_ir(
        f'EVALUATE SUMMARIZECOLUMNS(Sales[Region], "Val", {expr_body})',
        "m1",
    )


# ---------------------------------------------------------------------------
# Scalar wrapper around a single aggregate -> refuse (wrapper would be dropped)
# ---------------------------------------------------------------------------

def test_multiply_literal_rejected():
    """SUM(Sales[Amount]) * 1.1 must not bind to Amount (the *1.1 is dropped)."""
    with pytest.raises(UnsupportedSQL):
        _q("SUM(Sales[Amount]) * 1.1")


def test_divide_by_literal_rejected():
    """DIVIDE(SUM(Sales[Amount]), 100) must not bind to Amount (the /100 lost)."""
    with pytest.raises(UnsupportedSQL):
        _q("DIVIDE(SUM(Sales[Amount]), 100)")


def test_add_literal_rejected():
    with pytest.raises(UnsupportedSQL):
        _q("SUM(Sales[Amount]) + 5")


def test_nested_function_wrapper_rejected():
    """A single aggregate wrapped in another scalar function drops the wrapper."""
    with pytest.raises(UnsupportedSQL):
        _q("ROUND(SUM(Sales[Amount]), 2)")


# ---------------------------------------------------------------------------
# Non-SUM aggregate function over a bare column -> refuse (fail-closed).
# Bug-7796: these stay refused because the aggregate route does not read
# measure_agg_overrides -- accepting them would create a route-dependent
# wrong-number path. Opening these requires L3 to wire the aggregate matcher.
# ---------------------------------------------------------------------------

def test_min_aggregate_rejected():
    """MIN(Sales[Amount]) would be served with the measure's default_agg on
    the aggregate route — refuse until L3 wires the matcher check."""
    with pytest.raises(UnsupportedSQL):
        _q("MIN(Sales[Amount])")


def test_max_aggregate_rejected():
    with pytest.raises(UnsupportedSQL):
        _q("MAX(Sales[Amount])")


def test_average_aggregate_rejected():
    with pytest.raises(UnsupportedSQL):
        _q("AVERAGE(Sales[Amount])")


def test_distinctcount_aggregate_rejected():
    with pytest.raises(UnsupportedSQL):
        _q("DISTINCTCOUNT(Sales[Amount])")


# ---------------------------------------------------------------------------
# Representable shapes still bind (no regression / no over-refusal)
# ---------------------------------------------------------------------------

def test_lone_sum_still_binds():
    """A lone SUM(Sales[Amount]) is the representable shape — binds to Amount.
    Bug-7796: now also carries the 'sum' override so the rewriter applies SUM
    regardless of the measure's default_agg."""
    q = _q("SUM(Sales[Amount])")
    assert q.requested_measures == ["Amount"]
    assert q.requested_dimensions == ["Region"]
    assert q.measure_agg_overrides == {"amount": "sum"}


def test_lone_sum_quoted_table_still_binds():
    q = _q("SUM('Sales Table'[Order Amount])")
    assert q.requested_measures == ["Order Amount"]


def test_conflicting_agg_overrides_rejected_bug7796():
    """Bug-7796 R1 finding 8: SUM(Sales[Amount]) + MIN(Sales[Amount]) in the
    same query must be refused — silently picking one override would serve
    the wrong function for the other projection."""
    with pytest.raises(UnsupportedSQL):
        parse_dax_to_ir(
            'EVALUATE SUMMARIZECOLUMNS(Sales[Region], '
            '"Total", SUM(Sales[Amount]), "Low", MIN(Sales[Amount]))',
            "m1",
        )


def test_same_agg_override_twice_accepted_bug7796():
    """Two SUM(Sales[Amount]) aliases with the SAME function is OK — no conflict."""
    q = parse_dax_to_ir(
        'EVALUATE SUMMARIZECOLUMNS(Sales[Region], '
        '"Total1", SUM(Sales[Amount]), "Total2", SUM(Sales[Amount]))',
        "m1",
    )
    assert q.measure_agg_overrides == {"amount": "sum"}


def test_bare_measure_ref_unaffected():
    """A plain measure ref [Revenue] carries no inline aggregate — unaffected."""
    q = parse_dax_to_ir(
        'EVALUATE SUMMARIZECOLUMNS(Sales[Region], "Rev", [Revenue])',
        "m1",
    )
    assert q.requested_measures == ["Revenue"]


def test_calculate_measure_ref_still_binds():
    """CALCULATE([Revenue], FILTER(...)) binds the measure ref, not a wrapper."""
    q = _q('CALCULATE([Revenue], FILTER(Sales, Sales[Channel] = "WEB"))')
    assert q.requested_measures == ["Revenue"]
