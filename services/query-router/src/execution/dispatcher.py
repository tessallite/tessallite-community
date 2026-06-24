"""Centralised connector dispatch for query execution.

Bug-905: executor dispatch was previously duplicated inline in routes.py.
All connector-specific execution for user queries is routed through
``execute_on_connection`` here, ensuring new connectors need only be added
in one place within the query-router service.

Return contract: ``(rows, bytes_processed, columns)``
  - rows:            list of dicts keyed by column name
  - bytes_processed: integer bytes billed (meaningful only for BigQuery;
                     zero for all other connectors)
  - columns:         list of column name strings in result order
"""
from __future__ import annotations

from typing import Any

# F-027-11: the routed-SQL audit marker is applied centrally here so that
# EVERY dialect executor emits it, not just PostgreSQL. The marker lets a
# SOURCE_AUDIT log line distinguish SQL that came through the sanctioned
# bind -> route -> execute pipeline from any unrouted query reaching the
# source. A leading ``/* ... */`` block comment is standard SQL accepted by
# PostgreSQL/Redshift, BigQuery, Snowflake, Spark SQL and SQL Server, so the
# marker is dialect-neutral and never alters the parsed statement.
_ROUTED_MARKER = "/* tessallite:routed */"


def _tag_routed(sql: str) -> str:
    """Prefix ``sql`` with the routed-SQL audit marker (idempotent)."""
    if sql.lstrip().startswith(_ROUTED_MARKER):
        return sql
    return f"{_ROUTED_MARKER} {sql}"


async def execute_on_connection(
    sql: str,
    conn: Any,  # ProjectConnection ORM object
    db: Any,    # AsyncSession — required for PostgreSQL host resolution
) -> tuple[list[dict], int, list[str]]:
    """Dispatch SQL execution to the appropriate connector executor.

    Parameters
    ----------
    sql:
        Fully-rewritten SQL ready to execute against the source database.
    conn:
        ``ProjectConnection`` ORM object carrying ``connection_type`` and
        encrypted credentials.
    db:
        Async SQLAlchemy session used by the PostgreSQL executor for fallback
        host resolution.  Ignored by non-PostgreSQL executors.

    Returns
    -------
    tuple[list[dict], int, list[str]]
        ``(rows, bytes_processed, column_names)``.
    """
    from shared.schemas.connection_type import normalize_connection_type

    connector_type = normalize_connection_type(conn.connection_type.lower())

    # F-027-11: tag centrally so every dialect emits the routed marker.
    # PostgresExecutor._tag is idempotent, so the historical PG-side tag is
    # a no-op once the SQL is already marked here.
    sql = _tag_routed(sql)

    if connector_type == "bigquery":
        import asyncio
        from src.execution.bigquery_executor import BigQueryExecutor

        executor = BigQueryExecutor(conn)
        try:
            rows, bytes_processed, columns = await asyncio.to_thread(
                executor.execute, sql
            )
        finally:
            executor.close()
        return rows, bytes_processed, columns

    if connector_type in ("postgresql", "redshift"):
        from src.execution.postgres_executor import PostgresExecutor

        executor = await PostgresExecutor.create(conn, tenant_session=db)
        rows, bytes_processed, columns = await executor.execute(sql)
        return rows, bytes_processed, columns

    if connector_type == "hadoop_spark":
        from src.execution.spark_executor import SparkExecutor

        executor = SparkExecutor(conn)
        rows, bytes_processed, columns = await executor.execute(sql)
        return rows, bytes_processed, columns

    if connector_type == "snowflake":
        import asyncio
        from src.execution.snowflake_executor import SnowflakeExecutor

        executor = SnowflakeExecutor(conn)
        rows, bytes_processed, columns = await executor.execute(sql)
        return rows, bytes_processed, columns

    if connector_type == "sqlserver":
        from src.execution.sqlserver_executor import SqlServerExecutor

        executor = SqlServerExecutor(conn)
        rows, bytes_processed, columns = await executor.execute(sql)
        return rows, bytes_processed, columns

    raise ValueError(f"Unsupported connector type: {connector_type!r}")
