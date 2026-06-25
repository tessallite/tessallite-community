"""Dialect/connector support for the query rewriter.

Pure, self-contained helpers: dialect/connector name mapping, the BigQuery
generator patch, and the raw-SQL dialect translation / transpile boundary.
All SQL is built PostgreSQL-canonical and translated here at the return edge.

Extracted from query_rewriter.py (Phase 3 decomposition); behaviour-identical.
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.dialects.bigquery import BigQuery as _BigQueryDialect

# Register missing sqlglot dialect generators (sqlglot 30.x gaps) on import.
from shared.sqlglot_compat import register_bigquery_patches

register_bigquery_patches()


# Map sqlglot dialect strings → connector_qualify connector names.
_DIALECT_TO_CONNECTOR: dict[str, str] = {
    "bigquery": "bigquery",
    "spark": "hadoop_spark",
    "redshift": "redshift",
    "snowflake": "snowflake",
    "tsql": "sqlserver",
}


def _dialect_to_connector(dialect: str) -> str:
    """Convert a sqlglot dialect string to a connector_qualify connector name."""
    return _DIALECT_TO_CONNECTOR.get(dialect, "postgresql")


# Inverse of ``_DIALECT_TO_CONNECTOR`` (connector_qualify connector name →
# sqlglot dialect string). Used by the regex-substitution path to choose the
# right per-connector identifier quoting (F-006-03).
_CONNECTOR_TO_DIALECT: dict[str, str] = {
    connector: dialect for dialect, connector in _DIALECT_TO_CONNECTOR.items()
}
# Aliases that may appear as connector names but are not in the canonical map.
_CONNECTOR_TO_DIALECT.setdefault("spark", "spark")
_CONNECTOR_TO_DIALECT.setdefault("spark_sql", "spark")


def _connector_to_dialect(connector: str) -> str:
    """Convert a connector_qualify connector name back to a sqlglot dialect."""
    return _CONNECTOR_TO_DIALECT.get((connector or "").lower(), "postgres")


def _dialect_from_connection_type(connector_type: str | None) -> str:
    """Map a normalized connection_type string to a sqlglot dialect name.

    Always call normalize_connection_type() before this function so that
    legacy aliases (e.g. ``jdbc`` → ``hadoop_spark``) are already collapsed.
    """
    ct = (connector_type or "").lower()
    if ct == "bigquery":
        return "bigquery"
    if ct in ("hadoop_spark", "spark", "spark_sql"):
        return "spark"
    if ct == "redshift":
        return "redshift"
    if ct == "snowflake":
        return "snowflake"
    if ct == "sqlserver":
        return "tsql"
    return "postgres"


def _bq_filter_sql(self: _BigQueryDialect.Generator, expression: exp.Filter) -> str:
    """ARRAY_AGG(x ...) FILTER(WHERE x IS NOT NULL) → ARRAY_AGG(x IGNORE NULLS ...)"""
    agg = expression.this
    if isinstance(agg, exp.ArrayAgg):
        return self.sql(exp.IgnoreNulls(this=agg.copy()))
    return self.filter_sql(expression)


if exp.Filter not in _BigQueryDialect.Generator.TRANSFORMS:
    _BigQueryDialect.Generator.TRANSFORMS[exp.Filter] = _bq_filter_sql


def _requote_identifiers_for_bigquery(sql: str) -> str:
    """Convert ANSI double-quoted identifiers to BigQuery backtick quoting.

    BigQuery treats ``"value"`` as a *string literal*, not an identifier.
    Identifiers must use backtick quoting (`` `value` ``).

    Uses sqlglot to parse as Postgres and emit as BigQuery so string literals
    are handled correctly.  Falls back to a regex that skips single-quoted
    strings if sqlglot cannot parse the SQL (e.g. vendor-specific constructs).

    The regex fallback is conservative: it only skips single-quoted literals.
    Non-standard double-quoted string literals that may appear in hand-written
    SQL from some BI tools would still be rewritten.  In practice the
    passthrough SQL routed through here originates from the frontend or
    standards-compliant clients that use single quotes for strings.
    """
    import re
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
        return tree.sql(dialect="bigquery")
    except Exception:
        # Regex fallback: skip single-quoted string literals, rewrite identifiers.
        parts = re.split(r"('(?:[^'\\]|\\.)*')", sql)
        out = []
        for i, part in enumerate(parts):
            if i % 2 == 1:
                out.append(part)
            else:
                out.append(re.sub(r'"([^"]+)"', r'`\1`', part))
        return "".join(out)


def _requote_identifiers_for_dialect(sql: str, target_dialect: str) -> str:
    """Rewrite a PostgreSQL-canonical statement to *target_dialect*.

    F-006-03 / Bug-5339: the table-substitution path
    (``_substitute_table_names``) substitutes the physical table in
    PostgreSQL-canonical double-quoted form, so the whole statement parses as
    PostgreSQL here. This single sqlglot round-trip therefore performs the
    COMPLETE PG→target translation — identifier quoting AND dialect-specific
    function syntax (e.g. ``DATE_TRUNC('month', col)`` →
    ``TIMESTAMP_TRUNC(col, MONTH)`` for BigQuery) — in one pass:

    - BigQuery / Spark treat ``"value"`` as a *string literal*, not an
      identifier, and require backticks (`` `value` ``).
    - SQL Server expects bracket quoting (``[value]``).

    It parses as PostgreSQL and re-emits in the target dialect. A parse failure
    falls back to the original SQL (callers always receive a usable string);
    for BigQuery the dedicated regex-fallback helper is reused so its
    conservative literal-skipping behaviour is preserved when a parse fails.
    """
    if target_dialect in ("postgres", "postgresql"):
        return sql
    if target_dialect == "bigquery":
        return _requote_identifiers_for_bigquery(sql)
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
        return tree.sql(dialect=target_dialect)
    except Exception:
        return sql


def _translate_raw_sql(sql: str, target_dialect: str, input_dialect: str = "postgres") -> str:
    """Best-effort dialect translation for raw-SQL fallback return paths.

    When the rewriter cannot fully bind a query this ensures the SQL is at
    minimum translated to the target dialect rather than returned in the
    author's original syntax.

    F-006-11: raw queries carry an ``input_dialect`` (the dialect the query was
    authored in — honoured by the pocket and WHERE/passthrough re-parsers). The
    fallback previously always read ``postgres``, so a non-PG-authored raw
    query (e.g. BigQuery DATE syntax) was silently returned untranslated. Thread
    ``input_dialect`` so the read side matches how the query was written. The
    read is treated as a hint: if a parse under ``input_dialect`` fails we retry
    under ``postgres`` (the rewriter's canonical form) before giving up.

    For BigQuery the dedicated requote helper is preferred when the input is
    already PG-canonical; when an explicit non-PG input dialect is supplied the
    generic transpile path is used so the read side is honoured. Any parse or
    transpile error falls back silently to the original SQL so callers always
    receive a usable string.
    """
    if target_dialect in ("postgres", "postgresql"):
        return sql
    _read = (input_dialect or "postgres").lower()
    if _read in ("postgresql",):
        _read = "postgres"
    if target_dialect == "bigquery" and _read == "postgres":
        return _requote_identifiers_for_bigquery(sql)
    for _candidate_read in (_read, "postgres") if _read != "postgres" else ("postgres",):
        try:
            results = sqlglot.transpile(
                sql,
                read=_candidate_read,
                write=target_dialect,
                error_level=sqlglot.ErrorLevel.IGNORE,
            )
            if results:
                return results[0]
        except Exception:
            continue
    return sql


def _transpile_to_dialect(sql: str, target_dialect: str) -> str:
    """Transpile a PostgreSQL-canonical SQL string to *target_dialect* via SQLGlot.

    This is the single final translation step applied to every return path in
    ``_build_source_sql``.  All SQL construction inside that function uses
    PostgreSQL double-quoted identifiers and standard PostgreSQL syntax; this
    function is the only place where connector-native syntax (BigQuery backticks,
    SQL Server brackets, TSQL OFFSET/FETCH, etc.) is introduced.

    Returns the original SQL unchanged when:
    - target_dialect is "postgres" or "postgresql" (no translation needed).
    - SQLGlot cannot parse or transpile the query (safe fallback to avoid
      swallowing a valid query that was already formatted correctly for a target).
    """
    if target_dialect in ("postgres", "postgresql"):
        return sql
    try:
        results = sqlglot.transpile(
            sql,
            read="postgres",
            write=target_dialect,
            error_level=sqlglot.ErrorLevel.IGNORE,
        )
        return results[0] if results else sql
    except Exception:
        return sql


def dialect_to_connector(dialect: str) -> str:
    """Public alias for dialect → connector mapping — used by router.py."""
    return _dialect_to_connector(dialect)


def dialect_from_connection_type(connector_type: str | None) -> str:
    """Public alias for connection_type → sqlglot dialect mapping — used by router.py."""
    return _dialect_from_connection_type(connector_type)

