"""Canonical dispersion-statistic stat types (STDDEV / VARIANCE).

Single source of truth shared by the optimizer (create), scheduler (refresh),
and query-router (parse / route / rewrite), mirroring ``aggregate_quantiles``
for the percentile family.

These statistics are **not re-aggregatable** (a stddev of stddevs is not the
stddev of the union), so — like percentiles and count_distinct — they route
ONLY at EXACT grain, where the stored column value IS the exact answer.

Unlike percentiles, the four functions are standard SQL with identical names on
PostgreSQL, Spark, and BigQuery and transpile cleanly via sqlglot, so there is
no exact/approximate split and no per-dialect expression table.

Materialisation is OPT-IN per aggregate via ``include_stats`` (the analogue of
``include_quantiles``): a measure is numeric and these columns add cost, so they
are only built when the user asks for them.
"""
from __future__ import annotations

# Canonical stat types and their physical column suffixes ({measure}__{stat}).
STAT_TYPES: list[str] = ["stddev_pop", "stddev_samp", "var_pop", "var_samp"]
_STAT_SET = frozenset(STAT_TYPES)

# PostgreSQL-canonical SQL templates. Identical function names on Spark and
# BigQuery; the cross-db builders transpile the whole SELECT via sqlglot and the
# names survive unchanged (verified: STDDEV_POP/STDDEV_SAMP/VAR_POP stay as-is,
# VAR_SAMP <-> VARIANCE is an exact alias).
STAT_SQL_TEMPLATES: dict[str, str] = {
    "stddev_pop": "STDDEV_POP({ref})",
    "stddev_samp": "STDDEV_SAMP({ref})",
    "var_pop": "VAR_POP({ref})",
    "var_samp": "VAR_SAMP({ref})",
}

# sqlglot normalises the surface function names to these expression keys; map
# them back to the canonical stat type. Bare STDDEV == sample, bare VARIANCE ==
# sample (PostgreSQL/ANSI semantics), which is what sqlglot emits.
_SQLGLOT_KEY_TO_STAT: dict[str, str] = {
    "stddev": "stddev_samp",
    "stddevsamp": "stddev_samp",
    "stddevpop": "stddev_pop",
    "variance": "var_samp",
    "variancesamp": "var_samp",
    "variancepop": "var_pop",
}


def is_stat_type(stat_type: str | None) -> bool:
    """True for one of the canonical dispersion stat types."""
    return (stat_type or "") in _STAT_SET


def stat_type_for_sqlglot_key(key: str | None) -> str | None:
    """Map a sqlglot aggregate-expression key (e.g. 'stddevpop', 'variance')
    to the canonical stat type, or None when it is not a dispersion stat."""
    return _SQLGLOT_KEY_TO_STAT.get((key or "").lower())


def stat_columns_for_layout(layout, measure_source_ref, *, skip_physical_cols=None):
    """Return ``(alias, sql_expr)`` pairs for the materialised stat columns of
    a resolved aggregate layout.

    Shared by every DDL builder (scheduler + optimizer, all dialects) so the
    column set and naming stay identical. ``measure_source_ref(measure)`` must
    return the dialect-quoted source-column expression. Emitted once per
    numeric (sum/avg) measure — min/max/count/quantile/stat rows are skipped by
    the agg filter, and avg+sum on the same measure dedupe by name.

    ``skip_physical_cols``: physical column names whose rows must be excluded
    entirely — used for variant-measure columns: create-time builders never
    emit stat columns for variants, so refresh must not either (F-009-01
    parity).
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    skip = skip_physical_cols or set()
    for measure in layout.measure_cols:
        if measure.stat_type == "calculated":
            continue
        if measure.physical_col_name in skip:
            continue
        agg = (measure.aggregation_function or measure.stat_type or "sum").lower()
        if agg not in ("sum", "avg") or measure.measure_name in seen:
            continue
        seen.add(measure.measure_name)
        ref = measure_source_ref(measure)
        from shared.semantic.grain_resolver import bound_ident
        for stat in STAT_TYPES:
            out.append(
                (
                    bound_ident(f"{measure.measure_name}__{stat}"),
                    STAT_SQL_TEMPLATES[stat].format(ref=ref),
                )
            )
    return out
