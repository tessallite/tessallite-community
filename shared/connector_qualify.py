"""Unified object-name qualification and quoting for all connector types.

Every site that builds or quotes a table reference should use these
functions instead of hand-rolling connector-specific logic.

Connector types (canonical): ``postgresql``, ``bigquery``, ``hadoop_spark``,
``redshift``, ``snowflake``, ``sqlserver``.

Redshift uses the same double-quote identifier quoting as PostgreSQL.
SQL Server uses bracket quoting: ``[identifier]`` with ``]`` escaped as ``]]``.
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp

CONNECTOR_TO_SQLGLOT: dict[str, str] = {
    "postgresql": "postgres",
    "redshift": "postgres",
    "bigquery": "bigquery",
    "hadoop_spark": "spark",
    "snowflake": "snowflake",
    "sqlserver": "tsql",
}


def transpile_preview_sql(connector: str, canonical_sql: str) -> str:
    """Transpile canonical PostgreSQL SQL to the connector's dialect via sqlglot."""
    target = CONNECTOR_TO_SQLGLOT.get(connector, "postgres")
    if target == "postgres":
        return canonical_sql
    results = sqlglot.transpile(canonical_sql, read="postgres", write=target)
    return results[0] if results else canonical_sql


def qualify_table_name(
    connector: str,
    table_name: str,
    *,
    schema: str | None = None,
    project_id: str | None = None,
) -> str:
    """Build a fully-qualified dotted table name.

    Parameters
    ----------
    connector:
        Canonical connector type (``postgresql``, ``bigquery``, ``hadoop_spark``,
        ``redshift``, ``snowflake``).
    table_name:
        Bare table name (e.g. ``orders``).
    schema:
        Schema (PG), dataset (BQ), or database (Spark).
        For BQ, may already contain ``project_id.dataset`` — in that case
        *project_id* is not prepended again.
    project_id:
        GCP project ID (BQ only). Ignored for other connectors.

    Returns
    -------
    str
        The qualified name with dot separators, **unquoted**.
    """
    if not table_name:
        return table_name

    if connector == "bigquery":
        if schema and "." in schema:
            return f"{schema}.{table_name}"
        if schema and project_id:
            return f"{project_id}.{schema}.{table_name}"
        if schema:
            return f"{schema}.{table_name}"
        return table_name

    if schema:
        return f"{schema}.{table_name}"
    return table_name


def extract_dataset(connector: str, schema: str) -> str:
    """Return the bare dataset/schema name, stripping any project prefix.

    For BigQuery, ``tessallite-io.demo_data`` → ``demo_data``.
    For all other connectors the value is returned unchanged.
    """
    if not schema:
        return schema
    if connector == "bigquery" and "." in schema:
        return schema.rsplit(".", 1)[-1]
    return schema


def _strip_existing_quotes(identifier: str) -> str:
    """Remove pre-existing dialect-specific quoting to avoid double-quoting."""
    if len(identifier) >= 2:
        if identifier.startswith('"') and identifier.endswith('"'):
            return identifier[1:-1].replace('""', '"')
        if identifier.startswith("`") and identifier.endswith("`"):
            return identifier[1:-1]
        if identifier.startswith("[") and identifier.endswith("]"):
            return identifier[1:-1].replace("]]", "]")
    return identifier


def _escape_bigquery_identifier(identifier: str) -> str:
    """Escape literal characters using GoogleSQL quoted-identifier rules."""
    escapes = {
        "\\": "\\\\",
        "`": "\\`",
        "\a": "\\a",
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
        "\v": "\\v",
    }
    return "".join(escapes.get(char, char) for char in identifier)


def safe_ident(name: str) -> str:
    """Double-quote a SQL identifier using PostgreSQL quoting convention.

    Canonical quoting for SQL that will be processed by the query-router,
    which handles dialect-specific transpilation downstream.
    """
    return '"' + name.replace('"', '""') + '"'


def quote_identifier(connector: str, identifier: str) -> str:
    """Quote a single identifier for *connector* using sqlglot dialect handling."""
    if not identifier:
        return identifier
    bare = _strip_existing_quotes(identifier)
    if connector == "bigquery":
        return f"`{_escape_bigquery_identifier(bare)}`"
    dialect = CONNECTOR_TO_SQLGLOT.get(connector, "postgres")
    return exp.Identifier(this=bare, quoted=True).sql(dialect=dialect)


_TIMESTAMP_TYPES = frozenset({"TIMESTAMP", "TIMESTAMPTZ", "TIMESTAMP_TZ", "DATETIME"})
_DATE_TYPES = frozenset({"DATE"})


def coerce_join_types(
    lhs_expr: str, lhs_type: str | None,
    rhs_expr: str, rhs_type: str | None,
) -> tuple[str, str]:
    """Wrap the TIMESTAMP side in CAST(... AS DATE) when joining DATE to TIMESTAMP.

    Returns the (possibly modified) pair of expressions unchanged when both
    sides are the same type family.  Uses ANSI ``CAST(... AS DATE)`` which
    transpiles correctly across all dialects via sqlglot.
    """
    lt = (lhs_type or "").upper().split("(")[0].strip()
    rt = (rhs_type or "").upper().split("(")[0].strip()
    if lt in _TIMESTAMP_TYPES and rt in _DATE_TYPES:
        return f"CAST({lhs_expr} AS DATE)", rhs_expr
    if lt in _DATE_TYPES and rt in _TIMESTAMP_TYPES:
        return lhs_expr, f"CAST({rhs_expr} AS DATE)"
    return lhs_expr, rhs_expr


def quote_table_ref(connector: str, dotted_name: str) -> str:
    """Quote each segment of a dotted table reference.

    ``my-project.dataset.table`` becomes:

    - **PostgreSQL / Redshift / Snowflake**: ``"my-project"."dataset"."table"``
    - **BigQuery / Spark**: `` `my-project`.`dataset`.`table` ``
    """
    if not dotted_name:
        return dotted_name

    parts = dotted_name.split(".")
    return ".".join(quote_identifier(connector, p) for p in parts)
