"""Cross-database column type mapping for aggregate materialization.

Uses sqlglot for dialect-aware type transpilation. When the source type
cannot be mapped, falls back to a safe default (TEXT for PostgreSQL,
STRING for BigQuery, STRING for Spark).
"""
from __future__ import annotations

import logging

import sqlglot

logger = logging.getLogger(__name__)

_SQLGLOT_DIALECT_MAP: dict[str, str] = {
    "postgresql": "postgres",
    "bigquery": "bigquery",
    "hadoop_spark": "spark",
    "redshift": "redshift",
    "snowflake": "snowflake",
    "sqlserver": "tsql",
}

_SAFE_FALLBACK: dict[str, str] = {
    "postgres": "TEXT",
    "bigquery": "STRING",
    "redshift": "VARCHAR(MAX)",
    "snowflake": "VARCHAR",
    "spark": "STRING",
    "tsql": "NVARCHAR(MAX)",
}

_AGG_RESULT_TYPE: dict[str, dict[str, str]] = {
    "postgres": {
        "sum": "NUMERIC",
        "avg": "DOUBLE PRECISION",
        "min": "NUMERIC",
        "max": "NUMERIC",
        "count": "BIGINT",
        "count_distinct": "BIGINT",
        "percentile": "DOUBLE PRECISION",
    },
    "bigquery": {
        "sum": "FLOAT64",
        "avg": "FLOAT64",
        "min": "FLOAT64",
        "max": "FLOAT64",
        "count": "INT64",
        "count_distinct": "INT64",
        "percentile": "FLOAT64",
    },
    "redshift": {
        "sum": "NUMERIC",
        "avg": "DOUBLE PRECISION",
        "min": "NUMERIC",
        "max": "NUMERIC",
        "count": "BIGINT",
        "count_distinct": "BIGINT",
        "percentile": "DOUBLE PRECISION",
    },
    "snowflake": {
        "sum": "NUMBER",
        "avg": "FLOAT",
        "min": "NUMBER",
        "max": "NUMBER",
        "count": "NUMBER",
        "count_distinct": "NUMBER",
        "percentile": "FLOAT",
    },
    "spark": {
        "sum": "DOUBLE",
        "avg": "DOUBLE",
        "min": "DOUBLE",
        "max": "DOUBLE",
        "count": "BIGINT",
        "count_distinct": "BIGINT",
        "percentile": "DOUBLE",
    },
}


def map_column_type(
    source_type: str,
    source_dialect: str,
    target_dialect: str,
) -> str:
    """Transpile a source column type to the target dialect using sqlglot.

    Falls back to the target dialect's safe default if transpilation fails.
    """
    src = _SQLGLOT_DIALECT_MAP.get(source_dialect, source_dialect)
    tgt = _SQLGLOT_DIALECT_MAP.get(target_dialect, target_dialect)

    if src == tgt:
        return source_type

    try:
        transpiled = sqlglot.transpile(
            f"SELECT CAST(x AS {source_type})",
            read=src,
            write=tgt,
        )
        if transpiled:
            sql = transpiled[0]
            start = sql.upper().find("CAST(X AS ") + len("CAST(X AS ")
            end = sql.rfind(")")
            if start > 0 and end > start:
                return sql[start:end].strip()
    except Exception as exc:
        logger.debug(
            "Type mapping failed: %s (%s->%s): %s",
            source_type, src, tgt, exc,
        )

    return _SAFE_FALLBACK.get(tgt, "TEXT")


def aggregate_result_type(stat_type: str, target_dialect: str) -> str:
    """Return the target type for an aggregated measure column."""
    tgt = _SQLGLOT_DIALECT_MAP.get(target_dialect, target_dialect)
    types = _AGG_RESULT_TYPE.get(tgt, _AGG_RESULT_TYPE["postgres"])
    return types.get(stat_type, types.get("sum", "NUMERIC"))


def grain_column_type(
    target_dialect: str,
    source_type: str | None = None,
    source_dialect: str | None = None,
) -> str:
    """Return the target type for a grain (dimension) column.

    When *source_type* and *source_dialect* are provided the function
    transpiles the source type to the target dialect via ``map_column_type``
    so that numeric / date grain columns retain their native types on the
    aggregate table.  This prevents the type mismatch that Bug-6147 exposed:
    the query-router renders type-faithful filter literals (e.g. unquoted
    integers for an INTEGER source column) but the old code typed every
    cross-DB grain column as TEXT, causing ``operator does not exist: text >=
    integer`` failures at execution time.

    Falls back to the safe TEXT/STRING/VARCHAR default only when the source
    type is unknown or transpilation fails.
    """
    if source_type and source_dialect:
        return map_column_type(source_type, source_dialect, target_dialect)
    tgt = _SQLGLOT_DIALECT_MAP.get(target_dialect, target_dialect)
    return _SAFE_FALLBACK.get(tgt, "TEXT")
