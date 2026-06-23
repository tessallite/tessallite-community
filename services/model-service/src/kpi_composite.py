"""Composite KPI scoring engine.

Implements Section 9.1-9.3 of the KPI requirements specification.

Composite KPIs aggregate multiple child KPIs into a single weighted score.
Each child is normalised to 0-100, weighted, and summed. Two normalisation
methods are supported:

- ``pct_target``: Percentage of target (default).
- ``min_max``: Min-max scaling with configurable or historical bounds.

NULL children are excluded from the weighted sum and remaining weights are
re-normalised. If all children are NULL, the composite evaluates to NULL.

Error vs no-data distinction (Bug-4255, §9.1)
---------------------------------------------
A child can end up with a NULL value for two very different reasons:

- **No data:** the child evaluated successfully but legitimately has no
  value for the current slice (empty result, null target, etc.). This is a
  normal, expected condition. The child is silently excluded and the
  remaining children's weights are re-normalised — the parent still renders
  a healthy score. Behaviour unchanged.
- **Error:** the child's evaluation FAILED (raised, the SQL errored, a
  cycle/depth guard tripped, a time dimension was missing, ...). The child
  is still excluded from the weighted sum, but the parent now carries a
  DISTINCT, visible signal so a broken input is never silently dropped.

The caller distinguishes the two cases at the point of failure and passes a
non-None ``error_reason`` on the ``ChildScore`` only for genuine errors. A
no-data child carries ``error_reason=None``.

The composite result therefore exposes:

- ``status``: ``"ok"`` (no errored children), ``"degraded"`` (score computed
  from the valid children but at least one child errored), or ``"error"``
  (every child errored — no valid child to score, so the score is NULL
  rather than a fake number).
- ``errored_children``: per-child ``(kpi_id, kpi_name, error_reason)`` for
  every child whose evaluation failed, so a consumer (scorecard / API / UI)
  can show which input is broken and why.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ChildScore:
    """A single child KPI's contribution to a composite score."""
    kpi_id: str
    kpi_name: str
    raw_value: Optional[float]
    target: Optional[float]
    weight: float
    direction: str = "higher_is_better"
    normalised: Optional[float] = None
    excluded: bool = False
    exclude_reason: Optional[str] = None
    # Bug-4255: set ONLY when the child's evaluation genuinely failed
    # (raised / SQL error / cycle / depth / missing time dimension). A
    # no-data child leaves this None. Distinguishes an errored exclusion
    # (surfaced as ``degraded`` on the parent) from a silent no-data
    # exclusion (unchanged behaviour).
    error_reason: Optional[str] = None
    # F-017-09: per-child min_max bounds derived from the child's trailing
    # snapshots when the composite has no explicit normalisation_min/max.
    # Spec 9.1 mandates a historical-bounds fallback so a min_max composite
    # without configured bounds still scores instead of silently going NULL.
    bound_min: Optional[float] = None
    bound_max: Optional[float] = None


# Composite health status values (Bug-4255).
COMPOSITE_STATUS_OK = "ok"
COMPOSITE_STATUS_DEGRADED = "degraded"
COMPOSITE_STATUS_ERROR = "error"


@dataclass
class ErroredChild:
    """A child KPI whose evaluation failed (Bug-4255).

    Carried on the parent's result so a consumer can show which input is
    broken and why, instead of the failure being silently absorbed.
    """
    kpi_id: str
    kpi_name: str
    error_reason: str


@dataclass
class CompositeResult:
    """Result of a composite KPI evaluation."""
    composite_score: Optional[float]
    children: list[ChildScore] = field(default_factory=list)
    normalisation_method: str = "pct_target"
    total_weight_before: float = 0.0
    total_weight_after: float = 0.0
    # Bug-4255: health of the composite given its children's evaluation.
    # ``ok`` — no child errored; ``degraded`` — score computed but at least
    # one child errored; ``error`` — every child errored (score is None).
    status: str = COMPOSITE_STATUS_OK
    errored_children: list[ErroredChild] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Normalisation functions
# ---------------------------------------------------------------------------

def normalise_pct_target(
    value: Optional[float],
    target: Optional[float],
    direction: str = "higher_is_better",
) -> Optional[float]:
    """Normalise a value as percentage of target, capped at [0, 100].

    For ``higher_is_better``:  ``min(100, (value / target) * 100)``
    For ``lower_is_better``:   ``min(100, (target / value) * 100)``
    For ``closer_is_better``:  ``max(0, (1 - |value - target| / |target|) * 100)``

    Returns None if value or target is None, or if a division-by-zero
    would occur.
    """
    if value is None or target is None or target == 0:
        return None

    if direction == "lower_is_better":
        if value == 0:
            return 100.0
        return max(0.0, min(100.0, (target / value) * 100.0))

    if direction == "closer_is_better":
        return max(0.0, min(100.0, (1.0 - abs(value - target) / abs(target)) * 100.0))

    return min(100.0, (value / target) * 100.0)


def normalise_min_max(
    value: Optional[float],
    direction: str,
    bound_min: Optional[float],
    bound_max: Optional[float],
) -> Optional[float]:
    """Normalise a value using min-max scaling.

    For ``higher_is_better``:
        ``((value - min) / (max - min)) * 100``

    For ``lower_is_better``:
        ``((max - value) / (max - min)) * 100``

    For ``closer_is_better``:
        ``(1 - |value - midpoint| / half_spread) * 100``

    Returns None if value or bounds are None, or if min == max.
    Result is clamped to [0, 100].
    """
    if value is None or bound_min is None or bound_max is None:
        return None
    spread = bound_max - bound_min
    if spread == 0:
        return None

    if direction == "lower_is_better":
        raw = ((bound_max - value) / spread) * 100.0
    elif direction == "closer_is_better":
        mid = (bound_min + bound_max) / 2.0
        half_spread = spread / 2.0
        raw = (1.0 - abs(value - mid) / half_spread) * 100.0
    else:
        raw = ((value - bound_min) / spread) * 100.0

    return max(0.0, min(100.0, raw))


# ---------------------------------------------------------------------------
# Weight normalisation
# ---------------------------------------------------------------------------

def normalise_weights(children: list[ChildScore]) -> list[ChildScore]:
    """Normalise weights to sum to 1.0 among non-excluded children.

    Children with ``excluded=True`` are skipped. Remaining weights are
    scaled proportionally so their sum equals 1.0.

    Mutates and returns the same list for convenience.
    """
    active = [c for c in children if not c.excluded]
    if not active:
        return children

    total = sum(c.weight for c in active)
    if total == 0:
        # All weights are zero; distribute equally.
        equal_weight = 1.0 / len(active)
        for c in active:
            c.weight = equal_weight
    else:
        for c in active:
            c.weight = c.weight / total

    return children


# ---------------------------------------------------------------------------
# Composite evaluation
# ---------------------------------------------------------------------------

def evaluate_composite(
    children: list[ChildScore],
    normalisation_method: str = "pct_target",
    bound_min: Optional[float] = None,
    bound_max: Optional[float] = None,
) -> CompositeResult:
    """Evaluate a composite KPI from its children.

    Parameters
    ----------
    children : list[ChildScore]
        Child KPI scores with raw values, targets, weights, and directions.
    normalisation_method : str
        ``"pct_target"`` (default) or ``"min_max"``.
    bound_min : float | None
        Min bound for min_max normalisation. Required when method is min_max.
    bound_max : float | None
        Max bound for min_max normalisation. Required when method is min_max.

    Returns
    -------
    CompositeResult
        The weighted composite score and per-child breakdown.
    """
    total_weight_before = sum(c.weight for c in children)

    # Step 1: Normalise each child
    for child in children:
        if child.error_reason is not None:
            # Bug-4255: the child's evaluation FAILED. Exclude it from the
            # weighted sum (same as a null child) but mark it distinctly so
            # the parent can surface a degraded/error signal instead of
            # silently dropping a broken input.
            child.excluded = True
            child.exclude_reason = "error"
            child.normalised = None
            continue
        if child.raw_value is None:
            child.excluded = True
            child.exclude_reason = "null_value"
            child.normalised = None
            continue

        if normalisation_method == "min_max":
            # F-017-09: prefer the composite's configured bounds; otherwise fall
            # back to the child's historical (snapshot-derived) bounds populated
            # by the caller. Only when neither is available is the child excluded.
            eff_min = bound_min if bound_min is not None else child.bound_min
            eff_max = bound_max if bound_max is not None else child.bound_max
            child.normalised = normalise_min_max(
                child.raw_value,
                child.direction,
                eff_min,
                eff_max,
            )
            if child.normalised is None:
                child.excluded = True
                child.exclude_reason = "normalisation_failed"
        else:
            # pct_target
            if child.target is None or child.target == 0:
                child.excluded = True
                child.exclude_reason = "null_target"
                child.normalised = None
            else:
                child.normalised = normalise_pct_target(
                    child.raw_value,
                    child.target,
                    child.direction,
                )

    # Bug-4255: collect children whose evaluation genuinely failed (not
    # no-data). These drive the parent's degraded/error signal.
    errored_children = [
        ErroredChild(
            kpi_id=c.kpi_id,
            kpi_name=c.kpi_name,
            error_reason=c.error_reason or "evaluation_failed",
        )
        for c in children
        if c.error_reason is not None
    ]

    # Step 2: Re-normalise weights over non-excluded children
    normalise_weights(children)

    # Step 3: Compute weighted sum
    active = [c for c in children if not c.excluded]
    total_weight_after = sum(c.weight for c in active)

    if not active:
        # No scoreable child. If every child ERRORED the composite goes to an
        # error state (no fake score); if children were merely no-data the
        # score is None with the unchanged "ok" status (silent exclusion).
        status = (
            COMPOSITE_STATUS_ERROR if errored_children else COMPOSITE_STATUS_OK
        )
        return CompositeResult(
            composite_score=None,
            children=children,
            normalisation_method=normalisation_method,
            total_weight_before=total_weight_before,
            total_weight_after=0.0,
            status=status,
            errored_children=errored_children,
        )

    composite_score = sum(
        (c.normalised or 0.0) * c.weight
        for c in active
    )

    # Some valid children scored, but if any other child errored the parent is
    # degraded — the score is real yet one input is broken.
    status = (
        COMPOSITE_STATUS_DEGRADED if errored_children else COMPOSITE_STATUS_OK
    )

    return CompositeResult(
        composite_score=composite_score,
        children=children,
        normalisation_method=normalisation_method,
        total_weight_before=total_weight_before,
        total_weight_after=total_weight_after,
        status=status,
        errored_children=errored_children,
    )


# ---------------------------------------------------------------------------
# Batch composite evaluation with dependency ordering
# ---------------------------------------------------------------------------

def build_composite_children(
    parent_kpi_id: str,
    all_kpis: list[dict],
    eval_cache: dict[str, dict],
) -> list[ChildScore]:
    """Build ChildScore list for a composite KPI from cached eval results.

    Parameters
    ----------
    parent_kpi_id : str
        The composite KPI's ID.
    all_kpis : list[dict]
        All KPIs in the model. Each dict must have ``id``, ``name``,
        ``parent_kpi_id``, ``weight``, ``direction``, and ``target_value``.
    eval_cache : dict[str, dict]
        Cached evaluation results keyed by KPI ID (as string). Each entry
        should have ``value`` and ``target`` keys, and may carry an
        ``error_reason`` (Bug-4255) when the child's evaluation failed — that
        reason flags the parent as degraded instead of silently excluding the
        child as no-data.

    Returns
    -------
    list[ChildScore]
        Child scores ready for ``evaluate_composite()``.
    """
    children: list[ChildScore] = []

    for kpi in all_kpis:
        if str(kpi.get("parent_kpi_id")) != str(parent_kpi_id):
            continue

        kpi_id = str(kpi["id"])
        cached = eval_cache.get(kpi_id, {})

        children.append(ChildScore(
            kpi_id=kpi_id,
            kpi_name=kpi.get("name", ""),
            raw_value=cached.get("value"),
            target=cached.get("target"),
            weight=kpi.get("weight") or 1.0,
            direction=kpi.get("direction", "higher_is_better"),
            error_reason=cached.get("error_reason"),
        ))

    return children


def get_normalisation_config(presentation_meta: Optional[dict]) -> tuple[str, Optional[float], Optional[float]]:
    """Extract normalisation config from presentation_meta.

    Returns
    -------
    tuple
        (normalisation_method, bound_min, bound_max)
    """
    if not presentation_meta:
        return "pct_target", None, None

    method = presentation_meta.get("normalisation_method", "pct_target")
    bound_min = presentation_meta.get("normalisation_min")
    bound_max = presentation_meta.get("normalisation_max")

    # Coerce to float if present
    if bound_min is not None:
        bound_min = float(bound_min)
    if bound_max is not None:
        bound_max = float(bound_max)

    return method, bound_min, bound_max
