"""Dialect-specific DDL emitters for calendar tables.

Column expressions are written once in canonical PostgreSQL and
transpiled to the target dialect via sqlglot.  Only the date-
generation source clause and DDL wrapper use per-dialect templates.

Calendar types:
- standard / fiscal: year, half, quarter, month, week, day
- iso_week: iso_year, iso_week, iso_day_of_week
- retail_445: NRF 4-5-4 retail calendar
- thai_buddhist: Thai solar calendar (Gregorian + 543 year offset)
- hijri: Islamic calendar (requires hijri-converter package)

Pure functions: no side effects, no DB connection.
"""
from __future__ import annotations

from datetime import date

import sqlglot

from shared.connector_qualify import quote_table_ref

# NOTE: CALENDAR_TYPES is deliberately re-declared below (not imported from
# shared.semantic.calendar_types) and kept in sync by a parity test. Importing
# it here would shadow that local definition.

CALENDAR_DIALECTS = frozenset({"postgresql", "bigquery", "hadoop_spark", "redshift", "snowflake", "sqlserver"})

_CONNECTOR_TO_SQLGLOT: dict[str, str] = {
    "postgresql": "postgres",
    "redshift": "postgres",
    "bigquery": "bigquery",
    "hadoop_spark": "spark",
    "snowflake": "snowflake",
    "sqlserver": "tsql",
}

CALENDAR_TYPES = frozenset({
    "standard", "fiscal", "iso_week", "retail_445",
    "hijri", "thai_buddhist",
})

# Calendar types whose period boundaries (year / half / quarter / month /
# week / day partitions) can be computed directly from the fact date column
# with date arithmetic — no physical calendar table required:
#
#   standard       — Gregorian, ISO week numbering.
#   fiscal         — offset arithmetic from fiscal_year_start_month.
#   iso_week       — ISO 8601 week-numbering year / week / weekday.
#   thai_buddhist  — Gregorian + 543. The year LABEL differs, but the
#                    period PARTITIONING is identical to Gregorian (a
#                    constant offset is a bijection on the year key), so the
#                    expression path produces business-correct buckets.
#
# The remaining types need a materialised calendar table because their
# period boundaries cannot be derived from the Gregorian date with simple
# arithmetic:
#
#   retail_445     — NRF 4-5-4 week/period grid (Sunday nearest Feb 1 anchor).
#   hijri          — lunar calendar (library-computed per-date mapping).
#
# F-016-04 / F-016-09: this is the single source of truth for the
# expression-vs-table decision. Callers (the query-router preflight and the
# variant emitter) import it rather than maintaining parallel literal sets.
EXPRESSION_CAPABLE_CALENDAR_TYPES = frozenset({
    "standard", "fiscal", "iso_week", "thai_buddhist",
})

# Calendar types that REQUIRE a bound calendar table for time-variant
# computation. Period math against the bare fact date would silently return
# Gregorian numbers (F-016-09), so the query path must fail loud when one of
# these is configured but no calendar table is bound.
TABLE_BOUND_CALENDAR_TYPES = CALENDAR_TYPES - EXPRESSION_CAPABLE_CALENDAR_TYPES

STANDARD_COLUMNS = {
    "date_column": "date_key",
    "year_column": "year_no",
    "half_column": "half_no",
    "quarter_column": "quarter_no",
    "month_column": "month_no",
    "week_column": "week_no",
    "day_column": "day_no",
}

ISO_WEEK_COLUMNS = {
    "date_column": "date_key",
    "year_column": "iso_year",
    "week_column": "iso_week",
    "day_column": "iso_day_of_week",
}

RETAIL_445_COLUMNS = {
    "date_column": "date_key",
    "year_column": "retail_year",
    "quarter_column": "retail_quarter",
    "month_column": "retail_period",
    "week_column": "retail_week",
}

THAI_BUDDHIST_COLUMNS = {
    "date_column": "date_key",
    "year_column": "thai_year",
    "half_column": "half_no",
    "quarter_column": "quarter_no",
    "month_column": "month_no",
    "week_column": "week_no",
    "day_column": "day_no",
}

HIJRI_COLUMNS = {
    "date_column": "date_key",
    "year_column": "hijri_year",
    "month_column": "hijri_month",
    "day_column": "hijri_day",
}

CALENDAR_COLUMN_SETS: dict[str, dict[str, str]] = {
    "standard": STANDARD_COLUMNS,
    "fiscal": STANDARD_COLUMNS,
    "iso_week": ISO_WEEK_COLUMNS,
    "retail_445": RETAIL_445_COLUMNS,
    "thai_buddhist": THAI_BUDDHIST_COLUMNS,
    "hijri": HIJRI_COLUMNS,
}


def _transpile_expr(pg_expr: str, dialect: str) -> str:
    target = _CONNECTOR_TO_SQLGLOT.get(dialect, "postgres")
    return sqlglot.transpile(pg_expr, read="postgres", write=target)[0]


class _DialectConfig:
    """Encapsulates dialect-specific DDL patterns that sqlglot cannot transpile."""

    __slots__ = ("ctas_prefix", "supports_pk", "quote_connector", "date_source_fn", "row_table_emitter")

    def __init__(
        self,
        ctas_prefix: str,
        supports_pk: bool,
        date_source_fn: "type[_DateSource]",
        row_table_emitter: "type[_RowTableEmitter]",
        quote_connector: str | None = None,
    ):
        self.ctas_prefix = ctas_prefix
        self.supports_pk = supports_pk
        self.quote_connector = quote_connector
        self.date_source_fn = date_source_fn
        self.row_table_emitter = row_table_emitter

    def ddl_wrapper(self, table_name: str, select_sql: str, pk_col: str | None = "date_key") -> str:
        ddl = f"DROP TABLE IF EXISTS {table_name};\n{self.ctas_prefix.format(table=table_name)}\n{select_sql};\n"
        if pk_col and self.supports_pk:
            ddl += f"ALTER TABLE {table_name} ADD PRIMARY KEY ({pk_col});\n"
        return ddl


class _DateSource:
    @staticmethod
    def generate(s: str, e: str) -> tuple[str, str]:
        raise NotImplementedError


class _PgDateSource(_DateSource):
    @staticmethod
    def generate(s: str, e: str) -> tuple[str, str]:
        return "d::date", f"generate_series('{s}'::date, '{e}'::date, '1 day'::interval) d"


class _BqDateSource(_DateSource):
    @staticmethod
    def generate(s: str, e: str) -> tuple[str, str]:
        return "date_key", f"UNNEST(GENERATE_DATE_ARRAY(DATE '{s}', DATE '{e}', INTERVAL 1 DAY)) AS date_key"


class _SparkDateSource(_DateSource):
    @staticmethod
    def generate(s: str, e: str) -> tuple[str, str]:
        return "date_key", f"(\n    SELECT explode(sequence(to_date('{s}'), to_date('{e}'), interval 1 day)) AS date_key\n)"


class _SnowflakeDateSource(_DateSource):
    @staticmethod
    def generate(s: str, e: str) -> tuple[str, str]:
        return "date_key", (
            f"(\n    SELECT DATEADD(DAY, seq4(), DATE '{s}') AS date_key\n"
            f"    FROM TABLE(GENERATOR(ROWCOUNT => DATEDIFF(DAY, DATE '{s}', DATE '{e}') + 1))\n)"
        )


class _SqlServerDateSource(_DateSource):
    # F-016-20: GENERATE_SERIES is SQL Server 2022+ (compatibility level 160).
    # On older servers this DDL fails; the returned script carries the note so
    # a modeller running it manually knows the minimum version.
    SQL_SERVER_VERSION_NOTE = (
        "-- Requires SQL Server 2022+ (compatibility level 160) for "
        "GENERATE_SERIES.\n"
    )

    @staticmethod
    def generate(s: str, e: str) -> tuple[str, str]:
        return "date_key", (
            f"(\n    SELECT DATEADD(DAY, value, CAST('{s}' AS DATE)) AS date_key\n"
            f"    FROM GENERATE_SERIES(0, DATEDIFF(DAY, '{s}', '{e}'))\n)"
        )


# Row-materialised calendars (hijri, retail_445): the per-date period math is
# either library-computed (hijri) or follows the NRF 4-5-4 week grid
# (retail_445) — neither can be faithfully transpiled to every dialect via
# sqlglot, so the values are computed in Python and emitted as table rows.
# This is the same sanctioned per-dialect carve-out the date-source clauses
# use. A row is ``(date_iso, [int, ...])`` aligned to ``int_columns``.

RowTableRow = tuple[str, list[int]]


class _RowTableEmitter:
    """Emit ``DROP``+``CREATE``+populate DDL for a date_key + N integer columns
    table from pre-computed Python rows. One subclass per dialect family."""

    @staticmethod
    def emit(table_name: str, int_columns: list[str], rows: list[RowTableRow]) -> str:
        raise NotImplementedError


def _row_values_literal(row: RowTableRow) -> str:
    date_iso, ints = row
    return "(" + ", ".join([f"'{date_iso}'", *[str(i) for i in ints]]) + ")"


class _PgRowTableEmitter(_RowTableEmitter):
    @staticmethod
    def emit(table_name: str, int_columns: list[str], rows: list[RowTableRow]) -> str:
        col_defs = ",\n    ".join(
            ["date_key DATE PRIMARY KEY", *[f"{c} INT NOT NULL" for c in int_columns]]
        )
        col_list = ", ".join(["date_key", *int_columns])
        values = ",\n    ".join(_row_values_literal(r) for r in rows)
        return (
            f"DROP TABLE IF EXISTS {table_name};\n"
            f"CREATE TABLE {table_name} (\n"
            f"    {col_defs}\n"
            f");\n"
            f"INSERT INTO {table_name} ({col_list}) VALUES\n"
            f"    {values};\n"
        )


class _BqRowTableEmitter(_RowTableEmitter):
    @staticmethod
    def emit(table_name: str, int_columns: list[str], rows: list[RowTableRow]) -> str:
        def _select(row: RowTableRow) -> str:
            date_iso, ints = row
            parts = [f"DATE '{date_iso}' AS date_key"]
            parts += [f"{val} AS {col}" for col, val in zip(int_columns, ints)]
            return "SELECT " + ", ".join(parts)

        selects = " UNION ALL\n    ".join(_select(r) for r in rows)
        return (
            f"DROP TABLE IF EXISTS {table_name};\n"
            f"CREATE TABLE {table_name} AS\n"
            f"    {selects};\n"
        )


class _SparkRowTableEmitter(_RowTableEmitter):
    @staticmethod
    def emit(table_name: str, int_columns: list[str], rows: list[RowTableRow]) -> str:
        col_list = ", ".join(["date_key", *int_columns])
        values = ",\n    ".join(_row_values_literal(r) for r in rows)
        return (
            f"DROP TABLE IF EXISTS {table_name};\n"
            f"CREATE TABLE {table_name} USING parquet AS\n"
            f"SELECT {col_list} FROM VALUES\n"
            f"    {values}\n"
            f"AS t({col_list});\n"
        )


_DIALECT_CONFIGS: dict[str, _DialectConfig] = {
    "postgresql": _DialectConfig("CREATE TABLE {table} AS", True, _PgDateSource, _PgRowTableEmitter),
    "redshift": _DialectConfig("CREATE TABLE {table} AS", True, _PgDateSource, _PgRowTableEmitter),
    "bigquery": _DialectConfig("CREATE TABLE {table} AS", False, _BqDateSource, _BqRowTableEmitter, quote_connector="bigquery"),
    "hadoop_spark": _DialectConfig("CREATE TABLE {table} USING parquet AS", False, _SparkDateSource, _SparkRowTableEmitter),
    "snowflake": _DialectConfig("CREATE TABLE {table} AS", False, _SnowflakeDateSource, _PgRowTableEmitter),
    "sqlserver": _DialectConfig("CREATE TABLE {table} AS", False, _SqlServerDateSource, _PgRowTableEmitter),
}


def _get_dialect_config(dialect: str) -> _DialectConfig:
    config = _DIALECT_CONFIGS.get(dialect)
    if not config:
        raise ValueError(f"Unsupported dialect: {dialect}")
    return config


def _date_source(dialect: str, start: date, end: date) -> tuple[str, str]:
    s, e = start.isoformat(), end.isoformat()
    return _get_dialect_config(dialect).date_source_fn.generate(s, e)


def _ddl_wrapper(dialect: str, table_name: str, select_sql: str, pk_col: str | None = "date_key") -> str:
    return _get_dialect_config(dialect).ddl_wrapper(table_name, select_sql, pk_col)


def emit_calendar_ddl(
    dialect: str,
    table_name: str,
    start_date: date,
    end_date: date,
    fiscal_year_start_month: int = 1,
    calendar_type: str = "standard",
) -> str:
    if end_date <= start_date:
        raise ValueError("end_date must be after start_date")
    if calendar_type not in CALENDAR_TYPES:
        raise ValueError(f"Unsupported calendar_type: {calendar_type}")

    if calendar_type in ("standard", "fiscal"):
        if not 1 <= fiscal_year_start_month <= 12:
            raise ValueError("fiscal_year_start_month must be between 1 and 12")
        fys = fiscal_year_start_month
    else:
        fys = 1

    config = _get_dialect_config(dialect)
    if config.quote_connector:
        table_name = quote_table_ref(config.quote_connector, table_name)

    emitters = {
        "standard": _emit_standard,
        "fiscal": _emit_standard,
        "iso_week": _emit_iso_week,
        "retail_445": _emit_retail_445,
        "thai_buddhist": _emit_thai_buddhist,
        "hijri": _emit_hijri,
    }
    return emitters[calendar_type](dialect, table_name, start_date, end_date, fys)


def _emit_standard(
    dialect: str, table_name: str, start: date, end: date, fys: int
) -> str:
    date_alias, from_clause = _date_source(dialect, start, end)

    if fys == 1:
        year_pg = f"EXTRACT(YEAR FROM {date_alias})::int"
        half_pg = f"CASE WHEN EXTRACT(MONTH FROM {date_alias}) <= 6 THEN 1 ELSE 2 END"
        quarter_pg = f"EXTRACT(QUARTER FROM {date_alias})::int"
    else:
        year_pg = (
            f"CASE WHEN EXTRACT(MONTH FROM {date_alias})::int >= {fys} "
            f"THEN EXTRACT(YEAR FROM {date_alias})::int "
            f"ELSE EXTRACT(YEAR FROM {date_alias})::int - 1 END"
        )
        half_pg = (
            f"CASE WHEN ((EXTRACT(MONTH FROM {date_alias})::int - {fys} + 12) % 12) < 6 THEN 1 ELSE 2 END"
        )
        quarter_pg = (
            f"(((EXTRACT(MONTH FROM {date_alias})::int - {fys} + 12) % 12) / 3 + 1)::int"
        )
    # F-016-16: month_no is the CALENDAR month (1-12) for both standard and
    # fiscal calendars — the fiscal offset is applied to year/half/quarter but
    # not to the month column. A fiscal period number, when needed, is
    # ``((month_no - fys + 12) % 12) + 1`` derived on demand; the table does not
    # store a separate fiscal-period column. Documented in
    # architecture_multi-calendar.md.
    month_pg = f"EXTRACT(MONTH FROM {date_alias})::int"
    week_pg = f"EXTRACT(WEEK FROM {date_alias})::int"
    day_pg = f"EXTRACT(DAY FROM {date_alias})::int"

    columns = [
        (f"{date_alias}", "date_key"),
        (_transpile_expr(year_pg, dialect), "year_no"),
        (_transpile_expr(half_pg, dialect), "half_no"),
        (_transpile_expr(quarter_pg, dialect), "quarter_no"),
        (_transpile_expr(month_pg, dialect), "month_no"),
        (_transpile_expr(week_pg, dialect), "week_no"),
        (_transpile_expr(day_pg, dialect), "day_no"),
    ]
    col_sql = ",\n    ".join(f"{expr} AS {alias}" for expr, alias in columns)
    select_sql = f"SELECT\n    {col_sql}\nFROM {from_clause}"
    return _ddl_wrapper(dialect, table_name, select_sql)


def _emit_iso_week(
    dialect: str, table_name: str, start: date, end: date, _fys: int
) -> str:
    date_alias, from_clause = _date_source(dialect, start, end)

    year_pg = f"EXTRACT(ISOYEAR FROM {date_alias})::int"
    week_pg = f"EXTRACT(WEEK FROM {date_alias})::int"
    dow_pg = f"EXTRACT(ISODOW FROM {date_alias})::int"

    columns = [
        (f"{date_alias}", "date_key"),
        (_transpile_expr(year_pg, dialect), "iso_year"),
        (_transpile_expr(week_pg, dialect), "iso_week"),
        (_transpile_expr(dow_pg, dialect), "iso_day_of_week"),
    ]
    col_sql = ",\n    ".join(f"{expr} AS {alias}" for expr, alias in columns)
    select_sql = f"SELECT\n    {col_sql}\nFROM {from_clause}"
    return _ddl_wrapper(dialect, table_name, select_sql)


# NRF 4-5-4 retail calendar columns (in emit order, after date_key).
_RETAIL_445_INT_COLUMNS = ["retail_year", "retail_quarter", "retail_period", "retail_week"]


def _nrf_year_start(cal_year: int) -> date:
    """First day (a Sunday) of the NRF retail year whose anchor is *cal_year*.

    NRF rule: the retail year starts on the Sunday nearest to 1 February. The
    nearest Sunday to Feb 1 is at most three days either side: if Feb 1 falls
    Sun-Wed (DOW 0-3) the retail year started on the preceding Sunday; if it
    falls Thu-Sat (DOW 4-6) it starts on the following Sunday.
    """
    from datetime import timedelta

    anchor = date(cal_year, 2, 1)
    # Python weekday(): Mon=0..Sun=6. Convert to ISO/Postgres DOW Sun=0..Sat=6.
    dow = (anchor.weekday() + 1) % 7
    delta = dow if dow <= 3 else dow - 7
    return anchor - timedelta(days=delta)


def nrf_retail_445_fields(d: date) -> tuple[int, int, int, int]:
    """Return (retail_year, retail_quarter, retail_period, retail_week) for *d*
    under the NRF 4-5-4 retail calendar.

    * retail_year — the NRF year whose Sunday-nearest-Feb-1 start covers *d*.
    * retail_week — 1-based week within the retail year (1..52, or 53 in a
      53-week year).
    * retail_period — 1..12 (one period per 4-5-4 month). Period lengths
      cycle 4-5-4 weeks per quarter; a 53rd week extends the final period
      (period 12) of Q4 rather than creating a spurious 13th period.
    * retail_quarter — 1..4 (periods 1-3 → Q1, 4-6 → Q2, 7-9 → Q3, 10-12 → Q4).
    """
    cal_year = d.year
    start = _nrf_year_start(cal_year)
    if d >= start:
        retail_year = cal_year
        year_start = start
    else:
        retail_year = cal_year - 1
        year_start = _nrf_year_start(cal_year - 1)

    days_into = (d - year_start).days
    week = days_into // 7 + 1  # 1-based week within the retail year

    quarter_idx = min((week - 1) // 13, 3)  # 0..3
    week_in_quarter = (week - 1) - quarter_idx * 13  # 0..12 (or more in week 53)
    if week_in_quarter < 4:
        period_in_quarter = 1
    elif week_in_quarter < 9:
        period_in_quarter = 2
    else:
        period_in_quarter = 3
    period = quarter_idx * 3 + period_in_quarter
    return retail_year, quarter_idx + 1, period, week


def _emit_retail_445(
    dialect: str, table_name: str, start: date, end: date, _fys: int
) -> str:
    """NRF 4-5-4 retail calendar.

    The NRF week/period grid (Sunday-nearest-Feb-1 year anchor, 4-5-4 week
    periods, 52/53-week years) cannot be derived from the Gregorian date with
    arithmetic that transpiles faithfully across every dialect, so the values
    are computed in Python and materialised as table rows — the same approach
    the Hijri calendar uses (F-016-05).
    """
    from datetime import timedelta

    rows: list[RowTableRow] = []
    current = start
    while current <= end:
        ry, rq, rp, rw = nrf_retail_445_fields(current)
        rows.append((current.isoformat(), [ry, rq, rp, rw]))
        current += timedelta(days=1)

    return _get_dialect_config(dialect).row_table_emitter.emit(
        table_name, _RETAIL_445_INT_COLUMNS, rows
    )


def _emit_thai_buddhist(
    dialect: str, table_name: str, start: date, end: date, _fys: int
) -> str:
    date_alias, from_clause = _date_source(dialect, start, end)

    thai_year_pg = f"(EXTRACT(YEAR FROM {date_alias})::int + 543)"
    half_pg = f"CASE WHEN EXTRACT(MONTH FROM {date_alias}) <= 6 THEN 1 ELSE 2 END"
    quarter_pg = f"EXTRACT(QUARTER FROM {date_alias})::int"
    month_pg = f"EXTRACT(MONTH FROM {date_alias})::int"
    week_pg = f"EXTRACT(WEEK FROM {date_alias})::int"
    day_pg = f"EXTRACT(DAY FROM {date_alias})::int"

    columns = [
        (f"{date_alias}", "date_key"),
        (_transpile_expr(thai_year_pg, dialect), "thai_year"),
        (_transpile_expr(half_pg, dialect), "half_no"),
        (_transpile_expr(quarter_pg, dialect), "quarter_no"),
        (_transpile_expr(month_pg, dialect), "month_no"),
        (_transpile_expr(week_pg, dialect), "week_no"),
        (_transpile_expr(day_pg, dialect), "day_no"),
    ]
    col_sql = ",\n    ".join(f"{expr} AS {alias}" for expr, alias in columns)
    select_sql = f"SELECT\n    {col_sql}\nFROM {from_clause}"
    return _ddl_wrapper(dialect, table_name, select_sql)


def _emit_hijri(
    dialect: str, table_name: str, start: date, end: date, _fys: int
) -> str:
    """Hijri calendar — requires hijri-converter package.

    Since SQL dialects don't natively support Hijri dates, we generate
    the data in Python and emit INSERT statements.
    """
    try:
        from hijri_converter import Gregorian
    except ImportError:
        raise ValueError(
            "Hijri calendar requires the 'hijri-converter' package. "
            "Install it with: pip install hijri-converter"
        )

    from datetime import timedelta
    rows: list[RowTableRow] = []
    current = start
    while current <= end:
        hijri = Gregorian(current.year, current.month, current.day).to_hijri()
        rows.append((current.isoformat(), [hijri.year, hijri.month, hijri.day]))
        current += timedelta(days=1)

    return _get_dialect_config(dialect).row_table_emitter.emit(
        table_name, ["hijri_year", "hijri_month", "hijri_day"], rows
    )
