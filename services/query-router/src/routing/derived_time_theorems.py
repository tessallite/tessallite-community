"""Contiguous-time coarsening theorems with boundary-alignment guards.

Spec: architecture_derived-grain-aggregate-routing.md §7.5 and pitfall 23. A
time-coarsening theorem ``coarse = coarse(finer_key)`` lets the planner derive a
coarser DATE_TRUNC bucket from a finer stored DATE_TRUNC key WITHOUT re-scanning
the source — BUT only when every boundary of the coarse bucket set is also a
boundary of the finer bucket set (the coarse partition is a union of whole finer
buckets). This module encodes the FEW admissible v1 theorems as DATA and enforces
alignment structurally, so a boundary-misaligned theorem (week from month, month
from week) is impossible to admit.

Admissible v1 (identical calendar/timezone semantics, spec §7.5):

  DATE_TRUNC('year',  ts) = DATE_TRUNC('year',  DATE_TRUNC('month', ts))
  DATE_TRUNC('quarter', ts) = DATE_TRUNC('quarter', DATE_TRUNC('month', ts))
  DATE_TRUNC('year',  ts) = DATE_TRUNC('year',  DATE_TRUNC('quarter', ts))
  DATE_TRUNC('month'/'quarter'/'year', ts) from a 'day' key (day boundaries
    subdivide month/quarter/year boundaries).
  DATE_TRUNC('week', ts) from a 'day' key ONLY when the week-start convention is
    pinned in the semantic profile of BOTH the theorem and the artifact key.

Explicitly REFUSED (no theorem — SOURCE_ONLY):
  - week -> month/quarter/year and month/quarter/year -> week (week does not align
    with month boundaries: 2025-01-29 and 2025-02-02 share ISO week 2025-01-27).
  - EXTRACT(month/year/...) as a coarsening target (the prior year-collapse class):
    EXTRACT fields are served only by EXACT materialised identity, never derived.
  - month-of-year, formatted month name, or an ambiguous 'month' key -> calendar
    month.

PURE + data-driven: no SQL, no DB, no connector branch. The proof engine calls
``coarsen_edge`` to ask whether a finer stored key can derive a coarse query unit.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# Finer -> coarser DATE_TRUNC units that compose because their boundaries align.
# Value is the set of coarse units derivable from that finer key. Week is handled
# separately (needs a pinned week-start) and NEVER produces month/quarter/year.
_ALIGNED_COARSENINGS: dict[str, frozenset[str]] = {
    # Day boundaries subdivide week AND month/quarter/year boundaries, so a day key
    # can produce any of them (week only under a pinned week-start, enforced below).
    "day": frozenset({"day", "week", "month", "quarter", "year"}),
    "month": frozenset({"month", "quarter", "year"}),
    "quarter": frozenset({"quarter", "year"}),
    "year": frozenset({"year"}),
    # 'week' can coarsen only to 'week' (identity) — it aligns with nothing else.
    "week": frozenset({"week"}),
}

# Units whose boundaries do NOT subdivide into month/quarter/year — a week key
# can never produce a calendar month/quarter/year and vice versa.
_WEEK_UNIT = "week"
_CALENDAR_ALIGNED = frozenset({"month", "quarter", "year"})


@dataclass
class CoarsenEdge:
    admissible: bool
    finer_unit: str
    coarse_unit: str
    # A stable reason when refused, for the proof reason codes.
    reason: Optional[str] = None
    # True when the edge required (and had) a pinned week-start profile.
    week_start_pinned: bool = False


def coarsen_edge(
    *,
    finer_unit: str,
    coarse_unit: str,
    week_start_pinned: bool = False,
) -> CoarsenEdge:
    """Return whether ``coarse_unit`` is derivable from a stored ``finer_unit`` key.

    ``week_start_pinned`` MUST be True (both theorem + artifact key pin the same
    week-start convention) for any edge INTO or FROM 'week'; an unpinned week edge
    is SOURCE_ONLY (spec §7.5). Refusals carry a stable reason code.
    """
    finer = (finer_unit or "").strip().lower()
    coarse = (coarse_unit or "").strip().lower()

    # Week never aligns with calendar month/quarter/year in EITHER direction
    # (pitfall 23). Refuse before consulting the alignment table.
    if (finer == _WEEK_UNIT and coarse in _CALENDAR_ALIGNED) or (
        coarse == _WEEK_UNIT and finer in _CALENDAR_ALIGNED
    ):
        return CoarsenEdge(
            admissible=False, finer_unit=finer, coarse_unit=coarse,
            reason="DERIVED_EXACT_KEY_NOT_FOUND",
        )

    # Any edge touching 'week' requires a pinned week-start on both sides.
    if (finer == _WEEK_UNIT or coarse == _WEEK_UNIT) and not week_start_pinned:
        return CoarsenEdge(
            admissible=False, finer_unit=finer, coarse_unit=coarse,
            reason="DERIVED_TIMEZONE_UNPINNED",
        )

    derivable = _ALIGNED_COARSENINGS.get(finer)
    if derivable is None or coarse not in derivable:
        return CoarsenEdge(
            admissible=False, finer_unit=finer, coarse_unit=coarse,
            reason="DERIVED_EXACT_KEY_NOT_FOUND",
        )

    return CoarsenEdge(
        admissible=True, finer_unit=finer, coarse_unit=coarse,
        week_start_pinned=(finer == _WEEK_UNIT or coarse == _WEEK_UNIT),
    )


def extract_is_coarsenable(_field: str) -> bool:
    """EXTRACT(<field> FROM ts) is NEVER an admissible coarsening target in v1.

    Spec §7.5: EXTRACT fields (month-of-year, year, etc.) may be served only by
    EXACT materialised identity, never derived from a finer key. Returning False
    unconditionally makes the prior year-collapse class structurally unreachable
    (an EXTRACT('year', ...) that would wrongly collapse a month key to a bare
    year integer can never be proven as a coarsening).
    """
    return False
