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
# IMPORTANT: this constant governs the QUERY-TIME expression path only
# (query-router preflight and the variant emitter in time_variants_sql.py).
# It does NOT constrain the DDL emitter.  The DDL emitter creates a
# physical reference calendar table and may use row-materialised output
# (e.g. iso_week — Bug-6240) even for expression-capable types, because
# the DDL path and the query-time path are independent: the DDL path
# must produce correct SQL on every dialect for table creation, while the
# query-time path computes period boundaries inline on the fact query.
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

    __slots__ = (
        "ctas_prefix", "supports_pk", "connector_name", "date_source_fn",
        "row_table_emitter", "iso_week_unit",
    )

    def __init__(
        self,
        ctas_prefix: str,
        supports_pk: bool,
        date_source_fn: "type[_DateSource]",
        row_table_emitter: "type[_RowTableEmitter]",
        connector_name: str = "postgresql",
        iso_week_unit: str = "WEEK",
    ):
        self.ctas_prefix = ctas_prefix
        self.supports_pk = supports_pk
        # Bug-7206: connector name used for identifier quoting via
        # quote_table_ref. Every dialect must quote identifiers to prevent
        # SQL injection through table names containing semicolons or other
        # SQL metacharacters.
        self.connector_name = connector_name
        self.date_source_fn = date_source_fn
        self.row_table_emitter = row_table_emitter
        # Bug-6568: the EXTRACT unit that yields an ISO-8601 week number on this
        # dialect. PostgreSQL/Redshift/Spark ``WEEK`` is already ISO (Monday-
        # anchored, week 1 = the week with the first Thursday), but BigQuery /
        # Snowflake ``WEEK`` is Sunday-anchored and non-ISO — those must use the
        # ``ISOWEEK`` unit (sqlglot renders it as BigQuery ``EXTRACT(ISOWEEK …)``
        # and Snowflake ``DATE_PART(WEEKISO, …)``). This keeps the materialised
        # ``week_no`` column ISO-consistent across every source dialect instead
        # of silently diverging on BigQuery/Snowflake.
        self.iso_week_unit = iso_week_unit

    def ddl_wrapper(
        self,
        table_name: str,
        select_sql: str,
        pk_col: str | None = "date_key",
        cte_prefix: str = "",
    ) -> str:
        # F-016-06: ``cte_prefix`` (a top-level ``WITH`` clause) is injected
        # between ``CREATE TABLE t AS`` and the SELECT so a dialect whose day
        # spine is a recursive CTE (Redshift) renders valid SQL. It is empty for
        # every dialect whose source is a plain FROM-clause subquery.
        ddl = (
            f"DROP TABLE IF EXISTS {table_name};\n"
            f"{self.ctas_prefix.format(table=table_name)}\n"
            f"{cte_prefix}{select_sql};\n"
        )
        if pk_col and self.supports_pk:
            ddl += f"ALTER TABLE {table_name} ADD PRIMARY KEY ({pk_col});\n"
        return ddl

    def iso_week_expr(self, date_alias: str, dialect: str) -> str:
        """Return the target-dialect ISO-8601 week-number expression.

        Built from the dialect's ISO week unit and transpiled through sqlglot so
        the ``::int`` cast and function form match the sibling period columns.
        """
        pg_expr = f"EXTRACT({self.iso_week_unit} FROM {date_alias})::int"
        return _transpile_expr(pg_expr, dialect)


class _DateSource:
    @staticmethod
    def generate(s: str, e: str) -> tuple[str, str]:
        """Return ``(date_alias, from_clause)``: the SELECT-list date reference
        and the FROM-clause source that yields one row per day in ``[s, e]``."""
        raise NotImplementedError

    @staticmethod
    def cte(s: str, e: str) -> str:
        """Optional top-level ``WITH`` prefix a dialect needs before the
        SELECT (e.g. a recursive CTE date spine). Empty for dialects whose
        source is a plain FROM-clause subquery. F-016-06."""
        return ""


class _PgDateSource(_DateSource):
    @staticmethod
    def generate(s: str, e: str) -> tuple[str, str]:
        return "d::date", f"generate_series('{s}'::date, '{e}'::date, '1 day'::interval) d"


class _RedshiftDateSource(_DateSource):
    # F-016-06: Redshift's ``generate_series`` is a LEADER-NODE-ONLY function
    # and cannot be used in ``CREATE TABLE AS`` (which runs on the compute
    # nodes) — the previous config reused ``_PgDateSource`` and every
    # standard/fiscal/thai calendar DDL failed on Redshift. The portable
    # replacement is a recursive CTE, which Redshift supports (``WITH
    # RECURSIVE``) but only at the TOP of the query, so the day spine is emitted
    # as a ``cte()`` prefix and the FROM clause simply references it.
    @staticmethod
    def generate(s: str, e: str) -> tuple[str, str]:
        return "date_key", "date_seq"

    @staticmethod
    def cte(s: str, e: str) -> str:
        return (
            "WITH RECURSIVE date_seq (date_key) AS (\n"
            f"    SELECT CAST('{s}' AS DATE)\n"
            "    UNION ALL\n"
            f"    SELECT DATEADD(day, 1, date_key) FROM date_seq "
            f"WHERE date_key < CAST('{e}' AS DATE)\n"
            ")\n"
        )


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
    # F-016-10: GENERATE_SERIES is SQL Server 2022+ (compatibility level 160)
    # only, so the previous emitter failed on the still-common 2017/2019
    # servers. Replace it with a catalogue-derived numbers spine that works on
    # every supported version (2012+) without a recursive CTE or an
    # ``OPTION (MAXRECURSION ...)`` hint: ``sys.all_objects`` has thousands of
    # rows, so the self cross join yields far more than
    # MAX_ROW_MATERIALISED_DAYS (~150 years), and ``ROW_NUMBER() - 1`` bounded
    # by ``DATEDIFF`` gives contiguous 0..N day offsets. This is a plain
    # FROM-clause subquery, so it slots into the existing SELECT ... INTO wrapper
    # unchanged.
    @staticmethod
    def generate(s: str, e: str) -> tuple[str, str]:
        return "date_key", (
            "(\n"
            f"    SELECT DATEADD(DAY, n.num, CAST('{s}' AS DATE)) AS date_key\n"
            "    FROM (\n"
            "        SELECT ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) - 1 AS num\n"
            "        FROM sys.all_objects a CROSS JOIN sys.all_objects b\n"
            "    ) AS n\n"
            f"    WHERE n.num <= DATEDIFF(DAY, CAST('{s}' AS DATE), "
            f"CAST('{e}' AS DATE))\n"
            ") AS d"
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


# F-016-04: the previous BigQuery row emitter wrote one ``SELECT ... UNION ALL``
# arm per day into a single monolithic ``CREATE TABLE AS`` statement. Over a
# 100-year retail range that produced a ~4.8 MB statement (36,524 arms) that is
# slow to parse and can be rejected by the connector's statement-size limit.
# Like the T-SQL and Snowflake emitters, we now CREATE the table with an
# explicit schema and load rows with bounded, chunked ``INSERT ... VALUES``
# statements so no single statement grows without bound. BigQuery's documented
# ceiling is 10,000 rows per VALUES; a conservative chunk keeps each statement
# well under the query-length limit while preserving every row (no gaps, no
# duplicates).
_BQ_MAX_VALUES_ROWS = 1_000


class _BqRowTableEmitter(_RowTableEmitter):
    @staticmethod
    def emit(table_name: str, int_columns: list[str], rows: list[RowTableRow]) -> str:
        col_defs = ",\n    ".join(
            ["date_key DATE", *[f"{c} INT64 NOT NULL" for c in int_columns]]
        )
        col_list = ", ".join(["date_key", *int_columns])

        def _row_values(row: RowTableRow) -> str:
            date_iso, ints = row
            return (
                "(" + ", ".join([f"DATE '{date_iso}'", *[str(i) for i in ints]]) + ")"
            )

        parts: list[str] = [
            f"DROP TABLE IF EXISTS {table_name};\n"
            f"CREATE TABLE {table_name} (\n"
            f"    {col_defs}\n"
            f");\n"
        ]
        for chunk_start in range(0, len(rows), _BQ_MAX_VALUES_ROWS):
            chunk = rows[chunk_start:chunk_start + _BQ_MAX_VALUES_ROWS]
            values = ",\n    ".join(_row_values(r) for r in chunk)
            parts.append(
                f"INSERT INTO {table_name} ({col_list}) VALUES\n"
                f"    {values};\n"
            )
        return "".join(parts)


# F-016-09: Spark row-materialised calendars must not emit one unbounded
# ``VALUES`` clause. Row-materialised types (iso_week, retail_445, hijri) allow
# up to MAX_ROW_MATERIALISED_DAYS (~150 years) of rows; a single monolithic
# VALUES over that range can exceed the driver/parser statement-size limit and
# fail auto-create. Chunk the rows like the BigQuery / T-SQL / Snowflake
# emitters: the first chunk creates the table via ``CREATE TABLE ... AS SELECT
# ... FROM VALUES``; each later chunk appends via ``INSERT INTO ... SELECT ...
# FROM VALUES``. Every chunk keeps the Bug-7207 ``TO_DATE`` cast so date_key is
# typed DATE, not STRING.
_SPARK_MAX_VALUES_ROWS = 1_000


class _SparkRowTableEmitter(_RowTableEmitter):
    @staticmethod
    def emit(table_name: str, int_columns: list[str], rows: list[RowTableRow]) -> str:
        # Bug-7207: Spark infers bare string literals as STRING type.
        # Use TO_DATE() in the outer SELECT to ensure DATE typing.
        col_list = ", ".join(["date_key", *int_columns])
        select_cols = ", ".join(
            ["TO_DATE(date_key) AS date_key", *int_columns]
        )

        parts: list[str] = [f"DROP TABLE IF EXISTS {table_name};\n"]
        for chunk_start in range(0, len(rows), _SPARK_MAX_VALUES_ROWS):
            chunk = rows[chunk_start:chunk_start + _SPARK_MAX_VALUES_ROWS]
            values = ",\n    ".join(_row_values_literal(r) for r in chunk)
            if chunk_start == 0:
                parts.append(
                    f"CREATE TABLE {table_name} USING parquet AS\n"
                    f"SELECT {select_cols} FROM VALUES\n"
                    f"    {values}\n"
                    f"AS t({col_list});\n"
                )
            else:
                parts.append(
                    f"INSERT INTO {table_name}\n"
                    f"SELECT {select_cols} FROM VALUES\n"
                    f"    {values}\n"
                    f"AS t({col_list});\n"
                )
        return "".join(parts)


# Bug-7619: T-SQL limits VALUES clauses to 1000 rows. Row-materialised
# calendars (iso_week, retail_445, hijri) over multi-year date ranges easily
# exceed this. This emitter batches the INSERT into chunks of at most
# _TSQL_MAX_VALUES_ROWS rows each, emitting one INSERT statement per chunk.
# Bug-7768: Snowflake caps VALUES clauses at 16,384 rows. Redshift and
# PostgreSQL have no practical VALUES row limit for these volumes.
_TSQL_MAX_VALUES_ROWS = 999
_SNOWFLAKE_MAX_VALUES_ROWS = 16_384


class _SqlServerRowTableEmitter(_RowTableEmitter):
    """Emit CREATE TABLE + batched INSERT statements for SQL Server.

    T-SQL restricts a single ``INSERT ... VALUES`` to at most 1000 rows. For
    multi-year calendar date ranges the row count regularly exceeds this limit,
    so the emitter splits rows into chunks of ``_TSQL_MAX_VALUES_ROWS`` and
    emits one INSERT per chunk. This guarantees valid T-SQL regardless of the
    date range size while keeping every row present (no gaps, no duplicates).
    """

    @staticmethod
    def emit(table_name: str, int_columns: list[str], rows: list[RowTableRow]) -> str:
        col_defs = ",\n    ".join(
            ["date_key DATE PRIMARY KEY", *[f"{c} INT NOT NULL" for c in int_columns]]
        )
        col_list = ", ".join(["date_key", *int_columns])

        parts: list[str] = [
            f"DROP TABLE IF EXISTS {table_name};\n"
            f"CREATE TABLE {table_name} (\n"
            f"    {col_defs}\n"
            f");\n"
        ]

        # Batch rows into chunks that respect the T-SQL 1000-row VALUES limit.
        for chunk_start in range(0, len(rows), _TSQL_MAX_VALUES_ROWS):
            chunk = rows[chunk_start:chunk_start + _TSQL_MAX_VALUES_ROWS]
            values = ",\n    ".join(_row_values_literal(r) for r in chunk)
            parts.append(
                f"INSERT INTO {table_name} ({col_list}) VALUES\n"
                f"    {values};\n"
            )

        return "".join(parts)


# Bug-7768: Snowflake caps a single VALUES clause at 16,384 rows. For 45+
# year calendar date ranges (16,000+ days) the row count can exceed this
# limit. This emitter uses the same batched-INSERT approach as the T-SQL
# emitter but with a 16,384-row threshold.
class _SnowflakeRowTableEmitter(_RowTableEmitter):
    """Emit CREATE TABLE + batched INSERT statements for Snowflake.

    Snowflake restricts a single ``INSERT ... VALUES`` to at most 16,384 rows.
    For very wide date ranges the emitter splits rows into chunks and emits
    one INSERT per chunk, identical in structure to the T-SQL batching.
    """

    @staticmethod
    def emit(table_name: str, int_columns: list[str], rows: list[RowTableRow]) -> str:
        col_defs = ",\n    ".join(
            ["date_key DATE PRIMARY KEY", *[f"{c} INT NOT NULL" for c in int_columns]]
        )
        col_list = ", ".join(["date_key", *int_columns])

        parts: list[str] = [
            f"DROP TABLE IF EXISTS {table_name};\n"
            f"CREATE TABLE {table_name} (\n"
            f"    {col_defs}\n"
            f");\n"
        ]

        for chunk_start in range(0, len(rows), _SNOWFLAKE_MAX_VALUES_ROWS):
            chunk = rows[chunk_start:chunk_start + _SNOWFLAKE_MAX_VALUES_ROWS]
            values = ",\n    ".join(_row_values_literal(r) for r in chunk)
            parts.append(
                f"INSERT INTO {table_name} ({col_list}) VALUES\n"
                f"    {values};\n"
            )

        return "".join(parts)


class _SqlServerDialectConfig(_DialectConfig):
    """SQL Server uses ``SELECT ... INTO`` instead of ``CREATE TABLE ... AS SELECT``.

    T-SQL does not support the ANSI ``CREATE TABLE t AS SELECT ...`` form.
    The equivalent is ``SELECT columns INTO t FROM source``.  This subclass
    overrides ``ddl_wrapper`` to inject the ``INTO <table>`` clause between
    the column list and the ``FROM`` keyword of the emitted SELECT.
    """

    def ddl_wrapper(
        self,
        table_name: str,
        select_sql: str,
        pk_col: str | None = "date_key",
        cte_prefix: str = "",
    ) -> str:
        from_pos = select_sql.find("\nFROM ")
        if from_pos == -1:
            raise ValueError(
                "Cannot locate FROM clause in SELECT for SQL Server "
                "INTO rewrite"
            )
        select_part = select_sql[:from_pos]
        from_part = select_sql[from_pos:]  # includes leading \n
        # cte_prefix is empty for SQL Server (its numbers spine is a FROM-clause
        # subquery, not a top-level CTE); accepted for signature parity.
        ddl = (
            f"DROP TABLE IF EXISTS {table_name};\n"
            f"{cte_prefix}{select_part}\n"
            f"INTO {table_name}{from_part};\n"
        )
        if pk_col and self.supports_pk:
            ddl += f"ALTER TABLE {table_name} ADD PRIMARY KEY ({pk_col});\n"
        return ddl

    def iso_week_expr(self, date_alias: str, dialect: str) -> str:
        # Bug-6568: sqlglot renders the ISOWEEK extract unit as
        # ``DATEPART(ISOWEEK, …)``, which T-SQL rejects — SQL Server's ISO week
        # datepart token is ``ISO_WEEK``. Emit the native form directly (CAST for
        # parity with the sibling ::int columns).
        return f"CAST(DATEPART(ISO_WEEK, {date_alias}) AS INT)"


_DIALECT_CONFIGS: dict[str, _DialectConfig] = {
    "postgresql": _DialectConfig("CREATE TABLE {table} AS", True, _PgDateSource, _PgRowTableEmitter, connector_name="postgresql"),
    "redshift": _DialectConfig("CREATE TABLE {table} AS", True, _RedshiftDateSource, _PgRowTableEmitter, connector_name="redshift"),
    "bigquery": _DialectConfig("CREATE TABLE {table} AS", False, _BqDateSource, _BqRowTableEmitter, connector_name="bigquery", iso_week_unit="ISOWEEK"),
    "hadoop_spark": _DialectConfig("CREATE TABLE {table} USING parquet AS", False, _SparkDateSource, _SparkRowTableEmitter, connector_name="hadoop_spark"),
    "snowflake": _DialectConfig("CREATE TABLE {table} AS", False, _SnowflakeDateSource, _SnowflakeRowTableEmitter, connector_name="snowflake", iso_week_unit="ISOWEEK"),
    "sqlserver": _SqlServerDialectConfig("", True, _SqlServerDateSource, _SqlServerRowTableEmitter, connector_name="sqlserver"),
}


def _get_dialect_config(dialect: str) -> _DialectConfig:
    config = _DIALECT_CONFIGS.get(dialect)
    if not config:
        raise ValueError(f"Unsupported dialect: {dialect}")
    return config


def _date_source(dialect: str, start: date, end: date) -> tuple[str, str, str]:
    """Return ``(date_alias, from_clause, cte_prefix)`` for *dialect*.

    ``cte_prefix`` is a top-level ``WITH`` clause (empty unless the dialect's
    day spine is a recursive CTE — Redshift, F-016-06).
    """
    s, e = start.isoformat(), end.isoformat()
    fn = _get_dialect_config(dialect).date_source_fn
    alias, from_clause = fn.generate(s, e)
    return alias, from_clause, fn.cte(s, e)


def _ddl_wrapper(
    dialect: str,
    table_name: str,
    select_sql: str,
    pk_col: str | None = "date_key",
    cte_prefix: str = "",
) -> str:
    return _get_dialect_config(dialect).ddl_wrapper(
        table_name, select_sql, pk_col, cte_prefix
    )


# F-016-04: preflight row budget for ROW-MATERIALISED calendars (iso_week /
# retail_445 / hijri). Even with batched INSERTs, a runaway date range would
# emit thousands of statements and load millions of rows. Expression-capable
# DDL calendars (standard / fiscal / thai_buddhist) are a single CTAS and are
# not bounded here. ~150 years of daily rows is a generous ceiling for a real
# analytics calendar while refusing an accidental millennia-wide range.
#
# NOTE: ``iso_week`` is in EXPRESSION_CAPABLE_CALENDAR_TYPES for the QUERY-TIME
# path but its DDL is ROW-MATERIALISED (``_emit_iso_week`` computes ISO parts in
# Python because ISO date-part value semantics differ across dialects). The row
# budget must therefore key on the DDL-materialisation shape (the emitters that
# build a per-day ``rows`` list), NOT on EXPRESSION_CAPABLE_CALENDAR_TYPES —
# otherwise iso_week would slip the budget and emit ~146k INSERTs over a
# millennia-wide range.
ROW_MATERIALISED_CALENDAR_TYPES: frozenset[str] = frozenset({
    "iso_week", "retail_445", "hijri",
})
MAX_ROW_MATERIALISED_DAYS = 55_000


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

    # F-016-04: bound row-materialised calendar generation up front so a valid
    # but enormous range fails loud with an actionable message instead of
    # producing an oversized, slow-loading DDL payload. Keyed on the DDL
    # row-materialisation shape (iso_week is expression-capable at query time
    # but row-materialised in DDL — see ROW_MATERIALISED_CALENDAR_TYPES).
    if calendar_type in ROW_MATERIALISED_CALENDAR_TYPES:
        span_days = (end_date - start_date).days + 1
        if span_days > MAX_ROW_MATERIALISED_DAYS:
            raise ValueError(
                f"Calendar type {calendar_type!r} is row-materialised and the "
                f"requested range spans {span_days} days, exceeding the "
                f"{MAX_ROW_MATERIALISED_DAYS}-day limit "
                f"(~{MAX_ROW_MATERIALISED_DAYS // 365} years). Narrow the "
                "date range."
            )

    if calendar_type in ("standard", "fiscal"):
        if not 1 <= fiscal_year_start_month <= 12:
            raise ValueError("fiscal_year_start_month must be between 1 and 12")
        fys = fiscal_year_start_month
    else:
        fys = 1

    config = _get_dialect_config(dialect)
    # Bug-7206: quote identifiers for ALL dialects, not just BigQuery.
    # Prevents SQL injection via table names containing semicolons or other
    # SQL metacharacters (e.g. "cal; DELETE FROM sales.orders; --").
    table_name = quote_table_ref(config.connector_name, table_name)

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
    date_alias, from_clause, cte_prefix = _date_source(dialect, start, end)

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
            f"(FLOOR(((EXTRACT(MONTH FROM {date_alias})::int - {fys} + 12) % 12) / 3.0) + 1)::int"
        )
    # F-016-16: month_no is the CALENDAR month (1-12) for both standard and
    # fiscal calendars — the fiscal offset is applied to year/half/quarter but
    # not to the month column. A fiscal period number, when needed, is
    # ``((month_no - fys + 12) % 12) + 1`` derived on demand; the table does not
    # store a separate fiscal-period column. Documented in
    # architecture_multi-calendar.md.
    month_pg = f"EXTRACT(MONTH FROM {date_alias})::int"
    day_pg = f"EXTRACT(DAY FROM {date_alias})::int"

    columns = [
        (f"{date_alias}", "date_key"),
        (_transpile_expr(year_pg, dialect), "year_no"),
        (_transpile_expr(half_pg, dialect), "half_no"),
        (_transpile_expr(quarter_pg, dialect), "quarter_no"),
        (_transpile_expr(month_pg, dialect), "month_no"),
        # Bug-6568: dialect-aware ISO week (non-ISO EXTRACT(WEEK) on BigQuery).
        (_get_dialect_config(dialect).iso_week_expr(date_alias, dialect), "week_no"),
        (_transpile_expr(day_pg, dialect), "day_no"),
    ]
    col_sql = ",\n    ".join(f"{expr} AS {alias}" for expr, alias in columns)
    select_sql = f"SELECT\n    {col_sql}\nFROM {from_clause}"
    return _ddl_wrapper(dialect, table_name, select_sql, cte_prefix=cte_prefix)


def _emit_iso_week(
    dialect: str, table_name: str, start: date, end: date, _fys: int
) -> str:
    """ISO 8601 week calendar -- iso_year, iso_week, iso_day_of_week.

    Bug-6240 / F-016-25: ISO date parts (ISOYEAR, ISODOW) have incompatible
    value semantics across SQL dialects -- ISODOW is 1=Mon..7=Sun in
    PostgreSQL but BigQuery DAYOFWEEK is 1=Sun..7=Sat, and Snowflake /
    SQL Server lack ISOYEAR entirely.  sqlglot DATE_PART_MAPPING can
    rename parts but cannot adjust value ranges, so transpilation alone
    cannot produce correct materialised ISO day-of-week values on all
    dialects.

    To guarantee correct ISO 8601 values on every dialect, the columns
    are computed in Python via ``date.isocalendar()`` and materialised as
    row data -- the same sanctioned approach used for retail_445 and hijri
    calendars whose period math also cannot be faithfully transpiled.

    iso_week remains in EXPRESSION_CAPABLE_CALENDAR_TYPES because the
    query-time path (time_variants_sql.py) handles ISO date parts
    correctly with proper dialect mappings for equality-based operations
    (PARTITION BY / GROUP BY).  The DDL emitter and the query-time path
    are independent concerns; see the EXPRESSION_CAPABLE_CALENDAR_TYPES
    comment block.
    """
    from datetime import timedelta

    rows: list[RowTableRow] = []
    current = start
    while current <= end:
        iso_year, iso_week_no, iso_dow = current.isocalendar()
        rows.append((current.isoformat(), [iso_year, iso_week_no, iso_dow]))
        current += timedelta(days=1)

    return _get_dialect_config(dialect).row_table_emitter.emit(
        table_name, ["iso_year", "iso_week", "iso_day_of_week"], rows
    )


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
    date_alias, from_clause, cte_prefix = _date_source(dialect, start, end)

    thai_year_pg = f"(EXTRACT(YEAR FROM {date_alias})::int + 543)"
    half_pg = f"CASE WHEN EXTRACT(MONTH FROM {date_alias}) <= 6 THEN 1 ELSE 2 END"
    quarter_pg = f"EXTRACT(QUARTER FROM {date_alias})::int"
    month_pg = f"EXTRACT(MONTH FROM {date_alias})::int"
    day_pg = f"EXTRACT(DAY FROM {date_alias})::int"

    columns = [
        (f"{date_alias}", "date_key"),
        (_transpile_expr(thai_year_pg, dialect), "thai_year"),
        (_transpile_expr(half_pg, dialect), "half_no"),
        (_transpile_expr(quarter_pg, dialect), "quarter_no"),
        (_transpile_expr(month_pg, dialect), "month_no"),
        # Bug-6568: dialect-aware ISO week (non-ISO EXTRACT(WEEK) on BigQuery).
        (_get_dialect_config(dialect).iso_week_expr(date_alias, dialect), "week_no"),
        (_transpile_expr(day_pg, dialect), "day_no"),
    ]
    col_sql = ",\n    ".join(f"{expr} AS {alias}" for expr, alias in columns)
    select_sql = f"SELECT\n    {col_sql}\nFROM {from_clause}"
    return _ddl_wrapper(dialect, table_name, select_sql, cte_prefix=cte_prefix)


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
