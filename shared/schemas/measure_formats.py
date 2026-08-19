"""Closed enum of presentation format tokens for measures.

The frontend renders values client-side; the backend only stores the
token and validates it on create/update. Adding a token requires a
matching entry in ``frontend/src/utils/measureFormat.ts``.
"""
from __future__ import annotations

MEASURE_FORMAT_TOKENS: frozenset[str] = frozenset({
    "currency",
    "percent",
    "percent_2dp",
    "integer",
    "decimal_2dp",
    "decimal_0",
    "decimal_1",
    "decimal_3",
    "decimal_4",
    "decimal_5",
    "decimal_6",
})


def is_valid_format(token: str | None) -> bool:
    if token is None or token == "":
        return True
    return token in MEASURE_FORMAT_TOKENS


# Server-side token -> SSAS/.NET FORMAT_STRING, for XMLA/Excel cell formatting
# (Bug-5432). The Tessallite frontend formats client-side from the token; XMLA
# clients (Excel/Power BI) cannot, so the gateway must translate the token to a
# real format string for MDSCHEMA_MEASURES.DEFAULT_FORMAT_STRING and per-cell
# FORMAT_STRING / FmtValue. Keep aligned with the frontend map
# (frontend/src/utils/measureFormat.ts) and excel-plugin officeSpike FORMAT_MAP.
MEASURE_FORMAT_TO_MDX: dict[str, str] = {
    "currency": "$#,##0.00",
    "percent": "0%",
    "percent_2dp": "0.00%",
    "integer": "#,##0",
    "decimal_0": "#,##0",
    "decimal_1": "#,##0.0",
    "decimal_2dp": "#,##0.00",
    "decimal_3": "#,##0.000",
    "decimal_4": "#,##0.0000",
    "decimal_5": "#,##0.00000",
    "decimal_6": "#,##0.000000",
}


def format_token_to_mdx(token: str | None) -> str | None:
    """Translate a measure format TOKEN to an SSAS/.NET FORMAT_STRING for XMLA.

    Returns None for empty/unknown (caller then omits formatting). A value that
    already looks like a literal .NET format string (contains ``# 0 % $``) is
    passed through unchanged, so callers may also store raw format strings.
    """
    if not token:
        return None
    if token in MEASURE_FORMAT_TO_MDX:
        return MEASURE_FORMAT_TO_MDX[token]
    if any(c in token for c in "#0%$"):
        return token
    return None


HIERARCHY_TIME_UNITS: frozenset[str] = frozenset({
    "year", "half", "quarter", "month", "week", "day", "hour", "none",
})


HIERARCHY_TIME_CALCS: frozenset[str] = frozenset({
    "lag", "parallel_period", "period_to_date", "range", "moving_window",
})


HIERARCHY_DIMENSION_KINDS: frozenset[str] = frozenset({
    "time", "geo", "entity",
})


def is_valid_time_unit(token: str | None) -> bool:
    if token is None or token == "":
        return True
    return token in HIERARCHY_TIME_UNITS


def are_valid_time_calcs(tokens: list[str] | None) -> bool:
    if not tokens:
        return True
    return all(t in HIERARCHY_TIME_CALCS for t in tokens)


def is_valid_dimension_kind(token: str | None) -> bool:
    if token is None or token == "":
        return True
    return token in HIERARCHY_DIMENSION_KINDS


# ---------------------------------------------------------------------------
# Phase 2 — time-intelligence variants
# ---------------------------------------------------------------------------

TIME_VARIANT_NAMES: frozenset[str] = frozenset({
    "lag",
    "prior_year",
    "prior_quarter",
    "prior_month",
    "prior_week",
    "ytd",
    "qtd",
    "mtd",
    "wtd",
    "ytd_prior_year",
    "yoy_growth",
    "yoy_growth_pct",
    "trailing_n",
    "moving_avg_n",
    "last_n_periods",
    "period_to_date",
    "same_period_last_year",
    "lead",
    "cagr",
    "pct_change",
})


# Maps a variant name to the level-capability family that must be present
# on the associated time hierarchy for the variant to be admissible. Used by
# catalog expansion (Phase 2 Step 3) to silently drop variants whose
# family is not supported by the hierarchy the measure binds to.
TIME_VARIANT_FAMILY: dict[str, str] = {
    "lag": "lag",
    "prior_year": "parallel_period",
    "prior_quarter": "parallel_period",
    "prior_month": "parallel_period",
    "prior_week": "parallel_period",
    "ytd": "period_to_date",
    "qtd": "period_to_date",
    "mtd": "period_to_date",
    "wtd": "period_to_date",
    "ytd_prior_year": "period_to_date",
    "yoy_growth": "parallel_period",
    "yoy_growth_pct": "parallel_period",
    "trailing_n": "moving_window",
    "moving_avg_n": "moving_window",
    "last_n_periods": "moving_window",
    "period_to_date": "period_to_date",
    "same_period_last_year": "parallel_period",
    "lead": "lag",
    "cagr": "parallel_period",
    "pct_change": "parallel_period",
}


# Maps a variant name to the time_unit that must be present on at least
# one level of the associated hierarchy. Variants whose unit is not present
# are dropped from the catalog (Phase 2 Q2 decision).
TIME_VARIANT_REQUIRED_UNIT: dict[str, str | None] = {
    "lag": None,                # works against any unit
    "prior_year": "year",
    "prior_quarter": "quarter",
    "prior_month": "month",
    "prior_week": "week",
    "ytd": "year",
    "qtd": "quarter",
    "mtd": "month",
    "wtd": "week",
    "ytd_prior_year": "year",
    "yoy_growth": "year",
    "yoy_growth_pct": "year",
    "trailing_n": None,         # works against any unit; uses measure.trailing_n
    "moving_avg_n": None,       # works against any unit; uses measure.moving_avg_n
    "last_n_periods": None,     # alias for trailing_n
    "period_to_date": "year",   # alias for ytd
    "same_period_last_year": "year",  # alias for prior_year
    "lead": None,               # works against any unit
    "cagr": "year",
    "pct_change": None,         # works against any unit
}


def is_valid_time_variant(token: str | None) -> bool:
    if token is None or token == "":
        return True
    return token in TIME_VARIANT_NAMES


# Default values applied when a measure has time_variants_enabled=true but
# trailing_n / moving_avg_n columns are NULL (Phase 2 Q3 decision).
TIME_VARIANT_DEFAULT_TRAILING_N = 12
TIME_VARIANT_DEFAULT_MOVING_AVG_N = 30


# Bug-6222 (F-015-27): variant families that cumulate or window-aggregate
# the base value.  These are semantically incorrect for semi-additive
# measures (e.g. last_non_empty balances) because they sum per-period
# balances instead of carrying the period-end balance forward.  Shared
# constant so both the catalog gate (admissible_variant_kinds) and the
# execution guard (source_sql.py) use the same set.
SEMI_ADDITIVE_INELIGIBLE_FAMILIES: frozenset[str] = frozenset({
    "period_to_date",   # ytd, qtd, mtd, wtd, ytd_prior_year
    "moving_window",    # trailing_n, moving_avg_n
})


def admissible_variant_kinds(
    *,
    level_units: frozenset[str] | set[str],
    level_calcs: frozenset[str] | set[str],
    has_calendar_rules: bool = False,
    calendar_bound: bool | None = None,
    semi_additive_behavior: str | None = None,
) -> list[str]:
    """Variant kinds admissible for a base measure under these capabilities.

    ``has_calendar_rules`` is True when the associated time hierarchy carries a
    calendar_type (NULL treated as 'standard').  Period-boundary variants
    (YTD, QTD, prior_year, ...) require this flag.

    ``calendar_bound`` is the legacy alias kept for backward compatibility;
    when passed, it overrides ``has_calendar_rules``.

    ``semi_additive_behavior`` (Bug-6222): when set, cumulation and
    moving-window families are excluded because semi-additive measures
    represent balances, not flows -- cumulating them produces incorrect
    results.
    """
    if calendar_bound is not None:
        has_calendar_rules = calendar_bound
    units = set(level_units)
    calcs = set(level_calcs)
    # F-015-12 / F-015-13: emit only the canonical 14 kinds so this surface
    # agrees with ``/available-variants`` (previously this returned 17,
    # including alias kinds, while the popover returned 14).
    out: list[str] = []
    for variant in CANONICAL_TIME_VARIANT_ORDER:
        family = TIME_VARIANT_FAMILY[variant]
        if family not in calcs:
            continue
        required_unit = TIME_VARIANT_REQUIRED_UNIT[variant]
        if required_unit is not None and required_unit not in units:
            continue
        if variant in TIME_VARIANTS_NEEDING_CALENDAR and not has_calendar_rules:
            continue
        # Bug-6222: reject cumulation/window variants for semi-additive measures.
        if semi_additive_behavior and family in SEMI_ADDITIVE_INELIGIBLE_FAMILIES:
            continue
        out.append(variant)
    return out


# Window-family kinds (family ``lag`` or ``moving_window``). These operate
# on the fact's own date column and need NEITHER a hierarchy NOR a calendar.
# Their date anchor is the modeller-selected ``date_dimension_column_id``
# (F-015-01), not the hierarchy-derived ``resolved_date_col_id``. Their
# creation eligibility requires only a validated date column (F-015-02).
WINDOW_VARIANT_FAMILIES: frozenset[str] = frozenset({"lag", "moving_window"})


def is_window_variant(kind: str | None) -> bool:
    """True when ``kind`` is a pure-window variant (lag / trailing_n /
    moving_avg_n and their aliases). Window variants order by the fact's own
    ``date_dimension_column_id`` and require no hierarchy or calendar."""
    if kind is None:
        return False
    return TIME_VARIANT_FAMILY.get(kind) in WINDOW_VARIANT_FAMILIES


# Variants that need a bound calendar table to resolve period boundaries.
# Pure-window variants (lag, trailing_n, moving_avg_n) operate on the
# fact's own date column and do not need a calendar.
TIME_VARIANTS_NEEDING_CALENDAR: frozenset[str] = frozenset({
    "prior_year", "prior_quarter", "prior_month", "prior_week",
    "ytd", "qtd", "mtd", "wtd",
    "ytd_prior_year", "yoy_growth", "yoy_growth_pct",
    "period_to_date", "same_period_last_year",
})


# ---------------------------------------------------------------------------
# Canonical variant catalog (F-015-13: single source of truth across layers)
# ---------------------------------------------------------------------------
#
# ``TIME_VARIANT_NAMES`` (above) is the *validation* surface — it stays
# permissive so historical rows and raw-API callers carrying alias kinds
# (last_n_periods / period_to_date / same_period_last_year) or the
# undocumented extras (lead / cagr / pct_change) continue to validate. But
# only these 14 kinds are the documented, catalog-admitted, UI-offered
# canon. Eligibility (``/available-variants`` and the single-measure
# ``eligible_variant_kinds``) and the picker converge on this list, so the
# drawer no longer presents semantic duplicates ("YTD" and "Period to date").
CANONICAL_TIME_VARIANT_ORDER: tuple[str, ...] = (
    "lag",
    "prior_year",
    "prior_quarter",
    "prior_month",
    "prior_week",
    "ytd",
    "qtd",
    "mtd",
    "wtd",
    "ytd_prior_year",
    "yoy_growth",
    "yoy_growth_pct",
    "trailing_n",
    "moving_avg_n",
)
CANONICAL_TIME_VARIANTS: frozenset[str] = frozenset(CANONICAL_TIME_VARIANT_ORDER)

# Alias kinds map onto a canonical kind with identical semantics. Used to
# fold aliases at the API boundary so the catalog presents one entry per
# concept.
TIME_VARIANT_ALIASES: dict[str, str] = {
    "last_n_periods": "trailing_n",
    "period_to_date": "ytd",
    "same_period_last_year": "prior_year",
}


def canonical_variant_kind(kind: str | None) -> str | None:
    """Fold an alias variant kind onto its canonical kind; pass through
    canonical and unknown kinds unchanged."""
    if kind is None:
        return None
    return TIME_VARIANT_ALIASES.get(kind, kind)


# Human-readable display labels for variant kinds. The backend uses these to
# build ``suggested_display_name`` so the BI catalog never shows raw tokens
# like "Revenue (ytd_prior_year)" (F-015-22). Mirrors the frontend i18n
# defaults in ``constants/timeVariants.ts`` (English source values).
TIME_VARIANT_DISPLAY_LABELS: dict[str, str] = {
    "lag": "Lag",
    "prior_year": "Prior Year",
    "prior_quarter": "Prior Quarter",
    "prior_month": "Prior Month",
    "prior_week": "Prior Week",
    "ytd": "Year to Date",
    "qtd": "Quarter to Date",
    "mtd": "Month to Date",
    "wtd": "Week to Date",
    "ytd_prior_year": "YTD Prior Year",
    "yoy_growth": "YoY Growth",
    "yoy_growth_pct": "YoY Growth %",
    "trailing_n": "Trailing N",
    "moving_avg_n": "Moving Average N",
    "last_n_periods": "Last N Periods",
    "period_to_date": "Period to Date",
    "same_period_last_year": "Same Period Last Year",
}


def variant_display_label(kind: str) -> str:
    """Human-readable label for a variant kind, falling back to a
    title-cased token for unknown kinds."""
    return TIME_VARIANT_DISPLAY_LABELS.get(kind, kind.replace("_", " ").title())
