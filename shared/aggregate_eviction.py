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
  * ``lru``                — least-*used* dies first regardless of origin.
                              Ranked by real query service (``hit_count``,
                              credited only when an aggregate actually serves
                              a query — F-004-07), NOT by refresh recency.
                              ``last_refreshed_at`` is only a tiebreaker among
                              aggregates with equal usage. (Bug-7062: ranking
                              by ``last_refreshed_at`` alone evicted hot
                              aggregates that were merely materialised long
                              ago while sparing idle ones refreshed recently.)
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
        # Bug-7062: real query-service count. Credited by the router only when
        # an aggregate actually serves a query (F-004-07), so it is the true
        # usage signal for LRU — unlike last_refreshed_at, which tracks
        # materialisation recency and is unrelated to whether anyone queries.
        hit_count = getattr(agg, "hit_count", 0) or 0

        if policy == "lru":
            # Least-USED dies first: ascending hit_count is the primary key so
            # a cold, never-served aggregate (hit_count=0) evicts before a hot
            # one, regardless of when either was last refreshed. last_seen only
            # breaks ties between aggregates with identical usage (older refresh
            # loses); predictive origin is the final tiebreaker.
            return (hit_count, last_seen, 0 if is_predictive else 1)
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


def cap_countable_version_clause(
    *,
    version_column: Any,
    epoch_column: Any,
    deployed_version_id: Any,
    deploy_epoch: Any,
):
    """SQL arm restricting cap accounting to version-SERVABLE active aggregates.

    F-102-18 (Bug-9411): after a deploy/revert, an active aggregate whose
    ``built_for_version_id`` / ``built_for_epoch`` no longer match the model's
    deployed pointer is version-INCOMPATIBLE — the matcher's shared version gate
    (:mod:`shared.artifact_version_gate`) refuses to serve it. Yet it still counts
    as ``status == "active"``, so it occupied a ``max_aggregates`` slot and
    STARVED demand builds (11 dead rows of a 50 cap could not serve, and blocked
    new builds that could). Both cap-enforcement sites — the optimizer's on-demand
    ``enforce_cap`` and the scheduler's ``retirement_sweep`` — must count only the
    servable actives, and this ONE clause is the single source of truth so they
    cannot drift.

    On an UNDEPLOYED model (``deployed_version_id`` is None) every active is in
    its normal pre-deploy state and the historical all-active count is preserved
    (returns a tautology), so this never changes undeployed cap behaviour — it
    only frees the dead post-deploy slots. Uses ``not_(artifact_incompatible_sql)``
    so the two encodings of "compatible" stay identical to the runtime gate.
    """
    from sqlalchemy import not_, true

    from shared.artifact_version_gate import artifact_incompatible_sql

    if deployed_version_id is None:
        return true()
    return not_(
        artifact_incompatible_sql(
            version_column,
            epoch_column,
            deployed_version_id,
            deploy_epoch,
        )
    )
