"""Pure quantile serve-proof core (spec §4.4, §5.3, §17).

The ONLY module allowed to declare a stored quantile column eligible to serve a
``QuantileRequest``. It is a small pure function with explicit inputs and
exhaustive verdicts — DB loading, ranking, grain/predicate proof, and SQL
rendering live in the matcher/rewriter, not here.

It proves Stage 3 (semantic coverage, spec §5.3): for every request, exactly one
coverage row with identical fraction (ascending-normalised for CONTINUOUS,
direction-identical for DISCRETE), method, order direction, input-expression
fingerprint, null policy, value type, and exactness compatible with the
requested accuracy policy. Any unproven condition is a MISS, never an optimistic
match (fail closed, spec §3).

Grain bijection (I3), key-uniqueness (I3 reviewer), predicate partition (I5) and
security (I7) are enforced by the matcher/router which hold the ORM candidate;
this core assumes they will be checked and refuses only on the semantic-identity
axis it owns. The matcher must call BOTH — this core AND the structural gates —
before serving.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Mapping, Optional

from shared.quantile_contracts import (
    EXACT,
    METHOD_CONTINUOUS,
    METHOD_DISCRETE,
    QuantileCoverage,
    QuantileReason,
    QuantileRequest,
    QuantileServeVerdict,
)


def _norm(text: Optional[str]) -> str:
    return (text or "").strip().lower()


def _value_type_compatible(
    request_type: Optional[str],
    coverage_type: Optional[str],
    method: str = METHOD_CONTINUOUS,
) -> bool:
    """Whether a stored column type can faithfully represent the request domain.

    Spec I2 / §10 lossy-type row. A DISCRETE quantile returns an actual stored
    DATA value, so a lossy column type (e.g. BIGINT input stored in a DOUBLE
    column, values above 2^53) corrupts it — DISCRETE therefore requires PROVEN
    type identity: BOTH sides must declare a value_type and they must be equal.
    Any unknown on either side for a discrete request fails closed
    (``VALUE_TYPE_LOSSY``). A CONTINUOUS quantile returns an interpolated numeric
    and follows the same type discipline as ordinary additive aggregate reads:
    a declared mismatch fails; an undeclared request type defers to the coverage.

    (Fable R1 MEDIUM: the previous rule let unknown-vs-unknown serve a discrete
    lossy column. Discrete now never serves without a proven type match.)
    """
    rt = _norm(request_type)
    ct = _norm(coverage_type)
    if _norm(method) == METHOD_DISCRETE:
        # Discrete returns a data value -> require proven identical types.
        return bool(rt) and bool(ct) and rt == ct
    if not rt:
        return True
    if not ct:
        return False
    return rt == ct


def prove_request_coverage(
    request: QuantileRequest,
    coverages: Iterable[QuantileCoverage],
    *,
    accuracy_mode: str = EXACT,
) -> Optional[QuantileCoverage]:
    """Return the single coverage row that proves ``request``, or None.

    Semantic-identity proof (I2) across fraction, method, direction, input
    fingerprint, null policy, value type, and exactness. Returns the FIRST fully
    matching coverage; the caller guarantees at most one artifact column per
    ``(measure, method, fraction)`` so a match is unambiguous.
    """
    for cov in coverages:
        if _norm(cov.semantic_measure_name) != _norm(request.semantic_measure_name):
            continue
        # I2: bound input expression must be identical.
        if _norm(cov.input_expression_fingerprint) != _norm(request.input_expression_fingerprint):
            continue
        # I2: method identity — p50 CONT is not p50 DISC.
        if _norm(cov.method) != _norm(request.method):
            continue
        # I2: fraction identity. CONTINUOUS uses ascending-normalised equality
        # (DESC p == ASC 1-p, positionally symmetric). DISCRETE requires the
        # IDENTICAL (fraction, direction) pair — a DESC discrete has no
        # ascending equivalent (reviewer §4.1), so request.ascending_fraction is
        # None and we match direction + raw fraction instead.
        if request.method == METHOD_CONTINUOUS:
            req_frac = request.ascending_fraction
            cov_frac = cov.ascending_fraction
            if req_frac is None or cov_frac is None or req_frac != cov_frac:
                continue
        else:
            # discrete: direction AND raw fraction must be identical.
            if _norm(cov.order_direction) != _norm(request.order_direction):
                continue
            if not _decimal_eq(cov.fraction, request.fraction):
                continue
        # I2: null policy.
        if _norm(cov.null_policy) != _norm(request.null_policy):
            continue
        # I2: value type losslessness.
        if not _value_type_compatible(request.value_type, cov.value_type, request.method):
            continue
        # I2: collation / timezone identity when either side declares one.
        if not _optional_eq(request.collation, cov.collation):
            continue
        if not _optional_eq(request.timezone, cov.timezone):
            continue
        # I8: exact mode consumes only certified-exact production.
        if accuracy_mode == EXACT and _norm(cov.exactness) != EXACT:
            continue
        return cov
    return None


def _decimal_eq(a: Decimal, b: Decimal) -> bool:
    try:
        return Decimal(a) == Decimal(b)
    except Exception:
        return False


def _optional_eq(a: Optional[str], b: Optional[str]) -> bool:
    """Equal when both absent, or both present and equal (case-insensitive).

    A declared value on one side and absent on the other is NOT provably equal
    -> fail closed.
    """
    na, nb = _norm(a), _norm(b)
    if not na and not nb:
        return True
    return na == nb


def _diagnose_miss(
    request: QuantileRequest,
    coverages: list[QuantileCoverage],
    accuracy_mode: str,
) -> str:
    """Pick the most specific stable reason code for a request that found no
    proving coverage. Purely diagnostic — correctness already routed to source.
    """
    same_measure = [
        c for c in coverages
        if _norm(c.semantic_measure_name) == _norm(request.semantic_measure_name)
    ]
    if not same_measure:
        return QuantileReason.INPUT_MISMATCH
    same_input = [
        c for c in same_measure
        if _norm(c.input_expression_fingerprint) == _norm(request.input_expression_fingerprint)
    ]
    if not same_input:
        return QuantileReason.INPUT_MISMATCH
    same_method = [c for c in same_input if _norm(c.method) == _norm(request.method)]
    if not same_method:
        return QuantileReason.METHOD_MISMATCH
    # Fraction / direction.
    if request.method == METHOD_CONTINUOUS:
        req_frac = request.ascending_fraction
        if not any(
            req_frac is not None and c.ascending_fraction == req_frac for c in same_method
        ):
            return QuantileReason.FRACTION_NOT_COVERED
    else:
        same_dir = [
            c for c in same_method
            if _norm(c.order_direction) == _norm(request.order_direction)
        ]
        if not same_dir:
            return QuantileReason.DIRECTION_MISMATCH
        if not any(_decimal_eq(c.fraction, request.fraction) for c in same_dir):
            return QuantileReason.FRACTION_NOT_COVERED
    # Same fraction/method/direction exists but something else differed.
    fq = [
        c for c in same_method
        if (request.method == METHOD_CONTINUOUS and c.ascending_fraction == request.ascending_fraction)
        or (request.method != METHOD_CONTINUOUS and _decimal_eq(c.fraction, request.fraction)
            and _norm(c.order_direction) == _norm(request.order_direction))
    ]
    if any(_norm(c.null_policy) != _norm(request.null_policy) for c in fq):
        return QuantileReason.NULL_POLICY_MISMATCH
    if any(not _value_type_compatible(request.value_type, c.value_type, request.method) for c in fq):
        return QuantileReason.VALUE_TYPE_LOSSY
    if accuracy_mode == EXACT and all(_norm(c.exactness) != EXACT for c in fq):
        return QuantileReason.EXACTNESS_UNKNOWN
    return QuantileReason.FRACTION_NOT_COVERED


def prove_semantic_coverage(
    requests: Iterable[QuantileRequest],
    coverages: Iterable[QuantileCoverage],
    *,
    accuracy_mode: str = EXACT,
) -> QuantileServeVerdict:
    """Stage 3 (spec §5.3): every request must be covered by ONE artifact.

    Returns a matched verdict (request_id -> coverage) only when EVERY request
    is proven by a coverage row of this single candidate. A single uncovered
    request fails the whole candidate with the most specific reason (§5.1: a
    partially accelerated plan would violate the exact-grain obligation).
    """
    cov_list = list(coverages)
    req_list = list(requests)
    if not req_list:
        # No quantile requests -> the quantile proof imposes nothing (the caller
        # only invokes this when the inventory is non-empty; an empty inventory
        # is not this core's concern).
        return QuantileServeVerdict.match({})
    mapping: dict[str, QuantileCoverage] = {}
    for req in req_list:
        cov = prove_request_coverage(req, cov_list, accuracy_mode=accuracy_mode)
        if cov is None:
            reason = _diagnose_miss(req, cov_list, accuracy_mode)
            return QuantileServeVerdict.miss(
                reason,
                message=f"no exact coverage for {req.semantic_measure_name} "
                        f"{req.method} p{req.fraction}",
                request_ids=(req.request_id,),
            )
        mapping[req.request_id] = cov
    return QuantileServeVerdict.match(mapping)


def coverages_from_columns(agg_columns: Iterable, coverage_by_column_id: Mapping) -> list[QuantileCoverage]:
    """Build the ``QuantileCoverage`` list for a candidate from its persisted
    coverage rows, keyed by ``AggregateColumn.id``.

    A pNN ``AggregateColumn`` WITHOUT a persisted coverage row is NOT
    represented (I8/Gap D: the suffix is not proof; an un-covered legacy column
    is treated as ``unknown`` and therefore ineligible in exact mode). This
    keeps the pure core free of ORM types — the matcher passes the already
    hydrated coverage objects.
    """
    out: list[QuantileCoverage] = []
    for col in agg_columns:
        cov = coverage_by_column_id.get(getattr(col, "id", None))
        if cov is not None:
            out.append(cov)
    return out
