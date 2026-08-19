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

# Confirmed canonical set. This is the FULL set the query-router's recognition
# and fraction<->suffix mapping understand (so existing materialised columns keep
# being recognised for refresh/skip logic and MEDIAN keeps mapping to p50). It is
# NOT the set new aggregates materialise — see ROUTABLE_QUANTILE_PERCENTILES.
QUANTILE_PERCENTILES: list[int] = [1, 5, 10, 25, 50, 75, 90, 95, 99]


def quantile_suffix(pct: int) -> str:
    """Column-name stat suffix for a percentile: 5 -> 'p05', 50 -> 'p50'."""
    return f"p{pct:02d}"


# Canonical stat-type suffixes in order: ['p01','p05',...,'p99'].
QUANTILE_STAT_TYPES: list[str] = [quantile_suffix(p) for p in QUANTILE_PERCENTILES]
_SUFFIX_TO_PCT: dict[str, int] = {quantile_suffix(p): p for p in QUANTILE_PERCENTILES}


# ---------------------------------------------------------------------------
# Bug-5891 (DEC-PERCENTILE, user decision Option B, 2026-07-07): the percentile
# columns that new aggregates actually MATERIALISE, narrowed to the ones SQL
# routing can reach today.
#
# Only MEDIAN (p50) is routable: the query-router maps MEDIAN(col) -> the p50
# column, but does NOT route PERCENTILE_CONT/DISC(other fraction) WITHIN GROUP
# to the matching pNN column. That routing fix needs parsing/sql_parser.py +
# semantic/binder.py changed together (it was attempted once and reverted — see
# docs/execution/execution_issue-registry.md Bug-5891 and F-003-07). Until that
# lands, materialising p01/p05/p10/p25/p75/p90/p95/p99 produces DEAD columns:
# storage + refresh cost with zero acceleration (every non-median percentile
# query falls back to source anyway).
#
# So NEW aggregates materialise only the routable subset below. Existing p90/p95
# columns are left untouched (no deletion / no routing change). This is a
# deliberately reversible block: when the sql_parser/binder routing fix lands,
# restore the full set with the single line
#     ROUTABLE_QUANTILE_PERCENTILES = list(QUANTILE_PERCENTILES)
# and drop this note.
#
# Backfill note for whoever re-enables it: aggregates created during this
# median-only window carry only the p50 coverage row, and scheduler refresh is
# coverage-driven, so they will NOT auto-gain p90/p95/... after the flip. To
# backfill them, toggle include_quantiles off then on (re-registers the full
# coverage set) or rebuild the aggregate, so the next refresh materialises the
# restored columns.
ROUTABLE_QUANTILE_PERCENTILES: list[int] = [50]
ROUTABLE_QUANTILE_STAT_TYPES: list[str] = [
    quantile_suffix(p) for p in ROUTABLE_QUANTILE_PERCENTILES
]


def is_routable_quantile_percentile(pct: int) -> bool:
    """Whether percentile ``pct`` is one new aggregates should materialise
    (i.e. one SQL routing can currently serve). See ROUTABLE_QUANTILE_PERCENTILES.
    """
    return pct in ROUTABLE_QUANTILE_PERCENTILES


def is_quantile_stat_type(stat_type: str | None) -> bool:
    return (stat_type or "") in _SUFFIX_TO_PCT


# Aggregate/function tokens that denote a scalar quantile but are NOT the
# canonical pNN suffix: the legacy ``median`` default_agg synonym (canonicalised
# to p50 at rehydration, but a live/legacy ORM row may still carry the raw
# token) and the sqlglot function keys emitted when a percentile appears in a
# HAVING clause (``median`` / ``percentilecont`` / ``percentiledisc``, with and
# without underscores). Kept here as the single source of truth so the router's
# exactness gate and the matcher's non-additive gate agree (Bug-7779/Bug-7782).
_QUANTILE_ALIAS_TOKENS: frozenset[str] = frozenset(
    {"median", "percentilecont", "percentiledisc", "percentile_cont", "percentile_disc"}
)


def is_quantile_agg_token(token: str | None) -> bool:
    """True when ``token`` denotes a scalar quantile through ANY spelling.

    Covers the canonical pNN suffixes (``p01``..``p99``) AND the non-suffix
    aliases: the legacy ``median`` default_agg synonym and the sqlglot HAVING
    function keys. Use this — never a bare ``is_quantile_stat_type`` — wherever a
    ``default_agg`` value or an aggregate-function name is tested for
    quantile-ness, so a legacy ``median`` row or a HAVING ``MEDIAN()`` cannot slip
    past the exactness/non-additive gates (a wrong-numbers escape).
    """
    t = (token or "").strip().lower()
    return t in _SUFFIX_TO_PCT or t in _QUANTILE_ALIAS_TOKENS


def quantile_suffix_to_percentile(suffix: str | None) -> int | None:
    """Reverse of ``quantile_suffix``: 'p50' -> 50, 'p90' -> 90; None if not a
    canonical quantile suffix. Used by the BigQuery refresh builder to turn an
    existing pNN coverage row back into its ``APPROX_QUANTILES[OFFSET(pct)]``.
    """
    return _SUFFIX_TO_PCT.get(suffix or "")


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
