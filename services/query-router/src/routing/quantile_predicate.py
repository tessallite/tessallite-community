"""Predicate partition for quantile direct-read proofs (spec §4.3, §5.5).

A stored scalar quantile may be filtered at runtime ONLY by a predicate that
selects whole pre-computed groups — every referenced expression must be a
grouping key (I5). A predicate that changes the rows INSIDE any group (a
non-grain filter) invalidates the stored value and routes to source.

This module partitions the query's resolved filters relative to the exact
artifact grain into key-predicates (safe) and row-predicates (fatal), using the
engine's existing ``LogicalFilter`` IR. It is deliberately finite (spec §4.3):
the router already bails to source on any WHERE it cannot represent as a
``LogicalFilter`` (``has_unresolvable_where`` -> UNRESOLVABLE_WHERE in the
matcher), so every filter reaching here is a representable
``(dimension, operator, value)`` triple. Under that contract the only proof
obligation left for a pNN direct read is:

    every resolved filter dimension is a grain key of the exact-grain artifact.

Because current aggregates carry NO build row/key predicate (they materialise
the whole population at their grain), ``Pa_row`` is TRUE and ``Pa_key`` is TRUE,
so ``Pq_row`` must be empty (no non-grain filter) and ``Pq_key`` trivially
implies ``Pa_key``. A non-empty ``Pq_row`` is ``QUANTILE_ROW_PREDICATE_MISMATCH``.

The finite operator set (=, !=, <, <=, >, >=, IN, BETWEEN, IS NULL,
IS NOT NULL) matches the representable ``LogicalFilter`` operators; anything
outside it never becomes a ``LogicalFilter`` and has already routed to source.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from shared.quantile_contracts import QuantileReason


# Operators the ``LogicalFilter`` IR can carry AND the proof language supports
# as whole-group key predicates (spec §4.3). This is the intersection of "the
# parser can represent it" and "the prover has a sound rule for it".
_SUPPORTED_KEY_OPERATORS = frozenset(
    {"eq", "neq", "lt", "lte", "gt", "gte", "in", "between", "is_null", "is_not_null"}
)


@dataclass(frozen=True)
class PredicatePartition:
    """Result of partitioning query filters relative to an exact artifact grain.

    ``key_dimensions`` are filters on grain keys (whole-group selection, safe).
    ``row_dimensions`` are filters on non-grain expressions (change group
    membership, fatal). ``unsupported`` marks any filter whose operator the
    proof language cannot decide (fail closed).
    """
    key_dimensions: tuple = ()
    row_dimensions: tuple = ()
    unsupported: tuple = ()

    @property
    def is_direct_read_valid(self) -> bool:
        """A pNN direct read is valid only when NO filter touches a non-grain
        expression and every filter operator is decidable. Current aggregates
        have no build predicate, so key predicates always imply the (TRUE)
        artifact key predicate; the only failure modes are a row predicate or an
        unsupported operator.
        """
        return not self.row_dimensions and not self.unsupported

    @property
    def rejection_reason(self) -> Optional[str]:
        if self.unsupported:
            return QuantileReason.PREDICATE_PROOF_UNSUPPORTED
        if self.row_dimensions:
            return QuantileReason.ROW_PREDICATE_MISMATCH
        return None


def partition_query_filters(
    resolved_filters: Iterable,
    grain_keys: Iterable[str],
    *,
    canonicalize: Optional[callable] = None,
) -> PredicatePartition:
    """Partition ``resolved_filters`` relative to the artifact ``grain_keys``.

    ``canonicalize`` maps a dimension name to its canonical grain name so a
    hierarchy-alias filter is compared against the physically stored key (mirrors
    the matcher's canonical grain comparison). When absent, raw-name comparison
    is used.

    A filter whose dimension canonicalises into ``grain_keys`` is a key
    predicate (whole-group selection). A filter on any other dimension is a row
    predicate (changes group membership -> ineligible). An operator outside the
    supported set is unsupported (fail closed).
    """
    _canon = canonicalize or (lambda n: n)
    canon_grain = {_canon(k) for k in grain_keys}
    key_dims: list[str] = []
    row_dims: list[str] = []
    unsupported: list[str] = []
    for filt in resolved_filters:
        op = (getattr(filt, "operator", "") or "").lower()
        dim = getattr(filt, "dimension_name", None)
        if op not in _SUPPORTED_KEY_OPERATORS:
            unsupported.append(dim)
            continue
        if _canon(dim) in canon_grain:
            key_dims.append(dim)
        else:
            # A representable filter on a non-grain dimension changes the
            # population inside groups -> stored scalar quantile is invalid.
            row_dims.append(dim)
    return PredicatePartition(
        key_dimensions=tuple(key_dims),
        row_dimensions=tuple(row_dims),
        unsupported=tuple(unsupported),
    )
