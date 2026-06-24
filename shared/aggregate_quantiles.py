"""Canonical quantile (percentile) configuration for aggregates.

Single source of truth (D0 decision) for:
- which percentiles the aggregate builder materialises,
- the physical column-name suffix per percentile,
- the fraction -> suffix mapping the query-router uses to route
  MEDIAN / PERCENTILE_CONT / PERCENTILE_DISC queries,
- which dialects produce EXACT vs APPROXIMATE quantile columns.

Used by the optimizer + scheduler DDL generators, the query-router, and the
frontend label (via the API) so all stay consistent.
"""
from __future__ import annotations

# Confirmed canonical set.
QUANTILE_PERCENTILES: list[int] = [1, 5, 10, 25, 50, 75, 90, 95, 99]


def quantile_suffix(pct: int) -> str:
    """Column-name stat suffix for a percentile: 5 -> 'p05', 50 -> 'p50'."""
    return f"p{pct:02d}"


# Canonical stat-type suffixes in order: ['p01','p05',...,'p99'].
QUANTILE_STAT_TYPES: list[str] = [quantile_suffix(p) for p in QUANTILE_PERCENTILES]
_SUFFIX_TO_PCT: dict[str, int] = {quantile_suffix(p): p for p in QUANTILE_PERCENTILES}


def is_quantile_stat_type(stat_type: str | None) -> bool:
    return (stat_type or "") in _SUFFIX_TO_PCT


def fraction_to_quantile_suffix(fraction: float) -> str | None:
    """Map a PERCENTILE_CONT/DISC fraction (0..1) to a materialised column
    suffix, or None when the fraction is not in the canonical set.
    MEDIAN is fraction 0.5 -> 'p50'.
    """
    scaled = fraction * 100.0
    pct = round(scaled)
    if abs(scaled - pct) > 1e-9:
        return None
    return quantile_suffix(pct) if pct in _SUFFIX_TO_PCT.values() else None


def quantile_suffix_to_fraction(suffix: str | None) -> float | None:
    """Reverse of ``fraction_to_quantile_suffix``: 'p50' -> 0.5, 'p01' -> 0.01.

    Returns None when ``suffix`` is not a canonical quantile stat type. Used by
    the source-route SQL renderer to turn the internal pNN marker back into a
    native ``PERCENTILE_CONT(fraction)`` call when a percentile query cannot be
    served from a materialised aggregate column.
    """
    pct = _SUFFIX_TO_PCT.get(suffix or "")
    return None if pct is None else pct / 100.0


# Dialects whose quantile columns are EXACT and therefore routable for
# exact-semantics percentile queries.
#
#   - PostgreSQL/Redshift: exact via PERCENTILE_CONT (same- and cross-engine).
#   - Spark: exact via PERCENTILE() for a SAME-ENGINE refresh (Spark source →
#     Spark target). For a CROSS-ENGINE refresh (Spark source → non-Spark
#     target) the SELECT is transpiled and sqlglot rewrites PERCENTILE_CONT to
#     the APPROXIMATE PERCENTILE_APPROX, so the cross-db builders deliberately
#     SKIP Spark quantile materialisation there (no column → routed to source).
#     Spark cross-engine quantiles are therefore unsupported/approximate.
#   - BigQuery: only APPROX_QUANTILES exists; its quantile columns are
#     approximate and gated out of routing (Q9).
#   - F-004-09: Redshift was previously omitted from this set even though the
#     module's own contract (above) and the architecture docs declare its
#     PERCENTILE_CONT exact. The omission silently disabled percentile/MEDIAN
#     routing for every Redshift-source model and made the cross-db builders
#     skip Redshift quantile materialisation. Redshift is a postgres-family
#     engine with exact PERCENTILE_CONT same- and cross-engine, so it belongs
#     here alongside postgres; ``quantile_materialization_is_exact`` already
#     treats any non-Spark exact source as exact for every target.
_EXACT_QUANTILE_DIALECTS = frozenset({"postgresql", "postgres", "redshift", "spark"})


def quantiles_are_exact(dialect: str | None) -> bool:
    return (dialect or "").lower() in _EXACT_QUANTILE_DIALECTS


def quantile_materialization_is_exact(
    source_dialect: str | None,
    target_dialect: str | None,
) -> bool:
    """Whether an aggregate's quantile (pNN) columns are EXACT and routable,
    given BOTH the source dialect the aggregation ran in and the target dialect
    the columns were materialised into.

    ``quantiles_are_exact`` looks at the source alone, which is wrong for the
    cross-engine case: Spark produces exact ``PERCENTILE()`` only for a
    SAME-ENGINE refresh (Spark source -> Spark target). For a cross-engine
    refresh the SELECT is transpiled and sqlglot rewrites the exact percentile
    to the APPROXIMATE form, so the cross-db DDL builders deliberately SKIP
    Spark quantile columns — no exact pNN column is materialised, so an exact
    percentile query must NOT route to it. PostgreSQL/Redshift PERCENTILE_CONT
    stays exact same- or cross-engine; BigQuery is always approximate.

    This is the single capability predicate the DDL builders, metadata writers,
    and the query-router percentile gate should share so the three never drift
    (Bug-1007).
    """
    src = (source_dialect or "").lower()
    tgt = (target_dialect or "").lower()
    if not quantiles_are_exact(src):
        return False
    if src == "spark":
        # Spark exactness survives only when materialised back into Spark.
        return tgt == "spark"
    # postgres-family source: PERCENTILE_CONT is exact regardless of target.
    return True
