"""Measure roll-up recipe classifier for derived-grain serving proofs.

Spec: architecture_derived-grain-aggregate-routing.md §7.7 (measure proof) and
invariant I4 (re-aggregation is statistic-specific). Given the requested statistic
for one measure and the proof's key verdict (EXACT vs ROLLUP), decide the
``MeasureRollupPlan``:

  Requested        | Stored components   | Plan
  -----------------|---------------------|-------------------
  SUM(x)           | x__sum              | SUM_OF_SUM
  COUNT(x)         | x__count            | SUM_OF_COUNT
  COUNT(*)         | __row_count__count  | SUM_OF_COUNT
  MIN(x)           | x__min              | MIN_OF_MIN
  MAX(x)           | x__max              | MAX_OF_MAX
  AVG(x)           | x__sum AND x__count | AVG_FROM_SUM_COUNT
  quantile/pNN     | (direct only)       | DIRECT at EXACT, else SOURCE_ONLY
  stddev/variance  | (direct only)       | DIRECT at EXACT, else SOURCE_ONLY
  count_distinct   | (direct only)       | DIRECT at EXACT, else SOURCE_ONLY
  unknown          | -                   | SOURCE_ONLY

Non-negotiable (pitfall 9): the classifier NEVER defaults an unknown statistic to
SUM. ``SOURCE_ONLY`` is the default recipe. A DIRECT-only statistic (quantile,
dispersion, count-distinct, semi-additive, window) is servable ONLY when the key
verdict is EXACT (exact key identity or a verified bijection relabel that preserves
the complete tuple partition — §7.7); at ROLLUP it is ``SOURCE_ONLY``.

This module is PURE and does not execute SQL, touch the DB, or mutate any route.
It reuses the shipped stat vocabulary (``aggregate_stats``/``aggregate_quantiles``)
so the derived proof and the ordinary matcher agree on what is re-aggregatable.
Bug-6969 remains the authority for whether an exact quantile direct read is
enabled; this classifier does not weaken or replace that gate — it only reports
DIRECT-at-EXACT, and the exact-statistic gate still governs the actual read.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from shared.aggregate_quantiles import is_quantile_agg_token, is_quantile_stat_type
from shared.aggregate_stats import is_stat_type


# Measure roll-up plan vocabulary (spec §5.4 MeasureRollupPlan).
DIRECT = "DIRECT"
SUM_OF_SUM = "SUM_OF_SUM"
SUM_OF_COUNT = "SUM_OF_COUNT"
MIN_OF_MIN = "MIN_OF_MIN"
MAX_OF_MAX = "MAX_OF_MAX"
AVG_FROM_SUM_COUNT = "AVG_FROM_SUM_COUNT"
SOURCE_ONLY = "SOURCE_ONLY"

# Key verdicts this classifier cares about (subset of DerivedServeProof.verdict).
VERDICT_EXACT = "EXACT"
VERDICT_ROLLUP = "ROLLUP"

# Re-aggregatable additive statistic tokens -> their roll-up plan. These are the
# ONLY statistics safe to combine across coarser groups (I4).
_ADDITIVE_PLAN: dict[str, str] = {
    "sum": SUM_OF_SUM,
    "count": SUM_OF_COUNT,
    "count_star": SUM_OF_COUNT,
    "row_count": SUM_OF_COUNT,
    "min": MIN_OF_MIN,
    "max": MAX_OF_MAX,
    "avg": AVG_FROM_SUM_COUNT,
    "mean": AVG_FROM_SUM_COUNT,
}


@dataclass
class MeasureRollupPlan:
    """The chosen roll-up recipe for one requested measure statistic."""
    measure_name: str
    requested_stat: str
    plan: str
    # Physical stored component columns the plan needs (empty for SOURCE_ONLY).
    required_components: tuple[str, ...] = ()
    # Stable reason when the plan is SOURCE_ONLY, for the proof reason codes.
    reason: Optional[str] = None


def _is_direct_only_statistic(stat: str) -> bool:
    """A statistic that is NOT re-aggregatable and may serve only at EXACT key.

    Quantiles/percentiles (incl. median), dispersion (stddev/variance), and
    COUNT(DISTINCT) are the v1 direct-only family (§7.7). Semi-additive and window
    variants are handled by their own gates upstream and reach here as unknown ->
    SOURCE_ONLY, which is the safe default.
    """
    s = (stat or "").strip().lower()
    if is_quantile_agg_token(s) or is_quantile_stat_type(s):
        return True
    if is_stat_type(s):
        return True
    if s in ("count_distinct", "distinct_count", "countdistinct", "distinct"):
        return True
    return False


def classify_measure_rollup(
    *,
    measure_name: str,
    requested_stat: str,
    key_verdict: str,
    available_components: frozenset[str] = frozenset(),
) -> MeasureRollupPlan:
    """Return the MeasureRollupPlan for one requested measure statistic (§7.7, I4).

    ``key_verdict`` is the proof's key verdict (EXACT or ROLLUP). ``available_components``
    is the set of physical component column suffixes the candidate artifact stores
    for this measure (e.g. {"x__sum", "x__count"}); when provided, an additive plan
    is downgraded to SOURCE_ONLY if a required component is absent (pitfall 9: never
    fabricate a component).

    Rules:
      - A direct-only statistic (quantile/dispersion/distinct) -> DIRECT at EXACT,
        else SOURCE_ONLY (reason DERIVED_MEASURE_NOT_ROLLUP_SAFE).
      - An additive statistic -> its roll-up plan; SUM_OF_SUM/COUNT/MIN/MAX at both
        EXACT and ROLLUP, AVG_FROM_SUM_COUNT needs both sum + count components.
      - Anything unknown -> SOURCE_ONLY (never SUM by default).
    """
    stat = (requested_stat or "").strip().lower()

    if _is_direct_only_statistic(stat):
        if key_verdict == VERDICT_EXACT:
            # The stored value IS the exact answer under an exact-key / bijection
            # relabel; the actual read still passes its own exact-statistic gate
            # (Bug-6969 for quantiles). We only report DIRECT here.
            return MeasureRollupPlan(
                measure_name=measure_name, requested_stat=stat, plan=DIRECT,
            )
        return MeasureRollupPlan(
            measure_name=measure_name, requested_stat=stat, plan=SOURCE_ONLY,
            reason="DERIVED_MEASURE_NOT_ROLLUP_SAFE",
        )

    plan = _ADDITIVE_PLAN.get(stat)
    if plan is None:
        # Unknown / unsupported statistic — SOURCE_ONLY is the default recipe.
        return MeasureRollupPlan(
            measure_name=measure_name, requested_stat=stat, plan=SOURCE_ONLY,
            reason="DERIVED_MEASURE_NOT_ROLLUP_SAFE",
        )

    # Component-availability gate (only enforced when caller supplies the set).
    if available_components:
        needed: tuple[str, ...]
        if plan == AVG_FROM_SUM_COUNT:
            needed = (f"{measure_name}__sum", f"{measure_name}__count")
        elif plan == SUM_OF_SUM:
            needed = (f"{measure_name}__sum",)
        elif plan == SUM_OF_COUNT:
            needed = (f"{measure_name}__count",)
        elif plan == MIN_OF_MIN:
            needed = (f"{measure_name}__min",)
        elif plan == MAX_OF_MAX:
            needed = (f"{measure_name}__max",)
        else:
            needed = ()
        if any(c not in available_components for c in needed):
            return MeasureRollupPlan(
                measure_name=measure_name, requested_stat=stat, plan=SOURCE_ONLY,
                reason="DERIVED_MEASURE_NOT_ROLLUP_SAFE",
            )
        return MeasureRollupPlan(
            measure_name=measure_name, requested_stat=stat, plan=plan,
            required_components=needed,
        )

    return MeasureRollupPlan(
        measure_name=measure_name, requested_stat=stat, plan=plan,
    )


def all_measures_servable(plans: list[MeasureRollupPlan]) -> bool:
    """True only when EVERY requested measure has a non-SOURCE_ONLY plan (§7.8:
    one unproved measure rejects the whole candidate)."""
    return bool(plans) and all(p.plan != SOURCE_ONLY for p in plans)
