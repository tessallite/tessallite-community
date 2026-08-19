"""The single, defensible hit-rate formula (F-030-05 / Bug-9134).

Model Health (``api/metrics.py``) and Usage Analytics (``api/analytics.py``)
were computing two DIFFERENT acceleration numbers for the same model:

- Model Health divided by an ELIGIBLE denominator that excluded structurally
  unacceleratable ``raw`` detail-row queries (Bug-8180).
- Usage Analytics divided by ALL success rows, including ``raw`` — so the two
  tabs showed two different percentages and the product steering metric could
  not be defended.

This module is the one place that defines eligibility and the rate, so both
read aggregations agree by construction.

The contract (``architecture_cross-cutting-contracts.md`` §7): the hit rate is
``(aggregate + pocket serves) / eligible governed queries``. A result-cache
re-serve is NOT a new acceleration event (excluded at the query layer, keyed on
``cache_status='cache_hit'``); a ``raw`` force-route detail query cannot be
served by a pre-aggregate (there is nothing to aggregate) and is excluded from
the denominator so a rise in detail traffic is not misread as declining
coverage.
"""
from __future__ import annotations

# ``route_type='raw'`` is emitted only when a caller set ``force_route='raw'``
# for an ungrouped detail-row result. A pre-aggregated aggregate/pocket table
# cannot serve that shape, so it is not an acceleratable "miss" and must be
# excluded from the hit-rate denominator (F-102-06 / Bug-8180).
UNACCELERATABLE_ROUTE_TYPES: tuple[str, ...] = ("raw",)


def eligible_hit_rate(
    *,
    aggregate_hits: int,
    pocket_hits: int,
    total_queries: int,
    unacceleratable_queries: int,
) -> float:
    """Fraction (0..1) of ELIGIBLE governed queries served by an aggregate or pocket.

    ``total_queries`` counts every governed query (live + cache re-serves); the
    caller has already excluded cache re-serves from ``aggregate_hits`` /
    ``pocket_hits``. ``unacceleratable_queries`` is the ``raw`` subset removed
    from the denominator. Returns ``0.0`` when the eligible denominator is empty.
    """
    eligible = total_queries - unacceleratable_queries
    if eligible <= 0:
        return 0.0
    return (aggregate_hits + pocket_hits) / eligible
