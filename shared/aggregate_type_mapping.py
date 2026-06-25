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
}

_SAFE_FALLBACK: dict[str, str] = {
    "postgres": "TEXT",
    "bigquery": "STRING",
    "redshift": "VARCHAR(MAX)",
    "snowflake": "VARCHAR",
    "spark": "STRING",
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


def grain_column_type(target_dialect: str) -> str:
    """Default type for grain (dimension) columns on the target."""
    tgt = _SQLGLOT_DIALECT_MAP.get(target_dialect, target_dialect)
    return _SAFE_FALLBACK.get(tgt, "TEXT")
