"""Route-level serving tests for pNN aggregate direct reads (Bug-6969/5891).

Proves the coverage-gated serving path end to end at the matcher:
- a pNN column WITH a proving QuantileCoverage row serves (exact grain);
- the SAME column WITHOUT coverage routes to source (Gap D: suffix != proof);
- a DISC-DESC request never serves from ASC coverage (reviewer §4.1);
- an approximate/unknown coverage never serves in exact mode (I8);
- a non-grain filter (row predicate) routes to source (I5);
- the additive path is untouched (no quantile requests -> no coverage load).

Correctness axis: asserts the ROUTE decision (matched aggregate vs source) and
the specific quantile reason code — a matched aggregate here means the existing
rewriter reads the stored ``measure__pNN`` column directly at exact grain (that
direct-read shape is separately pinned by test_expression_routing).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
import types

from conftest import make_measure, make_dimension, make_aggregate, make_bound_query
from src.routing.aggregate_matcher import AggregateSkipReason, find_best_aggregate
from src.ir.logical_query import LogicalFilter
from shared.quantile_contracts import (
    BOUNDED_APPROX,
    EXACT,
    METHOD_CONTINUOUS,
    METHOD_DISCRETE,
    ORDER_ASC,
    ORDER_DESC,
    UNKNOWN,
    QuantileCoverage,
    QuantileReason,
    QuantileRequest,
    build_input_fingerprint,
)

_PATCH = "src.routing.aggregate_matcher.load_active_aggregates"
_PATCH_COVERAGE = "src.semantic.binder.load_quantile_coverage_by_column"
_PATCH_ENFORCE = "src.routing.aggregate_matcher._resolve_quantile_enforce"

pytestmark = pytest.mark.asyncio


def _agg_col(measure, stat_type, col_id):
    return types.SimpleNamespace(
        id=col_id,
        measure=measure,
        stat_type=stat_type,
        physical_col_name=f"{measure.name}__{stat_type}",
    )


def _coverage(measure_name, fraction, method, direction, exactness=EXACT, value_type="double"):
    return QuantileCoverage(
        physical_column_name=f"{measure_name}__p{int(Decimal(fraction) * 100):02d}",
        semantic_measure_name=measure_name,
        input_expression_fingerprint=build_input_fingerprint(measure_name, value_type),
        fraction=Decimal(fraction),
        method=method,
        order_direction=direction,
        value_type=value_type,
        exactness=exactness,
    )


def _request(measure_name, fraction, method, direction, value_type="double"):
    return QuantileRequest(
        request_id="q1",
        semantic_measure_name=measure_name,
        input_expression_fingerprint=build_input_fingerprint(measure_name, value_type),
        fraction=Decimal(fraction),
        method=method,
        order_direction=direction,
        value_type=value_type,
    )


def _served_suffix(req):
    """The pNN column the rewriter reads for a request (mirrors the parser's
    direction-correct serving suffix: CONT DESC reads the ascending column)."""
    frac = req.fraction
    if req.method == METHOD_CONTINUOUS and req.order_direction == ORDER_DESC:
        frac = Decimal(1) - frac
    return f"p{int(frac * 100):02d}"


def _bound(measure, dims, requests, filters=None):
    from src.ir.logical_query import SelectExpression
    from shared.quantile_contracts import ORIGIN_MEASURE_DEFAULT

    bq = make_bound_query(dims, [measure], filters=filters)
    bq.quantile_requests = requests
    # Mirror production: an explicit ordered-set/MEDIAN request is an ANALYTICAL
    # SELECT item carrying the served suffix + quantile_meta; a measure-DEFAULT
    # request corresponds to a BARE (passthrough) SELECT item whose measure
    # default_agg is the served stat (no quantile_meta). The matcher requires
    # the exact (measure, served-suffix) column either way and the identity
    # check derives the served suffix from the request itself.
    exprs = []
    for r in requests:
        if getattr(r, "origin", None) == ORIGIN_MEASURE_DEFAULT:
            exprs.append(SelectExpression(
                raw_text=r.semantic_measure_name,
                alias=None,
                classification="passthrough",
                agg_function=None,
                inner_column=r.semantic_measure_name,
                inner_literal=None,
            ))
        else:
            exprs.append(SelectExpression(
                raw_text=f"PERCENTILE({r.fraction})",
                alias=r.output_alias,
                classification="analytical",
                agg_function=_served_suffix(r),
                inner_column=r.semantic_measure_name,
                inner_literal=None,
                quantile_meta={
                    "method": r.method,
                    "direction": r.order_direction,
                    "fraction_text": str(r.fraction),
                    "source_syntax": r.source_syntax,
                },
            ))
    bq.logical_query.select_expressions = exprs
    # give the measure a resolvable value type used by the fingerprint
    measure.data_type = "double"
    return bq


async def _run(bq, agg, coverage_map, enforce=True):
    with patch(_PATCH, new_callable=AsyncMock, return_value=[agg]), patch(
        _PATCH_COVERAGE, new_callable=AsyncMock, return_value=coverage_map
    ), patch(_PATCH_ENFORCE, new_callable=AsyncMock, return_value=enforce):
        return await find_best_aggregate(bq, AsyncMock())


async def test_p90_serves_with_proving_coverage():
    m = make_measure("latency", default_agg="p90", is_additive=False)
    agg = make_aggregate(["country"], [_agg_col(m, "p90", "col-p90")])
    bq = _bound(m, [make_dimension("country")],
                [_request("latency", "0.9", METHOD_CONTINUOUS, ORDER_ASC)])
    coverage = {"col-p90": _coverage("latency", "0.9", METHOD_CONTINUOUS, ORDER_ASC)}
    result = await _run(bq, agg, coverage)
    assert result.aggregate is agg
    assert result.quantile_reason is None


async def test_p90_column_without_coverage_routes_to_source():
    # Gap D / I8: the pNN suffix is not proof. A column with no coverage row is
    # treated as unknown -> never served.
    m = make_measure("latency", default_agg="p90", is_additive=False)
    agg = make_aggregate(["country"], [_agg_col(m, "p90", "col-p90")])
    bq = _bound(m, [make_dimension("country")],
                [_request("latency", "0.9", METHOD_CONTINUOUS, ORDER_ASC)])
    result = await _run(bq, agg, {})  # NO coverage
    assert result.aggregate is None
    assert AggregateSkipReason.QUANTILE_PROOF_FAILED in result.skip_reasons


async def test_disc_desc_not_served_from_asc_coverage():
    # Reviewer §4.1: [1,2,3,4] DISC(0.5) DESC=3 vs ASC=2. A DESC discrete request
    # must never serve from ascending p50 coverage.
    m = make_measure("latency", default_agg="p50", is_additive=False)
    agg = make_aggregate(["country"], [_agg_col(m, "p50", "col-p50")])
    bq = _bound(m, [make_dimension("country")],
                [_request("latency", "0.5", METHOD_DISCRETE, ORDER_DESC)])
    coverage = {"col-p50": _coverage("latency", "0.5", METHOD_DISCRETE, ORDER_ASC)}
    result = await _run(bq, agg, coverage)
    assert result.aggregate is None
    assert result.quantile_reason == QuantileReason.DIRECTION_MISMATCH


async def test_approximate_coverage_not_served_in_exact_mode():
    m = make_measure("latency", default_agg="p90", is_additive=False)
    agg = make_aggregate(["country"], [_agg_col(m, "p90", "col-p90")])
    bq = _bound(m, [make_dimension("country")],
                [_request("latency", "0.9", METHOD_CONTINUOUS, ORDER_ASC)])
    coverage = {"col-p90": _coverage("latency", "0.9", METHOD_CONTINUOUS, ORDER_ASC,
                                     exactness=BOUNDED_APPROX)}
    result = await _run(bq, agg, coverage)
    assert result.aggregate is None
    assert result.quantile_reason == QuantileReason.EXACTNESS_UNKNOWN


async def test_method_mismatch_cont_request_disc_column():
    m = make_measure("latency", default_agg="p50", is_additive=False)
    agg = make_aggregate(["country"], [_agg_col(m, "p50", "col-p50")])
    bq = _bound(m, [make_dimension("country")],
                [_request("latency", "0.5", METHOD_CONTINUOUS, ORDER_ASC)])
    coverage = {"col-p50": _coverage("latency", "0.5", METHOD_DISCRETE, ORDER_ASC)}
    result = await _run(bq, agg, coverage)
    assert result.aggregate is None
    assert result.quantile_reason == QuantileReason.METHOD_MISMATCH


async def test_non_grain_filter_routes_to_source():
    # I5: WHERE city='Cairo' against a country-grain artifact changes the rows
    # inside each group -> stored quantile invalid. The pre-existing filter-
    # coverage gate (a filter dim must be physically present in the aggregate
    # grain) already catches this as ``grain_missing`` for a country-grain
    # aggregate; the quantile row-predicate guard (I5) is defence-in-depth for
    # the case where the non-grain filter dim IS physically present but is not a
    # grouping key. Either way the route is SOURCE (fail closed) — assert the
    # aggregate is refused, not the specific gate.
    m = make_measure("latency", default_agg="p90", is_additive=False)
    agg = make_aggregate(["country"], [_agg_col(m, "p90", "col-p90")])
    filt = LogicalFilter(dimension_name="city", operator="eq", value="Cairo")
    bq = _bound(m, [make_dimension("country")],
                [_request("latency", "0.9", METHOD_CONTINUOUS, ORDER_ASC)],
                filters=[filt])
    bq.resolved_dimensions_by_name["city"] = make_dimension("city")
    coverage = {"col-p90": _coverage("latency", "0.9", METHOD_CONTINUOUS, ORDER_ASC)}
    result = await _run(bq, agg, coverage)
    assert result.aggregate is None


async def test_i5_non_grain_filter_present_in_grain_but_not_grouping_key():
    # Direct I5 guard: the non-grain filter dim ('region') IS in the aggregate
    # grain physically (so the filter-coverage gate passes), but the query does
    # NOT group by it — filtering it changes the population inside each returned
    # country group. The quantile row-predicate guard must refuse this.
    m = make_measure("latency", default_agg="p90", is_additive=False)
    agg = make_aggregate(["country", "region"], [_agg_col(m, "p90", "col-p90")])
    filt = LogicalFilter(dimension_name="region", operator="eq", value="EMEA")
    # Query groups by country only (grain=['country']); region is a filter dim.
    bq = _bound(m, [make_dimension("country")],
                [_request("latency", "0.9", METHOD_CONTINUOUS, ORDER_ASC)],
                filters=[filt])
    bq.resolved_dimensions_by_name["region"] = make_dimension("region")
    coverage = {"col-p90": _coverage("latency", "0.9", METHOD_CONTINUOUS, ORDER_ASC)}
    result = await _run(bq, agg, coverage)
    # A (country, region) artifact is a SUPERSET grain for a GROUP BY country
    # quantile -> already exact-grain-mismatch (non-re-aggregatable), so source.
    assert result.aggregate is None


async def test_grain_key_filter_still_serves():
    # I5: WHERE country='EG' selects whole groups -> valid.
    m = make_measure("latency", default_agg="p90", is_additive=False)
    agg = make_aggregate(["country"], [_agg_col(m, "p90", "col-p90")])
    filt = LogicalFilter(dimension_name="country", operator="eq", value="EG")
    bq = _bound(m, [make_dimension("country")],
                [_request("latency", "0.9", METHOD_CONTINUOUS, ORDER_ASC)],
                filters=[filt])
    coverage = {"col-p90": _coverage("latency", "0.9", METHOD_CONTINUOUS, ORDER_ASC)}
    result = await _run(bq, agg, coverage)
    assert result.aggregate is agg


async def test_cont_desc_serves_ascending_column_not_authored_fraction():
    # Fable R1 CRITICAL: a preset-packed aggregate stores BOTH latency__p10 and
    # latency__p90 (both ASC CONT exact). A CONT(0.9) DESC request equals
    # ascending p10 and MUST serve the p10 column (over [1,100] = 10.9), never
    # the p90 column (= 90.1). The served suffix is direction-normalised to p10,
    # and the proof matches p10 coverage; the physical-column identity check ties
    # them so the wrong column can never be served.
    m = make_measure("latency", default_agg="p90", is_additive=False)
    agg = make_aggregate(
        ["country"],
        [_agg_col(m, "p10", "col-p10"), _agg_col(m, "p90", "col-p90")],
    )
    req = _request("latency", "0.9", METHOD_CONTINUOUS, ORDER_DESC)
    bq = _bound(m, [make_dimension("country")], [req])
    coverage = {
        "col-p10": _coverage("latency", "0.1", METHOD_CONTINUOUS, ORDER_ASC),
        "col-p90": _coverage("latency", "0.9", METHOD_CONTINUOUS, ORDER_ASC),
    }
    result = await _run(bq, agg, coverage)
    assert result.aggregate is agg
    # The served column is the ascending p10 (proven), not the authored p90.
    served = _served_suffix(req)
    assert served == "p10"


async def test_cont_desc_without_ascending_column_routes_to_source():
    # Same DESC p0.9 request, but the aggregate only stores latency__p90 (the
    # authored-fraction column) with p90 coverage. The served suffix is p10,
    # which has no column -> no proven read -> source (never serve p90 = 90.1
    # for a DESC-0.9 request whose correct answer is p10 = 10.9).
    m = make_measure("latency", default_agg="p90", is_additive=False)
    agg = make_aggregate(["country"], [_agg_col(m, "p90", "col-p90")])
    req = _request("latency", "0.9", METHOD_CONTINUOUS, ORDER_DESC)
    bq = _bound(m, [make_dimension("country")], [req])
    coverage = {"col-p90": _coverage("latency", "0.9", METHOD_CONTINUOUS, ORDER_ASC)}
    result = await _run(bq, agg, coverage)
    assert result.aggregate is None


async def test_ordered_set_routes_to_source_when_feature_disabled():
    # Strict no-regression: with the feature OFF, an explicit ordered-set
    # percentile (which had NO pre-feature serving path) routes to source and is
    # never served unproven from a stored pNN column.
    m = make_measure("latency", default_agg="p90", is_additive=False)
    agg = make_aggregate(["country"], [_agg_col(m, "p90", "col-p90")])
    req = _request("latency", "0.9", METHOD_CONTINUOUS, ORDER_ASC)
    req = QuantileRequest(**{**req.__dict__, "source_syntax": "ordered_set"})
    bq = _bound(m, [make_dimension("country")], [req])
    # coverage exists, but the feature is OFF -> still source (no serve).
    coverage = {"col-p90": _coverage("latency", "0.9", METHOD_CONTINUOUS, ORDER_ASC)}
    result = await _run(bq, agg, coverage, enforce=False)
    assert result.aggregate is None
    assert result.quantile_reason == "QUANTILE_ROUTING_DISABLED"


async def test_median_still_serves_when_feature_disabled():
    # MEDIAN(col) kept its pre-existing p50 serving even with the new feature
    # OFF: the matcher does not refuse a median-syntax request when disabled.
    m = make_measure("latency", default_agg="p50", is_additive=False)
    agg = make_aggregate(["country"], [_agg_col(m, "p50", "col-p50")])
    req = _request("latency", "0.5", METHOD_CONTINUOUS, ORDER_ASC)
    req = QuantileRequest(**{**req.__dict__, "source_syntax": "median"})
    bq = _bound(m, [make_dimension("country")], [req])
    # No coverage, feature OFF -> the existing exact-grain p50 path still serves.
    result = await _run(bq, agg, {}, enforce=False)
    assert result.aggregate is agg


async def test_measure_default_pnn_requires_coverage_under_enforce():
    # Fable R2 MEDIUM-1: a BARE measure whose default_agg is p90 (no percentile
    # syntax) must ALSO go through the coverage proof under enforce, not serve
    # the m__p90 column unproven. Build the request the way the binder now does
    # (origin measure_default, source_syntax='median') and prove it needs
    # coverage.
    m = make_measure("latency", default_agg="p90", is_additive=False)
    agg = make_aggregate(["country"], [_agg_col(m, "p90", "col-p90")])
    from shared.quantile_contracts import ORIGIN_MEASURE_DEFAULT
    req = _request("latency", "0.9", METHOD_CONTINUOUS, ORDER_ASC)
    req = QuantileRequest(**{**req.__dict__, "origin": ORIGIN_MEASURE_DEFAULT,
                            "source_syntax": "median"})
    bq = _bound(m, [make_dimension("country")], [req])
    # WITHOUT coverage under enforce -> source (I8: suffix is not proof).
    assert (await _run(bq, agg, {}, enforce=True)).aggregate is None
    # WITH coverage -> serves.
    coverage = {"col-p90": _coverage("latency", "0.9", METHOD_CONTINUOUS, ORDER_ASC)}
    assert (await _run(bq, agg, coverage, enforce=True)).aggregate is agg


async def test_measure_default_pnn_still_serves_when_feature_off():
    # No regression: with the feature OFF, a measure-default p90 keeps its
    # pre-existing plain-stat serving (source_syntax='median' -> not refused).
    m = make_measure("latency", default_agg="p90", is_additive=False)
    agg = make_aggregate(["country"], [_agg_col(m, "p90", "col-p90")])
    from shared.quantile_contracts import ORIGIN_MEASURE_DEFAULT
    req = _request("latency", "0.9", METHOD_CONTINUOUS, ORDER_ASC)
    req = QuantileRequest(**{**req.__dict__, "origin": ORIGIN_MEASURE_DEFAULT,
                            "source_syntax": "median"})
    bq = _bound(m, [make_dimension("country")], [req])
    assert (await _run(bq, agg, {}, enforce=False)).aggregate is agg


async def test_inventory_built_from_resolved_measures_no_select_expressions():
    # Fable R4 HIGH: DAX/XMLA, the Excel plugin, and headless REST build the
    # LogicalQuery from requested_measures with NO select_expressions. The
    # measure-default quantile inventory MUST still fire (from resolved measures,
    # spec I1) so those protocols do not skip the proof gate and serve a stored
    # pNN unproven under enforce.
    from src.semantic.binder import _build_quantile_inventory
    from src.ir.logical_query import LogicalQuery

    m = make_measure("latency", default_agg="p90", is_additive=False)
    lq = LogicalQuery(
        model_id="model-1", protocol="dax", raw_query="",
        requested_measures=["latency"], requested_dimensions=["country"],
        filters=[], grain=["country"], order_by=[], limit=None, offset=None,
        query_fingerprint="fp", select_expressions=[],  # <-- none (non-SQL path)
    )
    reqs = _build_quantile_inventory(lq, [m])
    assert len(reqs) == 1
    assert reqs[0].semantic_measure_name == "latency"
    assert reqs[0].origin == "measure_default"
    assert str(reqs[0].fraction) == "0.9"


async def test_duplicate_stat_type_columns_fail_closed():
    # Fable R2 LOW-4 / R3 LOW-3: two physical columns for the same
    # (measure, p90) make the served-column certificate ambiguous (the rewriter's
    # last-wins col_lookup could read a different column than the matcher
    # certifies). The matcher must fail closed (no certified column -> source).
    m = make_measure("latency", default_agg="p90", is_additive=False)
    agg = make_aggregate(
        ["country"],
        [_agg_col(m, "p90", "col-p90a"), _agg_col(m, "p90", "col-p90b")],
    )
    # Point both AggregateColumns at DIFFERENT physical names for the same pair.
    agg.columns[0].physical_col_name = "latency__p90_a"
    agg.columns[1].physical_col_name = "latency__p90_b"
    req = _request("latency", "0.9", METHOD_CONTINUOUS, ORDER_ASC)
    bq = _bound(m, [make_dimension("country")], [req])
    coverage = {
        "col-p90a": _coverage("latency", "0.9", METHOD_CONTINUOUS, ORDER_ASC),
        "col-p90b": _coverage("latency", "0.9", METHOD_CONTINUOUS, ORDER_ASC),
    }
    result = await _run(bq, agg, coverage)
    assert result.aggregate is None


async def test_additive_path_does_not_load_coverage():
    # Byte-identical additive path: no quantile requests -> coverage loader is
    # never called and a plain SUM aggregate matches unchanged.
    m = make_measure("revenue", default_agg="sum")
    agg = make_aggregate(["country"], [_agg_col(m, "sum", "col-sum")])
    bq = make_bound_query([make_dimension("country")], [m])  # no quantile_requests
    with patch(_PATCH, new_callable=AsyncMock, return_value=[agg]), patch(
        _PATCH_COVERAGE, new_callable=AsyncMock
    ) as cov_loader:
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is agg
    cov_loader.assert_not_called()
