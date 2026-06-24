"""Shared aggregate eviction-policy logic.

Single source of truth for HOW a cap-enforcement sweep chooses which
active aggregate to retire first. Used by BOTH retirement entry points so
they can never drift apart again (F-009-07):

  - optimizer ``src/lifecycle/retirement.py``  — on-demand ``enforce_cap``
    called inside the create path;
  - scheduler ``src/jobs/retirement_sweep.py`` — the daily batch sweep.

The four policies mirror ``models.predictive_eviction_policy``:

  * ``predicted_first``    — unvalidated predictive aggregates die first.
  * ``validated_survives`` — predictive dies first unless the F10 feedback
                              sweep stamped ``predictive_validated_at``.
  * ``lru``                — least-recently-used dies first regardless of
                              origin (``last_refreshed_at`` falls back to
                              ``created_at``).
  * ``never_evict``        — cap enforcement is disabled; no aggregate is
                              retired (callers must short-circuit on this).

``eviction_sort_key`` returns a callable usable directly as ``list.sort``'s
``key=`` argument; the lowest tuple is evicted first.
"""
from __future__ import annotations

from typing import Any, Callable

EVICTION_POLICIES: tuple[str, ...] = (
    "predicted_first",
    "lru",
    "validated_survives",
    "never_evict",
)

DEFAULT_EVICTION_POLICY = "predicted_first"


def eviction_sort_key(policy: str) -> Callable[[Any], tuple]:
    """Return a sort-key callable for the given eviction policy.

    The first tuple element is the kill-priority; the lowest tuple wins
    eviction. ``never_evict`` returns a constant-priority key so a caller
    that forgets to short-circuit still does no harm beyond an arbitrary
    stable order — but callers MUST short-circuit on ``never_evict`` to
    avoid retiring anything at all.
    """

    def key(agg: Any) -> tuple:
        is_predictive = agg.creation_reason == "predictive"
        # Recency proxy — last_refreshed_at falls back to created_at.
        last_seen = agg.last_refreshed_at or agg.created_at
        hit_rate = agg.estimated_hit_rate or 0.0

        if policy == "lru":
            return (last_seen, hit_rate, 0 if is_predictive else 1)
        if policy == "validated_survives":
            validated = is_predictive and (
                getattr(agg, "predictive_validated_at", None) is not None
            )
            return (
                0 if (is_predictive and not validated) else 1,
                hit_rate,
                last_seen,
            )
        # predicted_first (default) and never_evict both fall through here.
        # never_evict callers short-circuit before sorting.
        return (
            0 if is_predictive else 1,
            hit_rate,
            last_seen,
        )

    return key
