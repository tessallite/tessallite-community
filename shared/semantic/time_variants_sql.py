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
from typing import Any, Iterable, Optional

import sqlglot
from sqlglot.dialects.bigquery import BigQuery as _BigQueryDialect
from sqlglot.dialects.spark import Spark as _SparkDialect

from shared.connector_qualify import safe_ident

from ..schemas.measure_formats import (
    TIME_VARIANT_DEFAULT_MOVING_AVG_N,
    TIME_VARIANT_DEFAULT_TRAILING_N,
    TIME_VARIANT_NAMES,
    TIME_VARIANTS_NEEDING_CALENDAR,
    is_window_variant,
)
from .calendar_dialects import TABLE_BOUND_CALENDAR_TYPES


# ---------------------------------------------------------------------------
# Register missing sqlglot dialect mappings (sqlglot 30.x gaps)
# ---------------------------------------------------------------------------
# Bug-8328 [process-global dialect corruption — do NOT mutate in place].
# In sqlglot 30.8.0 neither BigQuery nor Spark overrides ``DATE_PART_MAPPING``:
# ``BigQuery.DATE_PART_MAPPING is Spark.DATE_PART_MAPPING is
# Dialect.DATE_PART_MAPPING`` — one dict object shared by every dialect that
# does not define its own (postgres, redshift, duckdb, ... all included).
# Writing a key into it therefore reconfigures the whole library for the life
# of the process, and the ``if key not in mapping`` guards then read the OTHER
# dialect's key and skip the registration they were meant to perform. Measured
# consequences of the previous in-place form, on this workspace's pinned
# sqlglot 30.8.0:
#   * Spark never received ``DOW_ISO``: it saw BigQuery's ``ISODOW->DAYOFWEEK``
#     already present and emitted ``EXTRACT(DAYOFWEEK ...)`` — Sunday=1..7
#     instead of the intended Monday=1..7 (a WRONG weekday bucket, not just a
#     relabelled one);
#   * BigQuery inherited Spark's ``ISOYEAR->YEAROFWEEK`` and emitted
#     ``EXTRACT(YEAROFWEEK ...)``, which BigQuery does not accept, breaking an
#     ISOYEAR rendering that was previously correct;
#   * unrelated dialects (duckdb, and any other sharing the base dict) were
#     silently reconfigured by importing this module.
# The fix is to give each dialect class its OWN copied mapping before
# registering, which leaves the shared base dict untouched, and to register
# unconditionally (the presence guards only existed to be polite to a mapping
# we now own outright).
def _register_date_part_mapping(dialect_cls, mapping: dict[str, str]) -> None:
    """Attach a dialect-local ``DATE_PART_MAPPING`` extended with *mapping*.

    Rebinding the class attribute to a NEW dict is what keeps the mutation
    scoped: the base ``Dialect.DATE_PART_MAPPING`` (and therefore every other
    dialect) is never written to.
    """
    dialect_cls.DATE_PART_MAPPING = {**dialect_cls.DATE_PART_MAPPING, **mapping}
    dialect_cls.Generator.NORMALIZE_EXTRACT_DATE_PARTS = True


# PG ISODOW (1=Mon..7=Sun) → BQ DAYOFWEEK (1=Sun..7=Sat).
# Values differ but both assign a unique int per weekday — safe for
# PARTITION BY / GROUP BY which only need equality, not magnitude.
# BigQuery supports ISOYEAR natively, so it is deliberately NOT remapped.
_register_date_part_mapping(_BigQueryDialect, {"ISODOW": "DAYOFWEEK"})
# Spark has no ISOYEAR / ISODOW extract fields; the documented Spark
# equivalents are YEAROFWEEK (ISO week-numbering year) and DOW_ISO
# (1=Mon..7=Sun, value-identical to PG ISODOW). Same registration
# pattern as the BigQuery ISODOW mapping above (B5, F-015-01).
_register_date_part_mapping(
    _SparkDialect, {"ISOYEAR": "YEAROFWEEK", "ISODOW": "DOW_ISO"}
)


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

    time_grain (Bug-6220): the resolved time grain of the query (e.g.
    "day", "month", "quarter", "year").  When the grain is coarser than
    day, day-level position keys in prior-period partition clauses are
    omitted because ``fact_date_column`` is an aggregated value
    (``MIN(date)``) whose day component is data-dependent and varies
    between periods, causing partition mismatches and spurious NULLs.
    None preserves the legacy raw-row behaviour for direct low-level callers;
    source and materialisation resolvers pass the selected grain explicitly so
    row-offset windows receive period-adjacency guards.
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
    time_grain: Optional[str] = None
    aggregate_calendar_columns: bool = False


class VariantSqlError(ValueError):
    """Raised when a variant cannot be rendered in the given context."""


# Keep time-dimension selection identical on the source and aggregate routes.
# Both routes can receive dimensions in persistence/query order, which is not
# a semantic order.  A multi-time-dimension query must choose the finest grain
# first, then use stable metadata tie-breakers; otherwise the same variant can
# order by different physical date columns on the two routes (Bug-8293).
_TIME_GRAIN_RANK: dict[str, int] = {
    "hour": 0,
    "day": 1,
    "week": 2,
    "month": 3,
    "quarter": 4,
    "half": 5,
    "year": 6,
}


@dataclass(frozen=True, slots=True)
class EffectiveVariantAnchor:
    """One source/build/admission decision for a variant's date anchor.

    Window-family variants are configured through
    ``date_dimension_column_id``; parallel/period variants (including CAGR
    and pct_change) use ``resolved_date_col_id``.  When that configured value
    is absent, every route falls back to the selected finest-grain dimension's
    source column.  Materialisation and admission may therefore prove identity
    by comparing ``effective_column_id`` with ``grain_column_id`` without
    re-implementing the field precedence (Bug-8293).

    When neither the configured value nor the selected dimension's source
    column exists, ``effective_column_id`` is None and the anchor is
    UNPROVEN: ``is_proven`` is False and ``matches_grain`` fails closed,
    so admission and CTAS consumers refuse instead of treating two missing
    physical identities as a proven match (Bug-8293).
    """

    grain_column_id: Any
    configured_column_id: Any
    effective_column_id: Any
    configured_field: str

    @property
    def is_proven(self) -> bool:
        """True only when a concrete physical anchor identity was resolved.

        Bug-8293: with no configured anchor AND a selected time dimension
        that exposes no source column, ``effective_column_id`` is None — the
        two missing physical identities carry no proof of anything. Every
        consumer must treat that as UNPROVEN and fail closed (refuse CTAS
        build / aggregate admission; serve from source), never as a match.
        """
        return self.effective_column_id is not None

    @property
    def matches_grain(self) -> bool:
        # Bug-8293 [fail closed]: a missing physical identity on either side
        # is NOT proof of equality. ``None is None`` must never read as a
        # proven match for a supported derived/expression grain.
        if self.grain_column_id is None or self.effective_column_id is None:
            return False
        return str(self.grain_column_id) == str(self.effective_column_id)


def resolve_effective_variant_anchor(
    measure: Any,
    selected_time_dimension: Any,
) -> EffectiveVariantAnchor:
    """Resolve the authoritative physical date-column identity for a variant."""
    kind = getattr(measure, "variant_kind", None)
    if is_window_variant(kind):
        configured_field = "date_dimension_column_id"
    else:
        configured_field = "resolved_date_col_id"
    configured = getattr(measure, configured_field, None)
    grain_column = getattr(selected_time_dimension, "source_column_id", None)
    return EffectiveVariantAnchor(
        grain_column_id=grain_column,
        configured_column_id=configured,
        effective_column_id=(configured if configured is not None else grain_column),
        configured_field=configured_field,
    )


def select_finest_time_dimension(
    dimensions: Iterable[Any], grain_names: set[str],
) -> Any | None:
    """Select the canonical time dimension for a query/aggregate grain.

    The returned dimension is the finest time grain in ``grain_names``. Equal
    grain ranks are resolved by logical name and source-column id so database
    row order cannot change the selected anchor. ``None`` is returned when the
    supplied grain has no time dimension.
    """
    candidates = [
        dimension for dimension in dimensions
        if (
            (getattr(dimension, "is_time_dim", False)
             or getattr(dimension, "dimension_kind", None) == "time")
            and getattr(dimension, "name", None) in grain_names
        )
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda dimension: (
            _TIME_GRAIN_RANK.get(
                (getattr(dimension, "time_grain", None) or "").lower(),
                99,
            ),
            str(getattr(dimension, "name", "")),
            str(getattr(dimension, "source_column_id", "")),
        ),
    )


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
    # DATE_DIFF is the canonical cross-source spelling used by the period
    # adjacency key. SQLGlot lowers it to PostgreSQL date/epoch arithmetic,
    # so this one helper expression must pass through the generator even for
    # the canonical target; other expressions retain the byte-stable path.
    if (
        dialect == "postgresql"
        and "DATE_DIFF(" not in pg_sql
        and "DATEDIFF(" not in pg_sql
    ):
        return pg_sql
    target = "postgres" if dialect == "postgresql" else _SQLGLOT_DIALECT[dialect]
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
    """Return the previous value, refusing to bridge a missing period."""
    base = b.base_unaggregated or b.base_expression
    order_key = _window_order_key(b)
    over = _window_over(b, order_key)
    value = f"LAG({base}) OVER ({over})"
    if not b.time_grain:
        return value
    period_key = _window_period_index(b)
    previous_key = f"LAG({period_key}, 1) OVER ({over})"
    return (
        f"CASE WHEN {previous_key} IS NOT NULL "
        f"AND ({period_key}) - ({previous_key}) = 1 "
        f"THEN {value} ELSE NULL END"
    )


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


def _qual_col(alias: str, column: str) -> str:
    """Qualify a calendar-table column as ``"alias"."column"``.

    SQL rule 2 (quote every identifier through connector_qualify): the alias
    and column names are routed through ``safe_ident`` rather than interpolated
    raw into ``"{...}"."{...}"``. This emitter authors canonical Postgres and
    transpiles downstream, so ``safe_ident`` (the canonical Postgres
    double-quote helper) is the correct quoter — no per-connector branch. For a
    well-formed identifier the output is byte-identical to the raw form; for a
    malformed name (e.g. an embedded double-quote) it escapes safely instead of
    emitting broken/injectable SQL (defense-in-depth).
    """
    return f"{safe_ident(alias)}.{safe_ident(column)}"


def _period_column(b: VariantBinding, key: str) -> str:
    cols = b.calendar_columns or {}
    lookup = _PERIOD_KEY_CALENDAR_LOOKUP.get(key, key)
    if key not in _PERIOD_KEYS_NEVER_FROM_CALENDAR and lookup in cols:
        _track_calendar_key(lookup)
        qualified = _qual_col(b.calendar_alias, cols[lookup])
        # CTAS renders one output row per grain and therefore consumes mapped
        # calendar keys as grouped values.  MIN keeps the mapped key legal in
        # the aggregate SELECT without adding hidden GROUP BY columns; source
        # rewriting leaves this false and tracks the actual key for GROUP BY.
        return f"MIN({qualified})" if b.aggregate_calendar_columns else qualified
    return _extract_period(b, key)


def _extract_period(b: VariantBinding, key: str) -> str:
    """Compute a period boundary expression from the fact date column."""
    d = b.fact_date_column
    cal_type = (b.calendar_type or "standard").lower()
    fy_start = b.fiscal_year_start_month or 1

    if key == "date":
        # The raw fact date column is calendar-agnostic (a physical date), so it
        # is always safe to return verbatim even for a table-bound calendar.
        return d

    # Bug-7809 [safety belt]: table-bound calendars (retail_445, hijri) carry
    # their period math in their own mapped columns (year_column / month_column
    # / period_column / week_column). Any period KEY beyond "date" MUST resolve
    # from that mapping via ``_period_column``; reaching this Gregorian
    # EXTRACT(...) fallback means the CalendarTable mapping is partial (or the
    # calc path bypassed the mapped column), so we would silently emit Gregorian
    # (WRONG) boundaries for ytd/qtd/mtd/wtd/prior_* on a retail/hijri calendar.
    # Fail loud per the F-016-09 doctrine and mirroring the ``_year_position``
    # guard (~L727), instead of returning plausible-looking wrong numbers. Only
    # ``_year_position`` / ``_table_bound_order_key`` know how to build a
    # table-bound composite key; every other consumer must have a mapped column.
    if cal_type in TABLE_BOUND_CALENDAR_TYPES:
        raise VariantSqlError(
            f"Table-bound calendar type {cal_type!r} cannot compute the "
            f"period key {key!r} from Gregorian date math: its CalendarTable "
            f"mapping does not provide the required period column. Map the "
            f"missing period column on the bound calendar (or route the calc "
            f"through the table-bound order key) — a Gregorian fallback would "
            f"return wrong period boundaries."
        )

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


_PERIOD_INDEX_EPOCH = "DATE '1900-01-01'"


def window_period_index_expression(b: VariantBinding) -> sqlglot.exp.Expression:
    """Build the one canonical SQLGlot period-index expression.

    Row-offset windows are only period windows when the emitter knows the
    semantic grain.  Every source and CTAS route uses this AST: day/week/hour
    use an epoch day index, and coarser grains combine the calendar's actual
    year and period numbering.  Fiscal month numbering is derived from the
    calendar month and fiscal start; standard, Hijri and NRF retail calendars
    all have twelve periods.  Missing periods therefore produce a key gap
    instead of silently widening a ``ROWS`` frame (Bug-7184).
    """
    grain = (b.time_grain or "").lower()
    if not grain:
        raise VariantSqlError(
            "period-aware row windows require a resolved time grain"
        )

    day_index = (
        f"DATE_DIFF(CAST({b.fact_date_column} AS DATE), "
        f"{_PERIOD_INDEX_EPOCH}, DAY)"
    )
    expression_sql: str
    if grain == "hour":
        # A day index plus the hour-of-day is portable through SQLGlot and
        # contiguous across midnight without connector-specific TIMESTAMPDIFF
        # spellings. It also avoids the DateDiff-vs-TimestampDiff ambiguity in
        # engines whose DATE_DIFF accepts day units only.
        expression_sql = (
            f"(({day_index}) * 24 + "
            f"EXTRACT(HOUR FROM {b.fact_date_column}))"
        )
    elif grain == "day":
        expression_sql = day_index
    elif grain == "week":
        # 1900-01-01 was a Monday. FLOOR keeps the Monday-based bucket
        # stable for dates before the epoch as well as dates after it.
        expression_sql = f"FLOOR(({day_index}) / 7)"
    elif grain == "month":
        year = _period_column(b, "month_year")
        month = _period_column(b, "month")
        if (b.calendar_type or "standard").lower() == "fiscal":
            fiscal_start = b.fiscal_year_start_month or 1
            period = (
                f"(MOD(({month}) - {fiscal_start} + 12, 12) + 1)"
            )
        else:
            period = month
        # Standard/Gregorian, fiscal, Hijri and NRF 4-5-4 all expose twelve
        # month/period numbers. A 13 scale makes NRF P12 -> next-year P1 jump
        # by two and falsely marks a valid 53-week-year boundary as sparse.
        expression_sql = f"(({year}) * 12 + ({period}))"
    elif grain == "quarter":
        year = _period_column(b, "month_year")
        quarter = _period_column(b, "quarter")
        expression_sql = f"(({year}) * 4 + ({quarter}))"
    elif grain == "half":
        year = _period_column(b, "month_year")
        half = _period_column(b, "half")
        expression_sql = f"(({year}) * 2 + ({half}))"
    elif grain == "year":
        expression_sql = _period_column(b, "year")
    else:
        raise VariantSqlError(f"No period adjacency key for time grain {grain!r}")

    try:
        return sqlglot.parse_one(expression_sql, read="postgres")
    except Exception as exc:  # pragma: no cover - construction is deterministic
        raise VariantSqlError(
            f"Failed to build canonical period index for {grain!r}: {exc}"
        ) from exc


def _window_period_index(b: VariantBinding) -> str:
    """Serialize the canonical period-index AST for the enclosing emitter."""
    return window_period_index_expression(b).sql()


def _window_order_key(b: VariantBinding) -> str:
    """Use the same semantic order key on source and aggregate routes."""
    return _window_period_index(b) if b.time_grain else b.fact_date_column


def _window_over(b: VariantBinding, order_key: str) -> str:
    parts = _partition_clause(b)
    return (
        f"PARTITION BY {parts} ORDER BY {order_key}"
        if parts
        else f"ORDER BY {order_key}"
    )


def _row_window_frame_guard(
    b: VariantBinding, order_key: str, preceding: int,
) -> str:
    """Verify every existing row in a ROWS frame is period-adjacent.

    Checking only ``LAG(key, preceding)`` misses a gap when the frame has
    fewer than ``preceding + 1`` rows.  The oldest key plus the row count
    proves contiguity for both a full frame and an allowed initial partial
    frame, so Jan/Feb/Apr cannot masquerade as a three-period window. ``MIN``
    is used for the oldest key because it transpiles cleanly to targets whose
    window ``FIRST_VALUE`` syntax differs.
    """
    over = _window_over(b, order_key)
    frame = f"ROWS BETWEEN {preceding} PRECEDING AND CURRENT ROW"
    count_rows = f"COUNT({order_key}) OVER ({over} {frame})"
    first_key = f"MIN({order_key}) OVER ({over} {frame})"
    return f"(({order_key}) - ({first_key}) + 1) = ({count_rows})"


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
    # Bug-6220 refinement: at very coarse grains (e.g. prior_year at year
    # grain), all sub-period partition keys may be empty. When no partition
    # columns remain, omit PARTITION BY entirely -- otherwise the SQL would
    # contain ``PARTITION BY  ORDER BY ...`` which is syntactically invalid.
    if all_parts:
        parts_str = ", ".join(all_parts)
        over = (
            f"PARTITION BY {parts_str} "
            f"ORDER BY {ord_key}"
        )
    else:
        over = f"ORDER BY {ord_key}"
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

    Bug-6220 (F-015-25): when ``b.time_grain`` indicates a grain coarser
    than day, the day-level partition key (``EXTRACT(DAY FROM d)``) is
    omitted. At aggregated grains ``d`` is ``MIN(date)`` whose day component
    is data-dependent (first transaction day), not a calendar position.
    Using it as a partition key causes rows for the same month across
    different years to land in different partitions, making ``LAG(1)``
    return NULL instead of the correct prior-period value. The same logic
    drops month-in-quarter keys when the grain is quarter or coarser, and
    month keys when the grain is year.
    """
    d = b.fact_date_column
    grain = (b.time_grain or "day").lower()
    # Grain hierarchy: day < week < month < quarter < half < year
    _GRAIN_RANK = {
        "day": 0, "week": 1, "month": 2, "quarter": 3, "half": 4, "year": 5,
    }
    grain_rank = _GRAIN_RANK.get(grain, 0)

    if unit == "year":
        # Bug-6636: prior_year must partition by the row's POSITION WITHIN THE
        # YEAR at the query grain, so LAG(1) ordered by year lands on the SAME
        # position one year earlier. Previously only day/month/year positions
        # were emitted: at quarter or half grain the month- and day-position
        # keys were pruned (Bug-6220 grain-rank), leaving NO partition at all
        # -- the window degraded to OVER (ORDER BY year) with all four
        # quarters of a year tied on the ordering key, so LAG(1) at the year
        # boundary returned an ARBITRARY prior-year quarter (Q1-2025 picked up
        # Q4-2024's value). Each coarse grain now contributes its own
        # position key so Q1-2025 compares to Q1-2024, H1 to H1, week W to
        # week W.
        parts: list[str] = []
        # Bug-6637: the year ORDER key for non-week grains must be
        # Gregorian/fiscal (month_year), NEVER ISOYEAR. Months, quarters,
        # and halves are Gregorian/fiscal objects; pairing them with the ISO
        # week-numbering year mis-buckets the days straddling Jan 1 (e.g.
        # 2027-01-01 has ISOYEAR 2026 but is Gregorian Q1-2027, so YoY at
        # quarter grain ties Q1-2027 with Q1-2026 and returns NULL/unstable).
        # Week grain keeps week_year (ISOYEAR) because ISO weeks belong to
        # exactly one ISO year.  Year grain (rank 5) has no sub-period
        # position keys so the choice is immaterial, but month_year is still
        # the safer default (avoids any future regression if a sub-key is
        # added).
        year_key = _period_column(b, "month_year")
        if grain_rank <= _GRAIN_RANK["day"]:
            # Day grain: full (month, day) position within the year.
            parts.append(_period_column(b, "month"))
            parts.append(f"EXTRACT(DAY FROM {d})")
        elif grain_rank == _GRAIN_RANK["week"]:
            # Week grain: partition by week-of-year and scope the comparison
            # on the ISO week-numbering year (matching prior_week / wtd), so a
            # week straddling Jan 1 (2024-12-30 = ISO 2025-W01) aligns to the
            # same ISO week one year earlier instead of a neighbouring month.
            parts.append(_period_column(b, "week"))
            year_key = _period_column(b, "week_year")
        elif grain_rank == _GRAIN_RANK["month"]:
            parts.append(_period_column(b, "month"))
        elif grain_rank == _GRAIN_RANK["quarter"]:
            # Quarter-of-year (1..4) position — the key that was missing.
            parts.append(_period_column(b, "quarter"))
        elif grain_rank == _GRAIN_RANK["half"]:
            # Half-of-year (1|2) shares the same defect as quarter.
            parts.append(_period_column(b, "half"))
        # Year grain (rank 5): one row per year, no sub-period key needed --
        # LAG(1) ordered by year finds the prior year directly.
        return (
            parts,
            year_key,
            1,
        )
    if unit == "quarter":
        # Bug-3608: quarters are Gregorian/fiscal objects — scope them on the
        # Gregorian month_year (matching _h_qtd), never the ISO week-numbering
        # year. Pairing ISOYEAR with a Gregorian quarter mis-buckets the days
        # straddling Jan 1 (e.g. 2024-12-30 is ISOYEAR 2025 / Gregorian Q4-2024).
        year_key = _period_column(b, "month_year")
        qtr_key = _period_column(b, "quarter")
        parts = []
        # F-015-03: month-position-within-quarter (0..2) disambiguates the
        # three months of a quarter; without it Jan/Feb/Mar tie on
        # (day, year*4+quarter) at month grain and LAG(1) is
        # nondeterministic. Fiscal-aware via _fact_month_position so the
        # position is relative to the fiscal quarter boundary.
        # Bug-6220: only include at month grain or finer.
        if grain_rank <= _GRAIN_RANK["month"]:
            month_in_quarter = f"MOD({_fact_month_position(b)} - 1, 3)"
            parts.append(month_in_quarter)
        # Day position only meaningful at day grain
        if grain_rank <= _GRAIN_RANK["day"]:
            parts.append(f"EXTRACT(DAY FROM {d})")
        # year*4+quarter: Q4-y = y*4+4, Q1-(y+1) = (y+1)*4+1 — adjacent step 1.
        return (
            parts,
            f"{year_key} * 4 + {qtr_key}",
            1,
        )
    if unit == "month":
        # Bug-3608: months are Gregorian/fiscal objects — scope them on the
        # Gregorian month_year (matching _h_mtd), never the ISO week-numbering
        # year. Pairing ISOYEAR with a Gregorian month mis-buckets the days
        # straddling Jan 1.
        year_key = _period_column(b, "month_year")
        parts = []
        # Day position only meaningful at day grain (Bug-6220)
        if grain_rank <= _GRAIN_RANK["day"]:
            parts.append(f"EXTRACT(DAY FROM {d})")
        # year*12+month: Dec-y = y*12+12, Jan-(y+1) = (y+1)*12+1 — step 1.
        return (
            parts,
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
        parts = []
        # ISODOW position only meaningful at day grain (Bug-6220)
        if grain_rank <= _GRAIN_RANK["day"]:
            parts.append(f"EXTRACT(ISODOW FROM {d})")
        # year*53+week: within a year adjacent weeks step by 1; at the year
        # boundary a 52-week ISO year's W52 = y*53+52 → next year W1 =
        # (y+1)*53+1 steps by 2. A 53-week year's W53 → next W1 steps by 1.
        # So weeks tolerate an adjacency gap up to 2 (F-015-19).
        return (
            parts,
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

# Grain hierarchy: day < week < month < quarter < half < year.
_GRAIN_RANK_POS = {"day": 0, "week": 1, "month": 2, "quarter": 3, "half": 4, "year": 5}


def _table_bound_missing(cal_type: str, need: str, grain: str) -> "VariantSqlError":
    return VariantSqlError(
        f"Table-bound calendar {cal_type!r} is missing the {need} column(s) "
        f"required to compute ytd_prior_year at {grain!r} grain. A "
        f"{cal_type!r} calendar table must expose these period columns; "
        f"computing the position from the bare Gregorian date would return a "
        f"non-monotone ordering key and silently wrong prior-year numbers."
    )


def _table_bound_order_key(b: VariantBinding) -> str:
    """Composite ORDER BY key for a table-bound (retail_445/hijri)
    ytd_prior_year window: ``year * S + within-year position``, both from the
    calendar table's OWN columns (never the bare Gregorian date).

    Monotonicity across the calendar-year boundary (Codex finding). For grains
    COARSER than day the position is calendar-only, so the whole per-row key
    ``year * S + pos`` is wrapped in a SINGLE ``MIN``. Taking
    ``MIN(year) * S + MIN(pos)`` as two separate aggregates is NON-monotone for
    a group that straddles the calendar-year boundary (e.g. a mismatched
    Gregorian month spanning two retail years: MIN(year) and MIN(period) then
    come from different calendar rows, so the key sorts among the wrong year
    and the RANGE frame pulls current-year data into the prior-year window).
    A single ``MIN(year * S + pos)`` is the min of a per-row monotone key over
    time-ordered disjoint groups, so it stays monotone.

    At DAY grain each group is exactly one calendar row, so ``MIN(year)`` and
    the position aggregates come from the same row — the separate-MIN form is
    exact AND carries sub-week (DOW) resolution the coarse grains cannot (there
    is no sub-week calendar column). retail_445 uses the week column
    (Sunday-start; EXTRACT(DOW) is 0=Sun..6=Sat, +1 = 1-based day-in-week);
    hijri uses month + day columns.

    Positions stay within [1, _MAX_POS=403] so the frame constants are
    unchanged: day week*7+dow<=378 (retail) / month*31+day<=402 (hijri);
    week week*7<=371; month period*31<=372; quarter quarter*31<=124; year 31.

    No calendar column is tracked (referenced_calendar_keys stays empty), so
    the rewriter adds nothing to GROUP BY and the requested grain's cardinality
    is preserved. Missing required columns fail loud (never fall back to
    Gregorian math, which would re-create the non-monotone mixed key).
    """
    cal_type = (b.calendar_type or "standard").lower()
    grain = (b.time_grain or "day").lower()
    grain_rank = _GRAIN_RANK_POS.get(grain, 0)
    alias = b.calendar_alias
    cols = b.calendar_columns or {}
    S = _YEAR_KEY_SCALE

    year_col = cols.get("year")
    if not year_col:
        raise _table_bound_missing(cal_type, "year", grain)
    yq = _qual_col(alias, year_col)

    # Year grain: one row per year -> fixed position.
    if grain_rank >= _GRAIN_RANK_POS["year"]:
        return f"(MIN({yq}) * {S} + 31)"

    # Coarse grains (>= week): calendar-only position -> single composite MIN
    # so the key stays monotone for boundary-straddling groups.
    if grain_rank >= _GRAIN_RANK_POS["quarter"]:
        p = cols.get("quarter") or cols.get("month")
        if not p:
            raise _table_bound_missing(cal_type, "quarter or month", grain)
        return f"MIN({yq} * {S} + {_qual_col(alias, p)} * 31)"
    if grain_rank >= _GRAIN_RANK_POS["month"]:
        p = cols.get("month")
        if not p:
            raise _table_bound_missing(cal_type, "month", grain)
        return f"MIN({yq} * {S} + {_qual_col(alias, p)} * 31)"
    if grain_rank >= _GRAIN_RANK_POS["week"]:
        p = cols.get("week")
        if not p:
            raise _table_bound_missing(cal_type, "week", grain)
        return f"MIN({yq} * {S} + {_qual_col(alias, p)} * 7)"

    # Day grain: one calendar row per group -> separate MINs are exact and add
    # sub-week (DOW) resolution.
    d = b.fact_date_column
    wk = cols.get("week")
    if wk:
        pos = f"MIN({_qual_col(alias, wk)}) * 7 + (EXTRACT(DOW FROM {d}) + 1)"
    else:
        mo, day = cols.get("month"), cols.get("day")
        if not (mo and day):
            raise _table_bound_missing(cal_type, "week, or month and day", grain)
        pos = f"MIN({_qual_col(alias, mo)}) * 31 + MIN({_qual_col(alias, day)})"
    return f"(MIN({yq}) * {S} + ({pos}))"


def _year_position(b: VariantBinding) -> str:
    """Monotone, year-independent position of a row within its year.

    For expression-capable calendars the position is derived from the fact
    date only (grain-safe -- no calendar-table columns, which would add
    GROUP BY entries and could explode the query grain). ``month_pos * 31 +
    day`` is strictly increasing within a year and identical across years
    for the same (month, day), so "same position, prior year" aligns
    calendar dates (DAX SAMEPERIODLASTYEAR semantics). For ISO calendars the
    position is ``iso_week * 7 + iso_dow`` so alignment follows the ISO week
    grid. Table-bound calendars (retail_445, hijri) do NOT reach this
    function: their whole composite key (year AND position, from the calendar
    table's own columns) is built by ``_table_bound_order_key``, which
    ``_h_ytd_prior_year`` calls directly for those types. This function only
    serves expression-capable calendars (standard/fiscal/iso/thai_buddhist).

    Bug-6220: at aggregated grains (month, quarter, year), the day
    component of ``fact_date_column`` is data-dependent (``MIN(date)``
    gives the first transaction day, not a calendar position). Omitting
    the day component at coarser grains keeps the position stable across
    years for the same period, so the RANGE frame correctly captures all
    prior-year rows up to the same period. The frame algebra still holds
    because the resulting positions remain within [1, _MAX_POS]:
      month grain:   month_pos * 31        in [31, 372]
      quarter grain: quarter_pos * 31      in [31, 124]
      year grain:    31 (fixed)            = 31
    """
    d = b.fact_date_column
    cal_type = (b.calendar_type or "standard").lower()
    grain = (b.time_grain or "day").lower()

    # Bug-6682 [safety belt]: table-bound calendars (retail_445, hijri) must
    # NEVER reach this function -- their composite key (year + position) uses
    # calendar-table columns via _table_bound_order_key. If a table-bound
    # type arrives here (e.g. because of a caller bug in the routing), fail
    # loud: computing Gregorian position for a retail/hijri year key produces
    # a non-monotone ORDER BY key and ~96% undercount on boundary months.
    if cal_type in TABLE_BOUND_CALENDAR_TYPES:
        raise VariantSqlError(
            f"Table-bound calendar type {cal_type!r} cannot use Gregorian "
            f"position math (_year_position). Route through "
            f"_table_bound_order_key instead."
        )
    _GRAIN_RANK = {
        "day": 0, "week": 1, "month": 2, "quarter": 3, "half": 4, "year": 5,
    }
    grain_rank = _GRAIN_RANK.get(grain, 0)

    # Year grain: one row per year, so use a fixed position. The RANGE
    # frame [K - 1403, K - 1000] always captures exactly the prior year's
    # rows when every year has the same position (31).
    if grain_rank >= _GRAIN_RANK["year"]:
        return "31"

    if cal_type in _ISO_CALENDAR_TYPES:
        if grain_rank >= _GRAIN_RANK["month"]:
            # At month grain or coarser, use week * 7 without ISODOW
            return f"(EXTRACT(WEEK FROM {d}) * 7)"
        return f"(EXTRACT(WEEK FROM {d}) * 7 + EXTRACT(ISODOW FROM {d}))"

    if grain_rank >= _GRAIN_RANK["quarter"]:
        # At quarter grain or coarser, use quarter position only
        if cal_type == "fiscal" and (b.fiscal_year_start_month or 1) != 1:
            fy_start = b.fiscal_year_start_month or 1
            fiscal_qtr = (
                f"FLOOR(MOD(EXTRACT(MONTH FROM {d}) "
                f"- {fy_start} + 12, 12) / 3) + 1"
            )
            return f"({fiscal_qtr} * 31)"
        return f"(EXTRACT(QUARTER FROM {d}) * 31)"

    if grain_rank >= _GRAIN_RANK["month"]:
        # At month grain, use month position only (no day)
        return f"({_fact_month_position(b)} * 31)"

    # Day grain: full resolution
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
    cal_type = (b.calendar_type or "standard").lower()
    if cal_type in TABLE_BOUND_CALENDAR_TYPES:
        # Codex findings (grain split + boundary monotonicity): a table-bound
        # calendar's year AND position both come from its own columns. Build
        # the whole composite key in one place so it can be a single MIN at
        # coarse grains (monotone across the calendar-year boundary) and the
        # exact day-grain form otherwise. No calendar column is tracked, so the
        # rewriter adds nothing to GROUP BY and the requested grain is
        # preserved. See _table_bound_order_key.
        order_key = _table_bound_order_key(b)
    else:
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
    # Bug-5673: ROWS BETWEEN {n} PRECEDING AND CURRENT ROW yields n+1 rows
    # (the current row plus the n preceding ones). To get exactly n rows in
    # the window (matching BI semantics for "trailing n periods"), use
    # {n-1} PRECEDING so the frame includes the current row and n-1 before it.
    #
    # Bug-7184: ROWS remains the physical frame, but when the resolved semantic
    # grain is available its contents must be contiguous in the period key.
    # This preserves the existing partial initial window while returning NULL
    # for a frame that bridges an absent period.
    base = b.base_unaggregated or b.base_expression
    order_key = _window_order_key(b)
    over = _window_over(b, order_key)
    preceding = max(n - 1, 0)
    window = (
        f"{agg}({base}) OVER ("
        f"{over} "
        f"ROWS BETWEEN {preceding} PRECEDING AND CURRENT ROW)"
    )
    if not b.time_grain:
        return window
    guard = _row_window_frame_guard(b, order_key, preceding)
    return f"CASE WHEN {guard} THEN {window} ELSE NULL END"


def _h_trailing_n(b: VariantBinding) -> str:
    n = b.n if b.n is not None else TIME_VARIANT_DEFAULT_TRAILING_N
    return _rolling_window(b, "SUM", n)


def _h_moving_avg_n(b: VariantBinding) -> str:
    n = b.n if b.n is not None else TIME_VARIANT_DEFAULT_MOVING_AVG_N
    return _rolling_window(b, "AVG", n)


def _h_lead(b: VariantBinding) -> str:
    """LEAD window function — mirror of LAG with forward offset."""
    base = b.base_unaggregated or b.base_expression
    order_key = _window_order_key(b)
    over = _window_over(b, order_key)
    return f"LEAD({base}) OVER ({over})"


def _h_cagr(b: VariantBinding) -> str:
    """Compound Annual Growth Rate: (end/start)^(1/years) - 1.

    Requires n to be set to the number of years.
    Uses the base expression as the end value and LAG(n_periods) as start.

    Bug-6645 [correctness]: the LAG offset must be grain-aware. At year
    grain there is one row per year, so ``LAG(base, n_years)`` steps back
    ``n_years`` years. At coarser sub-year grains, the offset must be
    multiplied by the number of periods per year (quarter: 4, month: 12).
    Below month grain (week / day) the periods per year are not constant
    (52/53 weeks, 365/366 days), so CAGR is not supported and raises a
    VariantSqlError.
    """
    base = b.base_unaggregated or b.base_expression
    years = b.n if b.n is not None else 1
    grain = (b.time_grain or "day").lower()

    _GRAIN_PERIODS_PER_YEAR = {
        "year": 1,
        "half": 2,
        "quarter": 4,
        "month": 12,
    }
    periods_per_year = _GRAIN_PERIODS_PER_YEAR.get(grain)
    if periods_per_year is None:
        raise VariantSqlError(
            f"CAGR is not supported at {grain!r} grain. The number of "
            f"{grain}s per year is not constant, so a fixed LAG offset "
            f"cannot reliably step back whole years. Use month, quarter, "
            f"half, or year grain instead."
        )
    lag_offset = years * periods_per_year

    order_key = _window_order_key(b)
    over = _window_over(b, order_key)
    start_val = f"LAG({base}, {lag_offset}) OVER ({over})"
    # Bug-5674: POWER(negative, fraction) is undefined in SQL and raises a
    # domain error. Guard against negative start AND negative end values:
    # start <= 0 is obvious; end < 0 with start > 0 produces a negative
    # ratio which also cannot be raised to a fractional power. end = 0 is
    # fine (POWER(0, frac) = 0 → CAGR = -1, i.e. 100% decline).
    return (
        f"CASE WHEN {start_val} IS NULL OR {start_val} <= 0 "
        f"OR {base} < 0 THEN NULL "
        f"ELSE POWER({base} * 1.0 / {start_val}, "
        f"1.0 / {years}) - 1 END"
    )


def _h_pct_change(b: VariantBinding) -> str:
    """Percent change from the prior row: (current - prior) / prior.

    Bug-6646 [correctness/semantics]: unlike the ``prior_*`` variants which
    partition by a composite period key and guard against gap-bridging
    (F-015-19), ``pct_change`` is a **row-based** comparison: ``LAG(1)``
    always returns the immediately preceding *existing* row (ordered by the
    fact date column).  On gapped data this means comparing to the last row
    that has data, which may be several periods earlier.

    This is by design -- ``pct_change`` has no inherent period unit (year /
    quarter / month / week) and therefore cannot define a composite period
    key to guard adjacency against.  Users who need period-aligned change
    detection with NULL-on-gap semantics should use the typed variants:
    ``prior_month`` + a ratio expression, ``yoy_growth_pct``, etc.

    To prevent silent confusion the LAG is wrapped with an ``IS NOT NULL``
    guard so a partition boundary (no prior row at all) returns NULL rather
    than an undefined ratio.
    """
    base = b.base_unaggregated or b.base_expression
    order_key = _window_order_key(b)
    over = _window_over(b, order_key)
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

# Bug-6653: the regex must match both the partition-less and the
# partitioned / composite-ORDER-BY forms of the LAST_VALUE(... IGNORE NULLS)
# window expression.  The original pattern only matched the simple
# ``OVER ( ORDER BY <single_token> ROWS ...)`` shape; a partitioned or
# composite-ORDER carry-forward emission passed through un-rewritten and
# failed on PostgreSQL < 16.
#
# Group layout:
#   1 — the LAST_VALUE expression argument (non-greedy, up to IGNORE NULLS)
#   2 — optional PARTITION BY clause (including the keyword itself)
#   3 — the ORDER BY column/expression(s) (everything between ORDER BY and
#        the ROWS keyword, trimmed by the replacement function)
_IGNORE_NULLS_PATTERN = _re.compile(
    r"LAST_VALUE\((.+?) IGNORE NULLS\)"
    r"\s+OVER\s*\("
    r"\s*(PARTITION BY\s+.+?)?"       # optional PARTITION BY clause
    r"\s*ORDER BY\s+(.+?)"            # ORDER BY with composite expressions
    r"\s+ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING"
    r"\s*\)"
)


class _OrderReversalUnsafe(Exception):
    """Raised when ``_reverse_order_keys`` cannot safely reverse the ORDER BY.

    The caller must NOT apply the ARRAY_AGG ignore-nulls rewrite with an
    unreversed inner order -- that would silently select the WRONG
    carry-forward row (wrong number). The caller should skip the rewrite
    and keep the original LAST_VALUE expression.
    """


def _reverse_order_keys(order_by_text: str) -> str:
    """Reverse the sort direction of each ordering term in an ORDER BY clause.

    Bug-6901: the ARRAY_AGG carry-forward rewrite needs each ORDER BY column
    in the REVERSED direction of the outer OVER clause (so element [1] of
    the descending-ordered aggregate is the most-recent non-null value in the
    ascending window frame, and vice versa).

    Uses sqlglot to parse the ORDER BY terms so function arguments containing
    commas (e.g. ``COALESCE(a, b) ASC``) are not mis-split, and NULLS
    FIRST/LAST is correctly swapped alongside the direction reversal.

    Raises ``_OrderReversalUnsafe`` if the ORDER BY cannot be parsed or
    contains constructs that cannot be safely reversed (e.g. ``USING >``
    operator-based ordering). The caller must catch this and skip the
    ignore-nulls rewrite rather than emitting a wrong windowed value.
    """
    import sqlglot
    from sqlglot import exp

    # Check if the original text contains explicit NULLS keywords.
    _has_explicit_nulls = bool(
        _re.search(r'\bNULLS\s+(FIRST|LAST)\b', order_by_text, _re.IGNORECASE)
    )

    # Fail closed on USING operator-based ordering (PostgreSQL-specific,
    # not representable in the sqlglot AST's Ordered.desc boolean).
    if _re.search(r'\bUSING\b', order_by_text, _re.IGNORECASE):
        raise _OrderReversalUnsafe(
            f"ORDER BY contains USING operator: {order_by_text!r}"
        )

    # Wrap in a minimal SELECT so sqlglot can parse the ORDER BY.
    stub = f"SELECT 1 ORDER BY {order_by_text}"
    try:
        tree = sqlglot.parse_one(stub, read="postgres")
    except Exception as exc:
        raise _OrderReversalUnsafe(
            f"Cannot parse ORDER BY for reversal: {order_by_text!r}"
        ) from exc

    order = tree.find(exp.Order)
    if not order or not order.expressions:
        raise _OrderReversalUnsafe(
            f"No ordering terms found in: {order_by_text!r}"
        )

    reversed_parts: list[str] = []
    for ordered in order.expressions:
        # Reverse direction: ASC (or default ASC) -> DESC, DESC -> ASC.
        is_desc = ordered.args.get("desc")
        new_desc = not is_desc
        # Render the expression (column/function) without direction.
        col_sql = ordered.this.sql(dialect="postgres")
        direction = "DESC" if new_desc else "ASC"
        # Only swap NULLS FIRST/LAST when the original text had explicit
        # NULLS keywords (sqlglot cannot distinguish implicit from explicit).
        if _has_explicit_nulls:
            nulls_first = ordered.args.get("nulls_first")
            if nulls_first is True:
                reversed_parts.append(f"{col_sql} {direction} NULLS LAST")
            elif nulls_first is False:
                reversed_parts.append(f"{col_sql} {direction} NULLS FIRST")
            else:
                reversed_parts.append(f"{col_sql} {direction}")
        else:
            reversed_parts.append(f"{col_sql} {direction}")

    return ", ".join(reversed_parts)


def rewrite_ignore_nulls_for_postgresql(sql: str) -> str:
    """Rewrite ANSI ``LAST_VALUE(... IGNORE NULLS)`` to PostgreSQL-compatible form.

    PostgreSQL < 16 does not support ``IGNORE NULLS``. This helper converts
    the ANSI pattern to the equivalent ``ARRAY_AGG ... FILTER ... [1]``
    window function pattern that achieves the same semantics.

    Bug-6653: handles both the simple (partition-less) form and the
    partitioned / composite-ORDER-BY form.  A ``PARTITION BY`` clause, when
    present, is preserved on the rewritten ``ARRAY_AGG`` window so the
    carry-forward scoping is identical to the original expression.

    Bug-6901: the original rewrite unconditionally appended ``DESC`` to the
    ARRAY_AGG ORDER BY, which (a) doubled the direction token when the
    original already had ``DESC`` (syntax error), and (b) for a
    comma-separated ORDER BY list, only reversed the LAST item (wrong
    carry-forward value).  Now each ORDER BY key is individually reversed.

    This is the canonical PostgreSQL compatibility transform for carry-forward
    NULL fill expressions. All callers that need PostgreSQL-compatible
    carry-forward SQL should use this helper rather than local branching.
    """
    def _pg_rewrite(m: _re.Match) -> str:
        expr = m.group(1)
        partition_clause = (m.group(2) or "").strip()
        time_col = m.group(3).strip()
        partition_prefix = f"{partition_clause} " if partition_clause else ""
        try:
            reversed_order = _reverse_order_keys(time_col)
        except _OrderReversalUnsafe:
            # Bug-6901 fail-closed: if the ORDER BY cannot be safely reversed,
            # skip the ARRAY_AGG rewrite entirely and keep the original
            # LAST_VALUE(... IGNORE NULLS) expression. PostgreSQL >= 16
            # supports IGNORE NULLS natively; on < 16 this will error at
            # execution time rather than silently returning the wrong
            # carry-forward row with an unreversed inner order.
            return m.group(0)
        return (
            f"(ARRAY_AGG({expr} ORDER BY {reversed_order})"
            f" FILTER (WHERE {expr} IS NOT NULL)"
            f" OVER ({partition_prefix}ORDER BY {time_col}"
            f" ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING))[1]"
        )

    return _IGNORE_NULLS_PATTERN.sub(_pg_rewrite, sql)
