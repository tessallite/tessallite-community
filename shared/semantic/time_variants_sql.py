"""Dialect-aware SQL emission for time-intelligence variants (Phase 2 Step 4).

Pure functions: given a base measure expression, a variant name, and the
binding context (calendar table, time grain column, target dialect),
return a SQL fragment that computes the variant.

Strategy
--------
Variant SQL is authored once in **canonical Postgres**. For non-Postgres
targets, the fragment is run through ``sqlglot.transpile`` (the codebase
already depends on sqlglot — see ``shared/semantic/sql_builder.py``).
This avoids maintaining a parallel triplet of DATE_SUB / INTERVAL /
add_months branches, and keeps the BigQuery / Spark renderings under
sqlglot's upstream maintenance.

The emitter does NOT build the surrounding SELECT; it returns the
column expression that should appear after ``SELECT`` (or in a SELECT
list). Callers wrap it with the appropriate FROM / JOIN / GROUP BY.
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Optional

import sqlglot
from sqlglot.dialects.bigquery import BigQuery as _BigQueryDialect
from sqlglot.dialects.spark import Spark as _SparkDialect

from ..schemas.measure_formats import (
    TIME_VARIANT_DEFAULT_MOVING_AVG_N,
    TIME_VARIANT_DEFAULT_TRAILING_N,
    TIME_VARIANT_NAMES,
    TIME_VARIANTS_NEEDING_CALENDAR,
)


# ---------------------------------------------------------------------------
# Register missing sqlglot dialect mappings (sqlglot 30.x gaps)
# ---------------------------------------------------------------------------
# PG ISODOW (1=Mon..7=Sun) → BQ DAYOFWEEK (1=Sun..7=Sat).
# Values differ but both assign a unique int per weekday — safe for
# PARTITION BY / GROUP BY which only need equality, not magnitude.
if "ISODOW" not in _BigQueryDialect.DATE_PART_MAPPING:
    _BigQueryDialect.DATE_PART_MAPPING["ISODOW"] = "DAYOFWEEK"
_BigQueryDialect.Generator.NORMALIZE_EXTRACT_DATE_PARTS = True
# Spark has no ISOYEAR / ISODOW extract fields; the documented Spark
# equivalents are YEAROFWEEK (ISO week-numbering year) and DOW_ISO
# (1=Mon..7=Sun, value-identical to PG ISODOW). Same registration
# pattern as the BigQuery ISODOW mapping above (B5, F-015-01).
if "ISOYEAR" not in _SparkDialect.DATE_PART_MAPPING:
    _SparkDialect.DATE_PART_MAPPING["ISOYEAR"] = "YEAROFWEEK"
if "ISODOW" not in _SparkDialect.DATE_PART_MAPPING:
    _SparkDialect.DATE_PART_MAPPING["ISODOW"] = "DOW_ISO"
_SparkDialect.Generator.NORMALIZE_EXTRACT_DATE_PARTS = True


SUPPORTED_DIALECTS = frozenset({
    "postgresql", "bigquery", "hadoop_spark", "snowflake",
    "redshift", "sqlserver",
})

# Map our dialect tokens to sqlglot's. Postgres is the canonical
# authoring dialect, so it never round-trips through transpile.
_SQLGLOT_DIALECT = {
    "postgresql": "postgres",
    "bigquery": "bigquery",
    "hadoop_spark": "spark",
    "snowflake": "snowflake",
    "redshift": "redshift",
    "sqlserver": "tsql",
}


@dataclass(frozen=True)
class VariantBinding:
    """Resolved context the emitter needs to render a single variant.

    partition_by carries the physical SQL expressions of every non-time
    dimension in the query grain, so window-style variants can scope
    cumulation per slice (Bug-090 Part C).

    calendar_type + fiscal_year_start_month enable expression-based period
    boundary computation when no calendar table is present.  NULL or
    'standard' means Gregorian with Jan-1 year start.
    """
    base_expression: str
    fact_date_column: str
    calendar_alias: str = "cal"
    calendar_columns: Optional[dict[str, str]] = None
    base_unaggregated: Optional[str] = None
    dialect: str = "postgresql"
    n: Optional[int] = None
    partition_by: tuple[str, ...] = ()
    calendar_type: Optional[str] = None
    fiscal_year_start_month: Optional[int] = None


class VariantSqlError(ValueError):
    """Raised when a variant cannot be rendered in the given context."""


@dataclass(frozen=True)
class VariantSql:
    """Structured result from ``emit_variant_expression``.

    ``referenced_calendar_keys`` lists the calendar period keys (e.g.
    ``"year"``, ``"quarter"``) that appear in the emitted SQL via the
    calendar table. Callers use this to add GROUP BY entries for the
    calendar columns without brittle string containment checks.
    """
    sql: str
    referenced_calendar_keys: frozenset[str]


# F-016-22: the calendar-key accumulator is a per-call ContextVar, not a
# module-global mutable list. ``emit_variant_expression`` installs a fresh
# list for the duration of one render and resets it afterwards, so concurrent
# renders (async tasks, future thread-pool execution) never see each other's
# referenced keys. ``_period_column`` reads the active list via
# ``_track_calendar_key`` and is a no-op when no render is in flight.
_referenced_keys_var: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "time_variants_referenced_keys", default=None
)


def _track_calendar_key(key: str) -> None:
    """Record a calendar period key referenced by the in-flight render."""
    acc = _referenced_keys_var.get()
    if acc is not None:
        acc.append(key)


def emit_variant_expression(
    variant_name: str, binding: VariantBinding,
) -> VariantSql:
    """Return the SQL fragment and calendar metadata for ``variant_name``.

    Raises VariantSqlError on unsupported variant or missing context.
    """
    if variant_name not in TIME_VARIANT_NAMES:
        raise VariantSqlError(f"Unknown variant {variant_name!r}")
    if binding.dialect not in SUPPORTED_DIALECTS:
        raise VariantSqlError(f"Unsupported dialect {binding.dialect!r}")

    needs_cal = variant_name in TIME_VARIANTS_NEEDING_CALENDAR
    has_rules = binding.calendar_columns or (binding.calendar_type is not None)
    if needs_cal and not has_rules:
        raise VariantSqlError(
            f"Variant {variant_name!r} requires calendar rules "
            f"(calendar_type on the hierarchy or a calendar table)"
        )

    handler = _HANDLERS.get(variant_name)
    if handler is None:
        raise VariantSqlError(f"No handler registered for {variant_name!r}")

    accumulator: list[str] = []
    token = _referenced_keys_var.set(accumulator)
    try:
        pg_sql = handler(binding)
        keys = frozenset(accumulator)
    finally:
        _referenced_keys_var.reset(token)
    sql = _to_dialect(pg_sql, binding.dialect)
    return VariantSql(sql=sql, referenced_calendar_keys=keys)


# ---------------------------------------------------------------------------
# Dialect transpilation
# ---------------------------------------------------------------------------


def _to_dialect(pg_sql: str, dialect: str) -> str:
    if dialect == "postgresql":
        return pg_sql
    target = _SQLGLOT_DIALECT[dialect]
    try:
        return sqlglot.transpile(pg_sql, read="postgres", write=target)[0]
    except Exception as exc:  # pragma: no cover — defensive
        raise VariantSqlError(
            f"Failed to transpile variant SQL to {dialect!r}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Canonical Postgres handlers — one per variant
# ---------------------------------------------------------------------------


def _partition_clause(b: VariantBinding, *period_keys: str) -> str:
    """Build the contents of an OVER (PARTITION BY ...) clause.

    Combines b.partition_by (non-time grain dims, supplied by the rewriter)
    with any period columns the variant itself needs (e.g. cal.year_no for
    YTD). Returns an empty string when neither is present so callers can
    omit PARTITION BY entirely.
    """
    cols: list[str] = list(b.partition_by)
    for key in period_keys:
        cols.append(_period_column(b, key))
    return ", ".join(cols)


def _h_lag(b: VariantBinding) -> str:
    base = b.base_unaggregated or b.base_expression
    parts = _partition_clause(b)
    over = f"PARTITION BY {parts} ORDER BY {b.fact_date_column}" if parts \
        else f"ORDER BY {b.fact_date_column}"
    return f"LAG({base}) OVER ({over})"


# Internal period keys (not part of the calendar_columns vocabulary):
#   month_year — the year that scopes month/quarter instances. Resolves to
#       the calendar table's year column when present, else the fiscal CASE
#       expression or EXTRACT(YEAR). Never ISOYEAR: months and quarters are
#       Gregorian/fiscal objects, and pairing them with the ISO
#       week-numbering year mis-buckets the days around Jan 1 (B5/F-015-01).
#   week_year — the ISO week-numbering year that scopes week instances.
#       Always EXTRACT(ISOYEAR FROM fact_date): every shipping calendar
#       dialect's week column is the ISO week number (EXTRACT(WEEK)), and an
#       ISO week belongs to exactly one ISO year, so (ISOYEAR, week) is the
#       unique week identifier. The calendar table's *year* column must NOT
#       be used here — the standard table pairs a Gregorian year_no with an
#       ISO week_no, which splits the weeks straddling Jan 1 (B5/F-015-01).
_PERIOD_KEY_CALENDAR_LOOKUP: dict[str, str] = {"month_year": "year"}
_PERIOD_KEYS_NEVER_FROM_CALENDAR: frozenset[str] = frozenset({"week_year"})

# Calendar types whose year/position math follows the ISO week grid. The
# canonical token is ``iso_week`` (F-016-04); the bare ``iso`` is the
# pre-H9 hierarchy spelling and is still accepted so an un-migrated row keeps
# computing correctly.
_ISO_CALENDAR_TYPES: frozenset[str] = frozenset({"iso_week", "iso"})


def _period_column(b: VariantBinding, key: str) -> str:
    cols = b.calendar_columns or {}
    lookup = _PERIOD_KEY_CALENDAR_LOOKUP.get(key, key)
    if key not in _PERIOD_KEYS_NEVER_FROM_CALENDAR and lookup in cols:
        _track_calendar_key(lookup)
        return f'"{b.calendar_alias}"."{cols[lookup]}"'
    return _extract_period(b, key)


def _extract_period(b: VariantBinding, key: str) -> str:
    """Compute a period boundary expression from the fact date column."""
    d = b.fact_date_column
    cal_type = (b.calendar_type or "standard").lower()
    fy_start = b.fiscal_year_start_month or 1

    if key == "date":
        return d

    if cal_type == "fiscal" and fy_start != 1:
        if key == "year":
            return (
                f"CASE WHEN EXTRACT(MONTH FROM {d}) >= {fy_start} "
                f"THEN EXTRACT(YEAR FROM {d}) "
                f"ELSE EXTRACT(YEAR FROM {d}) - 1 END"
            )
        if key == "quarter":
            return (
                f"FLOOR(MOD(EXTRACT(MONTH FROM {d}) "
                f"- {fy_start} + 12, 12) / 3) + 1"
            )

    if key == "year":
        if cal_type in _ISO_CALENDAR_TYPES:
            return f"EXTRACT(ISOYEAR FROM {d})"
        return f"EXTRACT(YEAR FROM {d})"
    if key == "month_year":
        # Year scope for month/quarter partitions — fiscal-aware, never ISO
        # (see the comment on _PERIOD_KEY_CALENDAR_LOOKUP).
        if cal_type == "fiscal" and fy_start != 1:
            return (
                f"CASE WHEN EXTRACT(MONTH FROM {d}) >= {fy_start} "
                f"THEN EXTRACT(YEAR FROM {d}) "
                f"ELSE EXTRACT(YEAR FROM {d}) - 1 END"
            )
        return f"EXTRACT(YEAR FROM {d})"
    if key == "week_year":
        # ISO week-numbering year — the only year that consistently scopes
        # ISO week numbers across the Jan-1 boundary.
        return f"EXTRACT(ISOYEAR FROM {d})"
    if key == "quarter":
        return f"EXTRACT(QUARTER FROM {d})"
    if key == "month":
        return f"EXTRACT(MONTH FROM {d})"
    if key == "week":
        return f"EXTRACT(WEEK FROM {d})"
    if key == "half":
        if cal_type == "fiscal" and fy_start != 1:
            fiscal_qtr = (
                f"FLOOR(MOD(EXTRACT(MONTH FROM {d}) "
                f"- {fy_start} + 12, 12) / 3) + 1"
            )
            return f"CASE WHEN {fiscal_qtr} <= 2 THEN 1 ELSE 2 END"
        return f"CASE WHEN EXTRACT(QUARTER FROM {d}) <= 2 THEN 1 ELSE 2 END"

    raise VariantSqlError(f"No expression for period key {key!r}")


def _ptd(b: VariantBinding, *partition_keys: str) -> str:
    """Period-to-date: cumulative SUM partitioned by the given period columns.

    The partition must identify ONE period instance, so every sub-year
    period key needs its scoping year alongside it (B5/F-015-01): a bare
    ``quarter_no`` puts Q1-2023 and Q1-2024 in the same partition and QTD
    silently cumulates across years.
    """
    base = b.base_unaggregated or b.base_expression
    part = _partition_clause(b, *partition_keys)
    return (
        f"SUM({base}) OVER ("
        f"PARTITION BY {part} "
        f"ORDER BY {b.fact_date_column} "
        f"ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
    )


# F-015-01: qtd/mtd partition by (year, period) — month_year is the
# fiscal-aware Gregorian year; wtd partitions by the ISO week-numbering
# year because EXTRACT(WEEK) / every calendar dialect's week_no is the ISO
# week number, and (ISOYEAR, week) is the unique week instance identifier.
def _h_ytd(b: VariantBinding) -> str: return _ptd(b, "year")
def _h_qtd(b: VariantBinding) -> str: return _ptd(b, "month_year", "quarter")
def _h_mtd(b: VariantBinding) -> str: return _ptd(b, "month_year", "month")
def _h_wtd(b: VariantBinding) -> str: return _ptd(b, "week_year", "week")


def _prior(b: VariantBinding, unit: str) -> str:
    """Compare-to-prior-<unit> via LAG window function.

    Partitions by sub-period components (month+day for prior_year, day for
    prior_quarter/month, iso-dow for prior_week) so LAG(1) yields the same
    sub-period in the prior parent period. When calendar columns are
    available, ``_period_column`` resolves to the calendar table's columns;
    otherwise it falls back to EXTRACT expressions from the fact date.

    Supports multi-grain queries: non-time dimensions in ``partition_by``
    are prepended so the LAG scopes correctly per slice.
    """
    base = b.base_unaggregated or b.base_expression
    sub_parts, ord_key, max_gap_step = _prior_partition_keys(b, unit)
    all_parts = list(b.partition_by) + sub_parts
    parts_str = ", ".join(all_parts)
    over = (
        f"PARTITION BY {parts_str} "
        f"ORDER BY {ord_key}"
    )
    lag_val = f"LAG({base}, 1) OVER ({over})"
    # F-015-19: LAG(1) returns the previous *existing* row, which silently
    # bridges a gap (e.g. prior_year of 2024-03 returns 2022-03 when 2023-03
    # has no row). Standard BI semantics return NULL for a missing parallel
    # period. Guard by also lagging the ordering key (the composite parent-
    # period key) and requiring the previous row to be the adjacent prior
    # period: the key gap must fall within [1, max_gap_step]. For year /
    # quarter / month the only legitimate step is 1; for week the year-
    # boundary step is 2 (a 52-week ISO year's week 52 → next year's week 1
    # under the *53 scaling), so weeks tolerate up to 2.
    lag_key = f"LAG({ord_key}, 1) OVER ({over})"
    if max_gap_step <= 1:
        adjacency = f"({lag_key}) = ({ord_key}) - 1"
    else:
        adjacency = (
            f"({ord_key}) - ({lag_key}) BETWEEN 1 AND {max_gap_step}"
        )
    return (
        f"CASE WHEN {lag_key} IS NOT NULL AND {adjacency} "
        f"THEN {lag_val} ELSE NULL END"
    )


def _fact_month_position(b: VariantBinding) -> str:
    """1-based month position within the (possibly fiscal) year, derived
    from the fact date column only.

    Deliberately avoids calendar-table columns: this expression is used in
    PARTITION BY / ORDER BY positions where referencing a calendar column
    would force an extra GROUP BY entry and could explode the query grain
    (e.g. a month column at quarter grain). ``fact_date_column`` is already
    grain-safe — the rewriter passes it pre-aggregated (``MIN(date)``).
    """
    d = b.fact_date_column
    cal_type = (b.calendar_type or "standard").lower()
    fy_start = b.fiscal_year_start_month or 1
    if cal_type == "fiscal" and fy_start != 1:
        return f"(MOD(EXTRACT(MONTH FROM {d}) - {fy_start} + 12, 12) + 1)"
    return f"EXTRACT(MONTH FROM {d})"


def _prior_partition_keys(
    b: VariantBinding, unit: str
) -> tuple[list[str], str, int]:
    """Return (sub-period partition columns, ordering key, max_gap_step) for a
    prior-unit LAG.

    Uses ``_period_column`` for ordering keys so calendar-table columns are
    preferred when available, falling back to EXTRACT expressions otherwise.

    The partition keys must pin the row's POSITION inside its parent period
    so that ``LAG(1)`` ordered by the parent-period key lands on the same
    position one period earlier. Missing position keys make peers inside the
    same period tie on both partition and ordering key, and ``LAG(1)``
    returns an arbitrary sibling (B5/F-015-03).

    ``max_gap_step`` is the largest legitimate difference between two adjacent
    ordering keys; the gap guard (F-015-19) uses it to distinguish a real
    adjacent prior period from a bridged gap.
    """
    d = b.fact_date_column
    if unit == "year":
        return (
            [_period_column(b, "month"), f"EXTRACT(DAY FROM {d})"],
            _period_column(b, "year"),
            1,
        )
    if unit == "quarter":
        # Bug-3608: quarters are Gregorian/fiscal objects — scope them on the
        # Gregorian month_year (matching _h_qtd), never the ISO week-numbering
        # year. Pairing ISOYEAR with a Gregorian quarter mis-buckets the days
        # straddling Jan 1 (e.g. 2024-12-30 is ISOYEAR 2025 / Gregorian Q4-2024).
        year_key = _period_column(b, "month_year")
        qtr_key = _period_column(b, "quarter")
        # F-015-03: month-position-within-quarter (0..2) disambiguates the
        # three months of a quarter; without it Jan/Feb/Mar tie on
        # (day, year*4+quarter) at month grain and LAG(1) is
        # nondeterministic. Fiscal-aware via _fact_month_position so the
        # position is relative to the fiscal quarter boundary.
        month_in_quarter = f"MOD({_fact_month_position(b)} - 1, 3)"
        # year*4+quarter: Q4-y = y*4+4, Q1-(y+1) = (y+1)*4+1 — adjacent step 1.
        return (
            [month_in_quarter, f"EXTRACT(DAY FROM {d})"],
            f"{year_key} * 4 + {qtr_key}",
            1,
        )
    if unit == "month":
        # Bug-3608: months are Gregorian/fiscal objects — scope them on the
        # Gregorian month_year (matching _h_mtd), never the ISO week-numbering
        # year. Pairing ISOYEAR with a Gregorian month mis-buckets the days
        # straddling Jan 1.
        year_key = _period_column(b, "month_year")
        # year*12+month: Dec-y = y*12+12, Jan-(y+1) = (y+1)*12+1 — step 1.
        return (
            [f"EXTRACT(DAY FROM {d})"],
            f"{year_key} * 12 + {_period_column(b, 'month')}",
            1,
        )
    if unit == "week":
        # Same family as F-015-01 wtd: week numbers are ISO weeks, so the
        # ordering key must use the ISO week-numbering year. A Gregorian
        # year key mis-orders the boundary days (2024-12-30 is ISO
        # 2025-W01; keying it 2024*53+1 sorts it before 2024-W52).
        year_key = _period_column(b, "week_year")
        week_key = _period_column(b, "week")
        # year*53+week: within a year adjacent weeks step by 1; at the year
        # boundary a 52-week ISO year's W52 = y*53+52 → next year W1 =
        # (y+1)*53+1 steps by 2. A 53-week year's W53 → next W1 steps by 1.
        # So weeks tolerate an adjacency gap up to 2 (F-015-19).
        return (
            [f"EXTRACT(ISODOW FROM {d})"],
            f"{year_key} * 53 + {week_key}",
            2,
        )
    raise VariantSqlError(f"Unknown prior unit {unit!r}")


def _h_prior_year(b: VariantBinding) -> str: return _prior(b, "year")
def _h_prior_quarter(b: VariantBinding) -> str: return _prior(b, "quarter")
def _h_prior_month(b: VariantBinding) -> str: return _prior(b, "month")
def _h_prior_week(b: VariantBinding) -> str: return _prior(b, "week")


# Frame constants for _h_ytd_prior_year. The composite ordering key is
#   K = year * _YEAR_KEY_SCALE + pos
# where pos is the row's position within its year (see _year_position):
#   month path: month_pos * 31 + day            → pos ∈ [32, 403]
#   iso path:   iso_week * 7 + iso_dow          → pos ∈ [8, 378]
# _MAX_POS bounds pos from above; _YEAR_KEY_SCALE > 2 * _MAX_POS guarantees
# the RANGE frame below can never leak rows from year-2 or the current year
# (proof in _h_ytd_prior_year's docstring).
_YEAR_KEY_SCALE = 1000
_MAX_POS = 403


def _year_position(b: VariantBinding) -> str:
    """Monotone, year-independent position of a row within its year.

    Derived from the fact date only (grain-safe — never references
    calendar-table columns, which would add GROUP BY entries and could
    explode the query grain). ``month_pos * 31 + day`` is strictly
    increasing within a year and identical across years for the same
    (month, day), so "same position, prior year" aligns calendar dates
    (DAX SAMEPERIODLASTYEAR semantics). For ISO calendars the position is
    ``iso_week * 7 + iso_dow`` so alignment follows the ISO week grid.
    """
    d = b.fact_date_column
    cal_type = (b.calendar_type or "standard").lower()
    if cal_type in _ISO_CALENDAR_TYPES:
        return f"(EXTRACT(WEEK FROM {d}) * 7 + EXTRACT(ISODOW FROM {d}))"
    return f"({_fact_month_position(b)} * 31 + EXTRACT(DAY FROM {d}))"


def _h_ytd_prior_year(b: VariantBinding) -> str:
    """Prior-year YTD at the same point in time (B5/F-015-02 ≡ F-016-01).

    A window function cannot reach outside its own partition, so the old
    ``PARTITION BY (year - 1)`` only relabelled partitions and returned the
    row's OWN year-to-date. ``LAG`` over the YTD value is also illegal
    (window calls cannot nest). The correct single-expression form is a
    RANGE-offset frame over the composite key K = year * S + pos
    (S = _YEAR_KEY_SCALE, pos = _year_position ∈ [1, _MAX_POS]):

        SUM(base) OVER (ORDER BY K
                        RANGE BETWEEN (S + _MAX_POS) PRECEDING
                              AND     S             PRECEDING)

    For the current row (year y, position p), the frame covers keys
    [ (y-1)*S + p - _MAX_POS , (y-1)*S + p ] — exactly the prior year's
    rows at positions <= p:
      * upper bound: rows of year y start at y*S + 1 > (y-1)*S + p
        because p <= _MAX_POS < S — the current year can never enter;
      * lower bound: a year y-2 row with position q needs
        q >= S + p - _MAX_POS > _MAX_POS (since S > 2*_MAX_POS) — impossible;
      * within year y-1 every position 1..p satisfies both bounds.
    An empty frame (no prior-year rows) yields NULL — the business-correct
    "no comparison period" answer.
    """
    base = b.base_unaggregated or b.base_expression
    year_key = _period_column(b, "year")
    order_key = f"({year_key} * {_YEAR_KEY_SCALE} + {_year_position(b)})"
    extra = ", ".join(b.partition_by)
    over_partition = f"PARTITION BY {extra} " if extra else ""
    return (
        f"SUM({base}) OVER ("
        f"{over_partition}"
        f"ORDER BY {order_key} "
        f"RANGE BETWEEN {_YEAR_KEY_SCALE + _MAX_POS} PRECEDING "
        f"AND {_YEAR_KEY_SCALE} PRECEDING)"
    )


def _h_yoy_growth(b: VariantBinding) -> str:
    base = b.base_unaggregated or b.base_expression
    py = _h_prior_year(b)
    return f"({base} - {py})"


def _h_yoy_growth_pct(b: VariantBinding) -> str:
    # F-015-05: force float division. SUM(int) is bigint in PostgreSQL and
    # bigint / bigint truncates toward zero, so (150-100)/100 returns 0 for
    # integer measures. The * 1.0 coercion mirrors _h_cagr / _h_pct_change.
    base = b.base_unaggregated or b.base_expression
    py = _h_prior_year(b)
    return f"(({base} - {py}) * 1.0 / NULLIF({py}, 0))"


def _rolling_window(b: VariantBinding, agg: str, n: int) -> str:
    base = b.base_unaggregated or b.base_expression
    parts = _partition_clause(b)
    over = f"PARTITION BY {parts} ORDER BY {b.fact_date_column}" if parts \
        else f"ORDER BY {b.fact_date_column}"
    return (
        f"{agg}({base}) OVER ("
        f"{over} "
        f"ROWS BETWEEN {n} PRECEDING AND CURRENT ROW)"
    )


def _h_trailing_n(b: VariantBinding) -> str:
    n = b.n if b.n is not None else TIME_VARIANT_DEFAULT_TRAILING_N
    return _rolling_window(b, "SUM", n)


def _h_moving_avg_n(b: VariantBinding) -> str:
    n = b.n if b.n is not None else TIME_VARIANT_DEFAULT_MOVING_AVG_N
    return _rolling_window(b, "AVG", n)


def _h_lead(b: VariantBinding) -> str:
    """LEAD window function — mirror of LAG with forward offset."""
    base = b.base_unaggregated or b.base_expression
    parts = _partition_clause(b)
    over = f"PARTITION BY {parts} ORDER BY {b.fact_date_column}" if parts \
        else f"ORDER BY {b.fact_date_column}"
    return f"LEAD({base}) OVER ({over})"


def _h_cagr(b: VariantBinding) -> str:
    """Compound Annual Growth Rate: (end/start)^(1/years) - 1.

    Requires n to be set to the number of years.
    Uses the base expression as the end value and LAG(n_periods) as start.
    """
    base = b.base_unaggregated or b.base_expression
    years = b.n if b.n is not None else 1
    parts = _partition_clause(b)
    over = f"PARTITION BY {parts} ORDER BY {b.fact_date_column}" if parts \
        else f"ORDER BY {b.fact_date_column}"
    start_val = f"LAG({base}, {years}) OVER ({over})"
    return (
        f"CASE WHEN {start_val} IS NULL OR {start_val} = 0 THEN NULL "
        f"ELSE POWER({base} * 1.0 / NULLIF({start_val}, 0), "
        f"1.0 / {years}) - 1 END"
    )


def _h_pct_change(b: VariantBinding) -> str:
    """Percent change from prior period: (current - prior) / prior."""
    base = b.base_unaggregated or b.base_expression
    parts = _partition_clause(b)
    over = f"PARTITION BY {parts} ORDER BY {b.fact_date_column}" if parts \
        else f"ORDER BY {b.fact_date_column}"
    prior_val = f"LAG({base}, 1) OVER ({over})"
    return (
        f"CASE WHEN {prior_val} IS NULL OR {prior_val} = 0 THEN NULL "
        f"ELSE ({base} - {prior_val}) * 1.0 / NULLIF({prior_val}, 0) END"
    )


_HANDLERS: dict[str, callable] = {
    "lag": _h_lag,
    "prior_year": _h_prior_year,
    "prior_quarter": _h_prior_quarter,
    "prior_month": _h_prior_month,
    "prior_week": _h_prior_week,
    "ytd": _h_ytd,
    "qtd": _h_qtd,
    "mtd": _h_mtd,
    "wtd": _h_wtd,
    "ytd_prior_year": _h_ytd_prior_year,
    "yoy_growth": _h_yoy_growth,
    "yoy_growth_pct": _h_yoy_growth_pct,
    "trailing_n": _h_trailing_n,
    "moving_avg_n": _h_moving_avg_n,
    "last_n_periods": _h_trailing_n,
    "period_to_date": _h_ytd,
    "same_period_last_year": _h_prior_year,
    "lead": _h_lead,
    "cagr": _h_cagr,
    "pct_change": _h_pct_change,
}


# ---------------------------------------------------------------------------
# PostgreSQL dialect compatibility helper
# ---------------------------------------------------------------------------

import re as _re

_IGNORE_NULLS_PATTERN = _re.compile(
    r"LAST_VALUE\((.+?) IGNORE NULLS\)"
    r"\s+OVER\s*\(\s*ORDER BY\s+(\S+)"
    r"\s+ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING\s*\)"
)


def rewrite_ignore_nulls_for_postgresql(sql: str) -> str:
    """Rewrite ANSI ``LAST_VALUE(... IGNORE NULLS)`` to PostgreSQL-compatible form.

    PostgreSQL < 16 does not support ``IGNORE NULLS``. This helper converts
    the ANSI pattern to the equivalent ``ARRAY_AGG ... FILTER ... [1]``
    window function pattern that achieves the same semantics.

    This is the canonical PostgreSQL compatibility transform for carry-forward
    NULL fill expressions. All callers that need PostgreSQL-compatible
    carry-forward SQL should use this helper rather than local branching.
    """
    def _pg_rewrite(m: _re.Match) -> str:
        expr = m.group(1)
        time_col = m.group(2)
        return (
            f"(ARRAY_AGG({expr} ORDER BY {time_col} DESC)"
            f" FILTER (WHERE {expr} IS NOT NULL)"
            f" OVER (ORDER BY {time_col}"
            f" ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING))[1]"
        )

    return _IGNORE_NULLS_PATTERN.sub(_pg_rewrite, sql)
