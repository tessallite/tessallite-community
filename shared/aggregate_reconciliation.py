"""Sealed-bucket drift comparison for the aggregate periodic full reconciliation.

Bug-8768 / Bug-8818, Adjustment 5. The AGGREGATE incremental leg
(``shared/aggregate_incremental.py`` + ``services/scheduler/src/jobs/
incremental_refresh.py``) patches only the buckets at or above the watermark and
leaves the buckets below it — the *sealed* buckets — untouched, on the strength of
a user-declared append-only contract. This module owns the periodic *proof* that
the contract still holds: it compares the sealed buckets the incremental leg has
been carrying forward against the sealed buckets a fresh full rebuild produces
from current source truth. Any difference below the sealed watermark is drift the
append-only contract promised would never happen.

Why *sealed* buckets only
-------------------------
The most recent incremental run re-derived every bucket at or above its window
start from current source truth, so those buckets already match a full rebuild by
construction and comparing them proves nothing. Normal new rows also land there.
Only buckets STRICTLY BELOW that window start ("sealed") are the ones the
incremental strategy assumed were frozen — so only those can reveal an
append-only violation. The scheduler filters both reads with the SAME
``partition < window`` predicate before handing the rows here (a NULL partition
value is excluded on both sides, matching the leg's NULL bucket being re-derived
every run rather than sealed).

Why a per-bucket comparison, not a checksum
-------------------------------------------
A below-window row-count total or a single ``SUM`` checksum misses net-cancelling
changes (one bucket +10, another -10) and intra-window restatements — the exact
partial-detector escalation shape CLAUDE.md and the pocket sibling both reject.
This module keys every sealed bucket by its grain tuple and compares the measure
tuple per key, so a MISSING bucket (deleted history), an ADDITIONAL bucket (a
late row below the lookback), and a CHANGED bucket (an in-place restatement, a
grain re-pointing, a joined-dimension edit) are each detected independently and
cannot cancel each other.

This module is deliberately pure — no database, no I/O. The scheduler reads the
two sealed-bucket sets and acts on the result (raise a ``ModelAlert`` and suspend
further incremental runs on drift); this module only decides whether they differ.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "SealedBucketDrift",
    "compare_sealed_buckets",
    "FLOAT_MAX_ULPS",
]

# EXACT database numerics — ``int`` (BIGINT/INTEGER) and ``Decimal`` (NUMERIC) —
# are compared EXACTLY, with ZERO tolerance. A NUMERIC or integer sealed bucket
# that moved by a single unit is drift, and it MUST be caught: coercing such a
# value to binary ``float`` before comparison silently loses that unit above
# 2**53, so ``Decimal("1000000000000")`` vs ``Decimal("1000000000001")`` and
# ``9007199254740992`` vs ``9007199254740993`` both collapse to "equal" (the
# reported false-negative, B8768-R1-03). SUM/COUNT/MIN/MAX over NUMERIC/INTEGER
# columns is exact arithmetic on both the incremental and the full-rebuild side,
# so exact comparison never false-alarms on them.
#
# Native ``float`` (DOUBLE PRECISION) measures are the only ones that can differ
# in the last bits purely from summation-ORDER noise between a full rebuild and
# the incrementally-carried value. Those get a SMALL, magnitude-scaled ULP bound
# — never an unbounded relative tolerance — so a genuine restatement is still
# detected while true last-bit reorder noise is ignored. Erring toward detection
# is the safe bias: a false drift alarm suspends incremental and runs a full
# rebuild (correct numbers), whereas a false "clean" would retain a wrong total.
FLOAT_MAX_ULPS = 16

# Sentinel so a legitimately-empty/``None`` measure tuple is never confused with
# an absent key in the ``dict.get`` lookup below.
_MISSING: Any = object()


@dataclass(frozen=True)
class SealedBucketDrift:
    """The count of sealed buckets that diverged, by kind.

    ``missing``    — present in the OLD (incrementally-carried) sealed set,
                     absent from the NEW (fresh full rebuild) set. History was
                     deleted or re-pointed out of a bucket.
    ``additional`` — present in the NEW set, absent from the OLD set. A row
                     arrived below the sealed watermark (a late insert beyond the
                     lookback) or was re-pointed into a bucket.
    ``changed``    — present in both, but the measure tuple differs beyond the
                     numeric tolerance. An in-place restatement of a measure, or
                     a joined-dimension change that altered an aggregated value.
    ``compared``   — how many distinct sealed grain keys were examined across
                     both sides. Zero means there was nothing sealed to compare
                     (a young aggregate, or an empty history) — not a clean bill.
    """

    missing: int
    additional: int
    changed: int
    compared: int

    @property
    def has_drift(self) -> bool:
        """True when any sealed bucket diverged — the append-only contract broke."""
        return (self.missing + self.additional + self.changed) > 0

    @property
    def total_diverged(self) -> int:
        return self.missing + self.additional + self.changed

    def summary(self) -> str:
        """A one-line human-readable description for logs and the ModelAlert."""
        return (
            f"{self.total_diverged} of {self.compared} sealed bucket(s) diverged "
            f"from a full rebuild (missing={self.missing}, "
            f"additional={self.additional}, changed={self.changed})"
        )


def _grain_key(row: Mapping[str, Any], grain_cols: Sequence[str]) -> tuple:
    """A hashable identity for a bucket from its grain columns.

    Grain values (dates, timestamps, ints, strings, NULL) are all hashable, so
    the tuple keys a dict directly. A NULL grain value is preserved as ``None``
    and matches another ``None`` — two buckets with the same grain including a
    NULL attribute are the same bucket.
    """
    return tuple(row.get(col) for col in grain_cols)


def _bucket_values(
    row: Mapping[str, Any], value_cols: Sequence[str]
) -> tuple:
    return tuple(row.get(col) for col in value_cols)


def _floats_close(a: float, b: float, max_ulps: int) -> bool:
    """Two native floats are equal within a small magnitude-scaled ULP bound."""
    if a == b:
        return True
    if math.isnan(a) or math.isnan(b) or math.isinf(a) or math.isinf(b):
        # NaN != NaN and inf comparisons: only bit-identical values are equal,
        # which the ``a == b`` fast path above already decided.
        return False
    # ``math.ulp(scale)`` is the size of one least-significant bit at ``scale``;
    # the ``1.0`` floor keeps the bound meaningful for values that cancel to
    # near zero without widening it for large magnitudes.
    scale = max(abs(a), abs(b), 1.0)
    return abs(a - b) <= max_ulps * math.ulp(scale)


def _values_equal(a: Any, b: Any, float_max_ulps: int) -> bool:
    """Element equality: exact for DB numerics/labels, ULP-bounded for floats.

    NULL semantics: a NULL on exactly one side is a change; two NULLs are equal.
    """
    if a is None or b is None:
        return a is None and b is None
    # ``bool`` is an ``int`` subclass; normalise so True/1 and False/0 compare by
    # value rather than by type.
    if isinstance(a, bool):
        a = int(a)
    if isinstance(b, bool):
        b = int(b)
    a_float = isinstance(a, float)
    b_float = isinstance(b, float)
    if a_float or b_float:
        # Native float on either side: the only place last-bit reorder noise can
        # arise. Compare with the small ULP bound. A non-numeric partner (should
        # not happen for a value column) falls back to exact equality.
        try:
            return _floats_close(float(a), float(b), float_max_ulps)
        except (TypeError, ValueError):
            return a == b
    if isinstance(a, (int, Decimal)) and isinstance(b, (int, Decimal)):
        # EXACT: NUMERIC/INTEGER compared without lossy binary-float coercion.
        try:
            return Decimal(a) == Decimal(b)
        except (InvalidOperation, TypeError, ValueError):
            return a == b
    # Non-numeric bucket values (passenger labels, dates, text) compare exactly.
    return a == b


def _values_all_equal(old: tuple, new: tuple, float_max_ulps: int) -> bool:
    """Element-wise value equality across a bucket's compared columns."""
    if len(old) != len(new):
        return False
    return all(_values_equal(a, b, float_max_ulps) for a, b in zip(old, new))


def compare_sealed_buckets(
    old_rows: Iterable[Mapping[str, Any]],
    new_rows: Iterable[Mapping[str, Any]],
    *,
    grain_cols: Sequence[str],
    value_cols: Sequence[str],
    float_max_ulps: int = FLOAT_MAX_ULPS,
) -> SealedBucketDrift:
    """Compare two sealed-bucket sets and count the divergences.

    Parameters
    ----------
    old_rows / new_rows:
        Sealed buckets as row dicts keyed by physical column name — the shape
        ``shared.source_executor.execute_source_sql`` returns. ``old_rows`` is
        read from the LIVE aggregate table and ``new_rows`` from the fresh
        full-rebuild replacement; both are already filtered to ``partition <
        window`` by the caller.
    grain_cols:
        Physical grain column names that identify a bucket. Must be non-empty; an
        aggregate always groups by at least its partition grain.
    value_cols:
        EVERY non-grain output column the aggregate materialises — measure/stat
        columns AND passenger/detail columns (a joined dimension's label that the
        router serves for a sealed key, B8768-R1-04). Their values are compared
        per bucket. May be empty (a grain-only aggregate), then only
        missing/additional matter.

    Returns
    -------
    :class:`SealedBucketDrift` — the divergence counts. ``has_drift`` is the
    single decision the scheduler acts on.
    """
    if not grain_cols:
        raise ValueError(
            "compare_sealed_buckets requires at least one grain column to key "
            "buckets by; an aggregate with no grain cannot be reconciled"
        )

    old_map: dict[tuple, tuple] = {}
    for row in old_rows:
        old_map[_grain_key(row, grain_cols)] = _bucket_values(row, value_cols)
    new_map: dict[tuple, tuple] = {}
    for row in new_rows:
        new_map[_grain_key(row, grain_cols)] = _bucket_values(row, value_cols)

    missing = 0
    changed = 0
    for key, old_vals in old_map.items():
        new_vals = new_map.get(key, _MISSING)
        if new_vals is _MISSING:
            missing += 1
        elif not _values_all_equal(old_vals, new_vals, float_max_ulps):
            changed += 1
    additional = sum(1 for key in new_map if key not in old_map)

    compared = len(set(old_map) | set(new_map))
    return SealedBucketDrift(
        missing=missing,
        additional=additional,
        changed=changed,
        compared=compared,
    )
