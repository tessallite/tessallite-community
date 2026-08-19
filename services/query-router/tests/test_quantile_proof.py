"""Truth-table tests for the pure quantile serve-proof core (spec §12.2/§12.3).

Asserts semantic-identity coverage (I2), exactness (I8), CONT direction
symmetry, DISC direction non-equivalence (the [1,2,3,4] DISC(0.5) DESC=3 vs
ASC=2 counterexample), Decimal fraction fidelity, and the predicate partition
(I5). Correctness axis only: a proven verdict must be a SAME-value serve; every
near-miss must produce a structured miss with the right reason code.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from shared.quantile_contracts import (
    BOUNDED_APPROX,
    EXACT,
    METHOD_CONTINUOUS,
    METHOD_DISCRETE,
    NULL_IGNORE,
    NULL_RESPECT,
    ORDER_ASC,
    ORDER_DESC,
    UNKNOWN,
    QuantileCoverage,
    QuantileReason,
    QuantileRequest,
    to_fraction,
)
from src.routing.quantile_proof import (
    prove_request_coverage,
    prove_semantic_coverage,
)
from src.routing.quantile_predicate import partition_query_filters


def _req(fraction, method, direction, **kw):
    return QuantileRequest(
        request_id=kw.pop("request_id", "r1"),
        semantic_measure_name=kw.pop("measure", "latency"),
        input_expression_fingerprint=kw.pop("fp", "latency:double"),
        fraction=Decimal(fraction),
        method=method,
        order_direction=direction,
        null_policy=kw.pop("null_policy", NULL_IGNORE),
        # Default a known type so direction/method tests aren't tripped by the
        # discrete typed-identity rule; type-specific tests override it.
        value_type=kw.pop("value_type", "double"),
    )


def _cov(fraction, method, direction, exactness=EXACT, **kw):
    return QuantileCoverage(
        physical_column_name=kw.pop("col", "latency__p50"),
        semantic_measure_name=kw.pop("measure", "latency"),
        input_expression_fingerprint=kw.pop("fp", "latency:double"),
        fraction=Decimal(fraction),
        method=method,
        order_direction=direction,
        null_policy=kw.pop("null_policy", NULL_IGNORE),
        value_type=kw.pop("value_type", "double"),
        exactness=exactness,
    )


# --- I2 semantic identity ---------------------------------------------------

def test_exact_cont_p50_matches():
    cov = _cov("0.5", METHOD_CONTINUOUS, ORDER_ASC)
    assert prove_request_coverage(_req("0.5", METHOD_CONTINUOUS, ORDER_ASC), [cov]) is cov


def test_cont_not_served_from_disc_column():
    # p50 CONT request must NOT match a p50 DISC column (§10, different statistic).
    cov = _cov("0.5", METHOD_DISCRETE, ORDER_ASC)
    v = prove_semantic_coverage([_req("0.5", METHOD_CONTINUOUS, ORDER_ASC)], [cov])
    assert not v.matched
    assert v.reason == QuantileReason.METHOD_MISMATCH


def test_disc_not_served_from_cont_column():
    cov = _cov("0.5", METHOD_CONTINUOUS, ORDER_ASC)
    v = prove_semantic_coverage([_req("0.5", METHOD_DISCRETE, ORDER_ASC)], [cov])
    assert not v.matched
    assert v.reason == QuantileReason.METHOD_MISMATCH


# --- CONT direction symmetry vs DISC non-equivalence (reviewer §4.1) --------

def test_cont_desc_p90_served_from_asc_p10():
    # CONT(0.9) DESC == CONT(0.1) ASC positionally (over [1,100] both == 10.9).
    cov = _cov("0.1", METHOD_CONTINUOUS, ORDER_ASC, col="latency__p10")
    req = _req("0.9", METHOD_CONTINUOUS, ORDER_DESC)
    assert prove_request_coverage(req, [cov]) is cov


def test_disc_desc_NOT_served_from_asc_coverage():
    # THE reviewer counterexample: over [1,2,3,4], DISC(0.5) DESC=3, ASC=2.
    # A DESC discrete request must NEVER be served from ascending p50 coverage.
    asc_cov = _cov("0.5", METHOD_DISCRETE, ORDER_ASC)
    req = _req("0.5", METHOD_DISCRETE, ORDER_DESC)
    assert prove_request_coverage(req, [asc_cov]) is None
    v = prove_semantic_coverage([req], [asc_cov])
    assert not v.matched
    assert v.reason == QuantileReason.DIRECTION_MISMATCH
    # ascending_fraction is undefined for discrete DESC (no data-independent equiv)
    assert req.ascending_fraction is None


def test_disc_desc_served_only_from_direction_matched_coverage():
    desc_cov = _cov("0.5", METHOD_DISCRETE, ORDER_DESC)
    req = _req("0.5", METHOD_DISCRETE, ORDER_DESC)
    assert prove_request_coverage(req, [desc_cov]) is desc_cov


# --- I8 exactness -----------------------------------------------------------

def test_approx_column_rejected_in_exact_mode():
    cov = _cov("0.9", METHOD_CONTINUOUS, ORDER_ASC, exactness=BOUNDED_APPROX)
    v = prove_semantic_coverage([_req("0.9", METHOD_CONTINUOUS, ORDER_ASC)], [cov], accuracy_mode=EXACT)
    assert not v.matched
    assert v.reason == QuantileReason.EXACTNESS_UNKNOWN


def test_unknown_legacy_column_rejected_in_exact_mode():
    cov = _cov("0.9", METHOD_CONTINUOUS, ORDER_ASC, exactness=UNKNOWN)
    v = prove_semantic_coverage([_req("0.9", METHOD_CONTINUOUS, ORDER_ASC)], [cov])
    assert not v.matched
    assert v.reason == QuantileReason.EXACTNESS_UNKNOWN


# --- fraction fidelity (§16.14) ---------------------------------------------

def test_arbitrary_decimal_fraction_not_matched_by_float_rounding():
    # PERCENTILE_CONT(0.3333333333) must not alias a p33/p30 column.
    cov = _cov("0.33", METHOD_CONTINUOUS, ORDER_ASC)
    req = _req("0.3333333333", METHOD_CONTINUOUS, ORDER_ASC)
    assert prove_request_coverage(req, [cov]) is None


def test_to_fraction_rejects_python_float():
    # A binary float would corrupt exact equality; must be rejected.
    assert to_fraction(0.5) is None
    assert to_fraction(Decimal("0.5")) == Decimal("0.5")
    assert to_fraction("0.95") == Decimal("0.95")
    assert to_fraction("1.5") is None  # out of range


# --- null policy / value type ------------------------------------------------

def test_null_policy_mismatch_rejected():
    cov = _cov("0.5", METHOD_CONTINUOUS, ORDER_ASC, null_policy=NULL_RESPECT)
    req = _req("0.5", METHOD_CONTINUOUS, ORDER_ASC, null_policy=NULL_IGNORE)
    v = prove_semantic_coverage([req], [cov])
    assert not v.matched
    assert v.reason == QuantileReason.NULL_POLICY_MISMATCH


def test_value_type_mismatch_rejected():
    cov = _cov("0.5", METHOD_DISCRETE, ORDER_ASC, value_type="double")
    req = _req("0.5", METHOD_DISCRETE, ORDER_ASC, value_type="bigint")
    v = prove_semantic_coverage([req], [cov])
    assert not v.matched
    assert v.reason == QuantileReason.VALUE_TYPE_LOSSY


def test_discrete_untyped_request_fails_closed():
    # Fable R1 MEDIUM: DISCRETE returns a data value, so an unknown type on
    # either side cannot prove losslessness -> never serve.
    cov = _cov("0.5", METHOD_DISCRETE, ORDER_ASC, value_type="double")
    req = _req("0.5", METHOD_DISCRETE, ORDER_ASC, value_type=None)
    v = prove_semantic_coverage([req], [cov])
    assert not v.matched
    assert v.reason == QuantileReason.VALUE_TYPE_LOSSY


def test_discrete_untyped_coverage_fails_closed():
    cov = _cov("0.5", METHOD_DISCRETE, ORDER_ASC, value_type=None)
    req = _req("0.5", METHOD_DISCRETE, ORDER_ASC, value_type="double")
    v = prove_semantic_coverage([req], [cov])
    assert not v.matched
    assert v.reason == QuantileReason.VALUE_TYPE_LOSSY


def test_continuous_untyped_request_defers_to_coverage():
    # CONTINUOUS returns an interpolated numeric; an undeclared request type
    # defers to the coverage (matches ordinary additive-read type discipline).
    cov = _cov("0.5", METHOD_CONTINUOUS, ORDER_ASC, value_type="double")
    req = _req("0.5", METHOD_CONTINUOUS, ORDER_ASC, value_type=None)
    assert prove_request_coverage(req, [cov]) is cov


# --- input fingerprint ------------------------------------------------------

def test_input_fingerprint_mismatch_rejected():
    cov = _cov("0.5", METHOD_CONTINUOUS, ORDER_ASC, fp="other_col:double")
    req = _req("0.5", METHOD_CONTINUOUS, ORDER_ASC, fp="latency:double")
    v = prove_semantic_coverage([req], [cov])
    assert not v.matched
    assert v.reason == QuantileReason.INPUT_MISMATCH


# --- multi-request: all-or-nothing (§5.3) -----------------------------------

def test_multi_request_one_uncovered_fails_whole_candidate():
    covs = [_cov("0.5", METHOD_CONTINUOUS, ORDER_ASC, col="latency__p50")]
    reqs = [
        _req("0.5", METHOD_CONTINUOUS, ORDER_ASC, request_id="a"),
        _req("0.9", METHOD_CONTINUOUS, ORDER_ASC, request_id="b"),
    ]
    v = prove_semantic_coverage(reqs, covs)
    assert not v.matched
    assert v.reason == QuantileReason.FRACTION_NOT_COVERED
    assert v.request_ids == ("b",)


def test_multi_request_all_covered_matches():
    covs = [
        _cov("0.5", METHOD_CONTINUOUS, ORDER_ASC, col="latency__p50"),
        _cov("0.9", METHOD_CONTINUOUS, ORDER_ASC, col="latency__p90"),
    ]
    reqs = [
        _req("0.5", METHOD_CONTINUOUS, ORDER_ASC, request_id="a"),
        _req("0.9", METHOD_CONTINUOUS, ORDER_ASC, request_id="b"),
    ]
    v = prove_semantic_coverage(reqs, covs)
    assert v.matched
    assert set(v.request_to_column) == {"a", "b"}
    assert v.request_to_column["b"].physical_column_name == "latency__p90"


# --- predicate partition (I5) -----------------------------------------------

class _F:
    def __init__(self, dim, op):
        self.dimension_name = dim
        self.operator = op


def test_grain_key_filter_is_whole_group_selection():
    part = partition_query_filters([_F("country", "eq")], ["country", "year"])
    assert part.is_direct_read_valid
    assert part.rejection_reason is None


def test_non_grain_filter_is_row_predicate_mismatch():
    # WHERE city='Cairo' against a (country, year) artifact changes group rows.
    part = partition_query_filters([_F("city", "eq")], ["country", "year"])
    assert not part.is_direct_read_valid
    assert part.rejection_reason == QuantileReason.ROW_PREDICATE_MISMATCH


def test_in_and_between_on_grain_keys_valid():
    part = partition_query_filters(
        [_F("country", "in"), _F("year", "between")], ["country", "year"]
    )
    assert part.is_direct_read_valid


def test_unsupported_operator_fails_closed():
    part = partition_query_filters([_F("country", "like")], ["country"])
    assert not part.is_direct_read_valid
    assert part.rejection_reason == QuantileReason.PREDICATE_PROOF_UNSUPPORTED
