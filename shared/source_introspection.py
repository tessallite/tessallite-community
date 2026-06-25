"""Connector-agnostic source database introspection.

Schema discovery, column discovery, table profiling, and connection testing
are inherently connector-specific (PG uses information_schema, BQ uses
client APIs, Spark uses SHOW/DESCRIBE). This module centralises dispatch so
calling code never branches on connector type.

Supported connectors: ``postgresql``, ``bigquery``, ``hadoop_spark``,
``redshift``, ``snowflake``, ``sqlserver``.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from shared.config.source_db import (
    resolve_source_db_endpoint,
    resolve_spark_thrift_defaults,
)
from shared.connector_qualify import (
    extract_dataset,
    qualify_table_name,
    quote_identifier,
)
from shared.schemas.connection_type import normalize_connection_type
from shared.source_executor import (
    _audit_log,
    _build_sqlserver_dsn,
    _get_source_timeout,
    _open_snowflake,
)

logger = logging.getLogger(__name__)


def _decrypt(encrypted: bytes) -> dict:
    from shared.security.credential_crypto import decrypt_json
    return decrypt_json(encrypted)


def _resolve_connector(conn_obj: Any) -> str:
    return normalize_connection_type((conn_obj.connection_type or "").lower()) or "unknown"


def _require_spark_host(creds: dict) -> str:
    """F-014-14: a Spark connection missing ``host`` previously surfaced a bare
    ``KeyError`` whose entire detail was ``"'host'"``. Fail with a clear,
    actionable message instead."""
    host = creds.get("host")
    if not host:
        raise ValueError(
            "Spark/Hive connection is missing a host. Set the Thrift server "
            "host in the connection credentials."
        )
    return host


async def _bounded(run_fn):
    """F-014-08: run a blocking introspection callable on a worker thread with
    a hard timeout, matching the ``asyncio.wait_for`` discipline already used in
    ``source_executor``. A firewalled/black-holed host (or a giant table scan)
    no longer hangs the request worker indefinitely.

    Returns whatever ``run_fn`` returns. Raises ``asyncio.TimeoutError`` on
    expiry (mapped to a failure detail by the test path / a 502 by the
    discover/profile paths)."""
    return await asyncio.wait_for(
        asyncio.to_thread(run_fn), timeout=_get_source_timeout(),
    )


# ---------------------------------------------------------------------------
# Connection testing
# ---------------------------------------------------------------------------

async def _test_pg(creds, config, *, tenant_session=None, project_id=None):
    from shared.source_pool import acquire_source_connection
    host, port, database = await resolve_source_db_endpoint(
        creds, config, tenant_session=tenant_session, project_id=project_id,
    )
    user = creds.get("user") or creds.get("username", "postgres")
    password = creds.get("password", "")
    _audit_log("introspect.test", "postgresql SELECT 1")
    async with acquire_source_connection(host, port, database, user, password) as conn:
        await conn.fetch("SELECT 1")


async def _test_bq(creds, config):
    def _run():
        from google.cloud import bigquery
        sa_info = creds.get("service_account_json", creds)
        if isinstance(sa_info, str):
            sa_info = json.loads(sa_info)
        client = bigquery.Client.from_service_account_info(
            sa_info,
            location=(config or {}).get("location") or (creds or {}).get("location") or None,
        )
        list(client.list_datasets(max_results=1))
        client.close()
    _audit_log("introspect.test", "bigquery SELECT datasets[1]")
    await _bounded(_run)


async def _test_spark(creds, config, *, tenant_session=None):
    host = _require_spark_host(creds)

    def _run():
        from pyhive import hive  # type: ignore
        conn = hive.connect(
            host=host,
            port=int(creds.get("port", spark_defaults["port"])),
            database=creds.get("database", spark_defaults["database"]),
            username=creds.get("username", ""),
            password=creds.get("password", ""),
            auth=creds.get("auth_method", spark_defaults["auth_mode"]),
        )
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        conn.close()

    spark_defaults = await resolve_spark_thrift_defaults(tenant_session=tenant_session)
    _audit_log("introspect.test", "hadoop_spark SELECT 1")
    await _bounded(_run)


async def _test_snowflake(creds, config):
    timeout_s = _get_source_timeout()

    def _run():
        conn = _open_snowflake(
            creds, config, login_timeout=timeout_s, network_timeout=timeout_s,
        )
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
        finally:
            conn.close()
    _audit_log("introspect.test", "snowflake SELECT 1")
    await _bounded(_run)


async def _test_sqlserver(creds, config):
    import aioodbc
    dsn = _build_sqlserver_dsn(creds, config)
    _audit_log("introspect.test", "sqlserver SELECT 1")

    async def _run():
        conn = await aioodbc.connect(dsn=dsn)
        try:
            cursor = await conn.cursor()
            await cursor.execute("SELECT 1")
            await cursor.close()
        finally:
            await conn.close()

    await asyncio.wait_for(_run(), timeout=_get_source_timeout())


_TEST_DISPATCH = {
    "postgresql": _test_pg,
    "bigquery": _test_bq,
    "hadoop_spark": _test_spark,
    "redshift": _test_pg,
    "snowflake": _test_snowflake,
    "sqlserver": _test_sqlserver,
}


async def test_connection(
    conn_obj: Any,
    *,
    tenant_session: Any = None,
) -> tuple[bool, str | None]:
    """Test connectivity using a ``ProjectConnection`` object.

    Returns ``(ok, error_detail)``. Error detail is ``None`` on success.
    """
    connector = _resolve_connector(conn_obj)
    creds = _decrypt(conn_obj.encrypted_credentials)
    config = conn_obj.config or {}
    return await test_connection_raw(
        connector, creds, config,
        tenant_session=tenant_session,
        project_id=getattr(conn_obj, "project_id", None),
    )


async def test_connection_raw(
    connector: str,
    creds: dict,
    config: dict,
    *,
    tenant_session: Any = None,
    project_id: Any = None,
) -> tuple[bool, str | None]:
    """Test connectivity using raw (already-decrypted) credentials.

    Returns ``(ok, error_detail)``. Error detail is ``None`` on success.
    """
    connector = normalize_connection_type(connector) or connector
    fn = _TEST_DISPATCH.get(connector)
    if fn is None:
        return False, f"Unsupported connection type: {connector}"

    try:
        if connector in ("postgresql", "redshift"):
            await fn(creds, config, tenant_session=tenant_session,
                     project_id=project_id)
        elif connector == "hadoop_spark":
            await fn(creds, config, tenant_session=tenant_session)
        else:
            await fn(creds, config)
    except asyncio.TimeoutError:
        # F-014-08: a firewalled/black-holed host no longer hangs the worker —
        # the bounded introspection call raises TimeoutError; give the user a
        # clear message rather than the empty string a bare TimeoutError carries.
        return False, (
            f"Connection test timed out after {_get_source_timeout()}s — the "
            "host may be unreachable or blocked by a firewall."
        )
    except Exception as exc:
        return False, str(exc)

    return True, None


# ---------------------------------------------------------------------------
# Discover tables
# ---------------------------------------------------------------------------

async def _discover_tables_pg(creds, config, *, schema=None,
                               tenant_session=None, project_id=None):
    from shared.source_pool import acquire_source_connection
    host, port, database = await resolve_source_db_endpoint(
        creds, config, tenant_session=tenant_session, project_id=project_id,
    )
    user = creds.get("user") or creds.get("username", "postgres")
    password = creds.get("password", "")
    _audit_log("introspect.discover_tables", f"postgresql schema={schema}")
    async with acquire_source_connection(host, port, database, user, password) as conn:
        sql = (
            "SELECT table_schema, table_name, table_type "
            "FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
        )
        args = []
        if schema:
            sql += "AND table_schema = $1 "
            args.append(schema)
        sql += "ORDER BY table_schema, table_name LIMIT 500"
        rows = await conn.fetch(sql, *args)
        return [
            {"schema": r["table_schema"], "table": r["table_name"], "type": r["table_type"]}
            for r in rows
        ]


async def _discover_tables_bq(creds, config, *, schema=None, **_kw):
    def _run():
        from google.cloud import bigquery
        sa_info = creds.get("service_account_json", creds)
        if isinstance(sa_info, str):
            sa_info = json.loads(sa_info)
        client = bigquery.Client.from_service_account_info(
            sa_info,
            location=(config or {}).get("location") or (creds or {}).get("location") or None,
        )
        project = creds.get("project_id", config.get("project_id"))
        tables = []
        if schema:
            bq_dataset = extract_dataset("bigquery", schema)
            datasets = [client.dataset(bq_dataset, project=project)]
        else:
            datasets = list(client.list_datasets(project=project, max_results=50))
        for ds_ref in datasets:
            ds_id = ds_ref.dataset_id if hasattr(ds_ref, "dataset_id") else str(ds_ref)
            for tbl in client.list_tables(f"{project}.{ds_id}", max_results=200):
                tables.append({
                    "schema": ds_id,
                    "table": tbl.table_id,
                    "type": tbl.table_type,
                })
        client.close()
        return tables
    _audit_log("introspect.discover_tables", f"bigquery schema={schema}")
    return await _bounded(_run)


async def _discover_tables_spark(creds, config, *, schema=None,
                                  tenant_session=None, **_kw):
    host = _require_spark_host(creds)
    spark_defaults = await resolve_spark_thrift_defaults(tenant_session=tenant_session)

    def _run():
        from pyhive import hive  # type: ignore
        conn = hive.connect(
            host=host,
            port=int(creds.get("port", spark_defaults["port"])),
            database=creds.get("database", spark_defaults["database"]),
            username=creds.get("username", ""),
            password=creds.get("password", ""),
            auth=creds.get("auth_method", spark_defaults["auth_mode"]),
        )
        cursor = conn.cursor()
        db_name = schema or creds.get("database", spark_defaults["database"])
        from shared.connector_qualify import quote_identifier
        cursor.execute(f"SHOW TABLES IN {quote_identifier('hadoop_spark', db_name)}")
        tables = []
        for row in cursor.fetchall():
            tables.append({
                "schema": db_name,
                "table": row[0] if len(row) == 1 else row[1],
                "type": "BASE TABLE",
            })
        conn.close()
        return tables
    _audit_log("introspect.discover_tables", f"hadoop_spark schema={schema}")
    return await _bounded(_run)


async def _discover_tables_snowflake(creds, config, *, schema=None, **_kw):
    def _run():
        conn = _open_snowflake(
            creds, config, login_timeout=_get_source_timeout(),
            network_timeout=_get_source_timeout(),
        )
        try:
            cursor = conn.cursor()
            if schema:
                cursor.execute(
                    "SELECT table_schema, table_name, table_type "
                    "FROM information_schema.tables "
                    "WHERE table_schema NOT IN ('INFORMATION_SCHEMA') "
                    "AND table_schema = %s "
                    "ORDER BY table_schema, table_name LIMIT 500",
                    (schema,),
                )
            else:
                cursor.execute(
                    "SELECT table_schema, table_name, table_type "
                    "FROM information_schema.tables "
                    "WHERE table_schema NOT IN ('INFORMATION_SCHEMA') "
                    "ORDER BY table_schema, table_name LIMIT 500"
                )
            tables = [
                {"schema": row[0], "table": row[1], "type": row[2]}
                for row in cursor.fetchall()
            ]
            cursor.close()
            return tables
        finally:
            conn.close()
    _audit_log("introspect.discover_tables", f"snowflake schema={schema}")
    return await _bounded(_run)


async def _discover_tables_sqlserver(creds, config, *, schema=None, **_kw):
    import aioodbc
    dsn = _build_sqlserver_dsn(creds, config)
    _audit_log("introspect.discover_tables", f"sqlserver schema={schema}")

    async def _run():
        conn = await aioodbc.connect(dsn=dsn)
        try:
            cursor = await conn.cursor()
            if schema:
                await cursor.execute(
                    "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE "
                    "FROM INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_SCHEMA = ? "
                    "ORDER BY TABLE_SCHEMA, TABLE_NAME",
                    (schema,),
                )
            else:
                await cursor.execute(
                    "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE "
                    "FROM INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA') "
                    "ORDER BY TABLE_SCHEMA, TABLE_NAME"
                )
            tables = [
                {"schema": row[0], "table": row[1], "type": row[2]}
                for row in await cursor.fetchall()
            ]
            await cursor.close()
            return tables
        finally:
            await conn.close()

    return await asyncio.wait_for(_run(), timeout=_get_source_timeout())


_DISCOVER_TABLES_DISPATCH = {
    "postgresql": _discover_tables_pg,
    "bigquery": _discover_tables_bq,
    "hadoop_spark": _discover_tables_spark,
    "redshift": _discover_tables_pg,
    "snowflake": _discover_tables_snowflake,
    "sqlserver": _discover_tables_sqlserver,
}


async def discover_tables(
    conn_obj: Any,
    *,
    schema: str | None = None,
    tenant_session: Any = None,
) -> list[dict]:
    """Return tables from the source database.

    Each entry: ``{"schema": str, "table": str, "type": str}``.
    """
    connector = _resolve_connector(conn_obj)
    fn = _DISCOVER_TABLES_DISPATCH.get(connector)
    if fn is None:
        raise ValueError(f"Unsupported connector type: {connector!r}")

    creds = _decrypt(conn_obj.encrypted_credentials)
    config = conn_obj.config or {}
    return await fn(
        creds, config,
        schema=schema,
        tenant_session=tenant_session,
        project_id=getattr(conn_obj, "project_id", None),
    )


# ---------------------------------------------------------------------------
# Discover columns
# ---------------------------------------------------------------------------

async def _discover_columns_pg(creds, config, *, schema, table,
                                tenant_session=None, project_id=None):
    from shared.source_pool import acquire_source_connection
    host, port, database = await resolve_source_db_endpoint(
        creds, config, tenant_session=tenant_session, project_id=project_id,
    )
    user = creds.get("user") or creds.get("username", "postgres")
    password = creds.get("password", "")
    _audit_log("introspect.discover_columns", f"postgresql {schema}.{table}")
    async with acquire_source_connection(host, port, database, user, password) as conn:
        rows = await conn.fetch(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = $1 AND table_name = $2 "
            "ORDER BY ordinal_position",
            schema, table,
        )
        return [
            {
                "column_name": r["column_name"],
                "data_type": r["data_type"],
                "is_nullable": r["is_nullable"] == "YES",
            }
            for r in rows
        ]


async def _discover_columns_bq(creds, config, *, schema, table, **_kw):
    def _run():
        from google.cloud import bigquery
        sa_info = creds.get("service_account_json", creds)
        if isinstance(sa_info, str):
            sa_info = json.loads(sa_info)
        client = bigquery.Client.from_service_account_info(
            sa_info,
            location=(config or {}).get("location") or (creds or {}).get("location") or None,
        )
        project = creds.get("project_id", config.get("project_id"))
        table_ref = qualify_table_name("bigquery", table, schema=schema, project_id=project)
        bq_table = client.get_table(table_ref)
        columns = [
            {
                "column_name": f.name,
                "data_type": f.field_type,
                "is_nullable": f.mode != "REQUIRED",
            }
            for f in bq_table.schema
        ]
        client.close()
        return columns
    _audit_log("introspect.discover_columns", f"bigquery {schema}.{table}")
    return await _bounded(_run)


async def _discover_columns_spark(creds, config, *, schema, table,
                                   tenant_session=None, **_kw):
    host = _require_spark_host(creds)
    spark_defaults = await resolve_spark_thrift_defaults(tenant_session=tenant_session)

    def _run():
        from pyhive import hive  # type: ignore
        conn = hive.connect(
            host=host,
            port=int(creds.get("port", spark_defaults["port"])),
            database=creds.get("database", spark_defaults["database"]),
            username=creds.get("username", ""),
            password=creds.get("password", ""),
            auth=creds.get("auth_method", spark_defaults["auth_mode"]),
        )
        cursor = conn.cursor()
        from shared.connector_qualify import quote_table_ref
        cursor.execute(f"DESCRIBE {quote_table_ref('hadoop_spark', f'{schema}.{table}')}")
        columns = []
        for row in cursor.fetchall():
            col_name = row[0]
            if col_name.startswith("#") or not col_name.strip():
                continue
            columns.append({
                "column_name": col_name,
                "data_type": row[1] if len(row) > 1 else "STRING",
                "is_nullable": True,
            })
        conn.close()
        return columns
    _audit_log("introspect.discover_columns", f"hadoop_spark {schema}.{table}")
    return await _bounded(_run)


async def _discover_columns_snowflake(creds, config, *, schema, table, **_kw):
    def _run():
        conn = _open_snowflake(
            creds, config, login_timeout=_get_source_timeout(),
            network_timeout=_get_source_timeout(),
        )
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s "
                "ORDER BY ordinal_position",
                (schema, table),
            )
            columns = [
                {
                    "column_name": row[0],
                    "data_type": row[1],
                    "is_nullable": row[2] == "YES",
                }
                for row in cursor.fetchall()
            ]
            cursor.close()
            return columns
        finally:
            conn.close()
    _audit_log("introspect.discover_columns", f"snowflake {schema}.{table}")
    return await _bounded(_run)


async def _discover_columns_sqlserver(creds, config, *, schema, table, **_kw):
    import aioodbc
    dsn = _build_sqlserver_dsn(creds, config)
    _audit_log("introspect.discover_columns", f"sqlserver {schema}.{table}")

    async def _run():
        conn = await aioodbc.connect(dsn=dsn)
        try:
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE "
                "FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? "
                "ORDER BY ORDINAL_POSITION",
                (schema, table),
            )
            columns = [
                {
                    "column_name": row[0],
                    "data_type": row[1],
                    "is_nullable": row[2] == "YES",
                }
                for row in await cursor.fetchall()
            ]
            await cursor.close()
            return columns
        finally:
            await conn.close()

    return await asyncio.wait_for(_run(), timeout=_get_source_timeout())


_DISCOVER_COLUMNS_DISPATCH = {
    "postgresql": _discover_columns_pg,
    "bigquery": _discover_columns_bq,
    "hadoop_spark": _discover_columns_spark,
    "redshift": _discover_columns_pg,
    "snowflake": _discover_columns_snowflake,
    "sqlserver": _discover_columns_sqlserver,
}


async def discover_columns(
    conn_obj: Any,
    *,
    schema: str,
    table: str,
    tenant_session: Any = None,
) -> list[dict]:
    """Return columns for a single table.

    Each entry: ``{"column_name": str, "data_type": str, "is_nullable": bool}``.
    """
    connector = _resolve_connector(conn_obj)
    fn = _DISCOVER_COLUMNS_DISPATCH.get(connector)
    if fn is None:
        raise ValueError(f"Unsupported connector type: {connector!r}")

    creds = _decrypt(conn_obj.encrypted_credentials)
    config = conn_obj.config or {}
    return await fn(
        creds, config,
        schema=schema,
        table=table,
        tenant_session=tenant_session,
        project_id=getattr(conn_obj, "project_id", None),
    )


# ---------------------------------------------------------------------------
# Profile table (columns + row count + optional cardinality)
# ---------------------------------------------------------------------------

async def _profile_pg(creds, config, *, schema, table,
                       tenant_session=None, project_id=None):
    from shared.source_pool import acquire_source_connection
    host, port, database = await resolve_source_db_endpoint(
        creds, config, tenant_session=tenant_session, project_id=project_id,
    )
    user = creds.get("user") or creds.get("username", "postgres")
    password = creds.get("password", "")
    _audit_log("introspect.profile", f"postgresql {schema}.{table}")
    async with acquire_source_connection(host, port, database, user, password) as conn:
        rows = await conn.fetch(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = $1 AND table_name = $2 "
            "ORDER BY ordinal_position",
            schema, table,
        )
        columns = [
            {
                "column_name": r["column_name"],
                "data_type": r["data_type"],
                "is_nullable": r["is_nullable"] == "YES",
                "approx_distinct": None,
            }
            for r in rows
        ]
        row_count = await _cardinality_pg(conn, schema, table, columns)
        return columns, row_count


def _to_int(val) -> int:
    if val is None:
        return 0
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return 0


def _build_cardinality_sql(connector: str, schema: str, table: str,
                           columns: list[dict]) -> tuple[str, list[dict]]:
    """Build ``SELECT COUNT(*), COUNT(DISTINCT col)... FROM schema.table``.

    F-014-02: ``approx_distinct`` was only computed for PostgreSQL/Redshift, so
    every cardinality-based auto-classify signal silently no-op'd on BigQuery,
    Snowflake, SQL Server, and Spark. This builds the same canonical
    ``COUNT(DISTINCT)`` probe for any connector, quoting identifiers via
    ``connector_qualify`` (no per-connector branching here — only quoting differs).

    Returns ``(sql, sampled_columns)`` where ``sampled_columns`` is the (≤40)
    slice whose distinct counts the SELECT returns, in column order after the
    leading ``COUNT(*)``.

    Naming note (F-014-13): the per-column result is stored on the column dict
    under the key ``approx_distinct`` for backward compatibility, but it is an
    **exact** ``COUNT(DISTINCT col)``, not an approximation. On a wide, large
    table this is a full scan across up to 40 columns in a single statement,
    bounded by the source statement timeout.
    """
    from shared.connector_qualify import quote_table_ref
    sample = columns[:40]
    parts = ["COUNT(*) AS _row_count"]
    for i, col in enumerate(sample):
        qi = quote_identifier(connector, col["column_name"])
        parts.append(f"COUNT(DISTINCT {qi}) AS _cd_{i}")
    qualified = quote_table_ref(connector, f"{schema}.{table}")
    sql = f'SELECT {", ".join(parts)} FROM {qualified}'
    return sql, sample


def _apply_cardinality_row(row, sampled: list[dict]) -> int:
    """Populate ``approx_distinct`` on *sampled* from a cardinality result row.

    *row* is the single result row: ``[row_count, cd_0, cd_1, ...]`` (sequence
    indexable by position — asyncpg Record, DB-API tuple, or list all qualify).
    """
    if row is None:
        return 0
    row_count = _to_int(row[0])
    for i, col in enumerate(sampled):
        col["approx_distinct"] = _to_int(row[i + 1])
    return row_count


async def _cardinality_pg(conn, schema: str, table: str, columns: list[dict]) -> int:
    """Query COUNT(*) and COUNT(DISTINCT col) for up to 40 columns."""
    if not columns:
        return 0
    sql, sample = _build_cardinality_sql("postgresql", schema, table, columns)
    row = await conn.fetchrow(sql)
    return _apply_cardinality_row(row, sample)


async def _profile_bq(creds, config, *, schema, table, **_kw):
    def _run():
        from google.cloud import bigquery
        sa_info = creds.get("service_account_json", creds)
        if isinstance(sa_info, str):
            sa_info = json.loads(sa_info)
        client = bigquery.Client.from_service_account_info(
            sa_info,
            location=(config or {}).get("location") or (creds or {}).get("location") or None,
        )
        project = creds.get("project_id", config.get("project_id"))
        if "." in table:
            table_ref = f"{schema}.{table}"
        else:
            table_ref = qualify_table_name("bigquery", table, schema=schema, project_id=project)
        bq_table = client.get_table(table_ref)
        row_count = bq_table.num_rows or 0
        columns = [
            {
                "column_name": f.name,
                "data_type": f.field_type.lower(),
                "is_nullable": f.mode != "REQUIRED",
                "approx_distinct": None,
            }
            for f in bq_table.schema
        ]
        # F-014-02: probe per-column cardinality so the auto-classify signals work.
        try:
            sql, sampled = _build_cardinality_sql("bigquery", schema, table, columns)
            result = list(client.query(sql).result())
            if result:
                row_count = _apply_cardinality_row(result[0], sampled)
        except Exception as exc:  # noqa: BLE001 — cardinality is best-effort
            logger.warning("BigQuery cardinality probe failed for %s.%s: %s",
                           schema, table, exc)
        client.close()
        return columns, row_count
    _audit_log("introspect.profile", f"bigquery {schema}.{table}")
    return await _bounded(_run)


async def _profile_spark(creds, config, *, schema, table,
                          tenant_session=None, **_kw):
    host = _require_spark_host(creds)
    spark_defaults = await resolve_spark_thrift_defaults(tenant_session=tenant_session)

    def _run():
        from pyhive import hive  # type: ignore
        conn = hive.connect(
            host=host,
            port=int(creds.get("port", spark_defaults["port"])),
            database=creds.get("database", spark_defaults["database"]),
            username=creds.get("username", ""),
            password=creds.get("password", ""),
            auth=creds.get("auth_method", spark_defaults["auth_mode"]),
        )
        cursor = conn.cursor()
        from shared.connector_qualify import quote_table_ref
        cursor.execute(f"DESCRIBE {quote_table_ref('hadoop_spark', f'{schema}.{table}')}")
        columns = []
        for row in cursor.fetchall():
            col_name = row[0]
            if col_name.startswith("#") or not col_name.strip():
                continue
            columns.append({
                "column_name": col_name,
                "data_type": (row[1] if len(row) > 1 else "string").lower(),
                "is_nullable": True,
                "approx_distinct": None,
            })
        row_count = 0
        # F-014-02: probe per-column cardinality (subsumes the bare COUNT(*)).
        try:
            sql, sampled = _build_cardinality_sql("hadoop_spark", schema, table, columns)
            cursor.execute(sql)
            row_count = _apply_cardinality_row(cursor.fetchone(), sampled)
        except Exception as exc:  # noqa: BLE001 — cardinality is best-effort
            logger.warning("Spark cardinality probe failed for %s.%s: %s",
                           schema, table, exc)
            try:
                cursor.execute(
                    f"SELECT COUNT(*) FROM {quote_table_ref('hadoop_spark', f'{schema}.{table}')}"
                )
                row_count = _to_int(cursor.fetchone()[0])
            except Exception:
                pass
        conn.close()
        return columns, row_count
    _audit_log("introspect.profile", f"hadoop_spark {schema}.{table}")
    return await _bounded(_run)


async def _profile_snowflake(creds, config, *, schema, table, **_kw):
    def _run():
        conn = _open_snowflake(
            creds, config, login_timeout=_get_source_timeout(),
            network_timeout=_get_source_timeout(),
        )
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s "
                "ORDER BY ordinal_position",
                (schema, table),
            )
            columns = [
                {
                    "column_name": row[0],
                    "data_type": row[1].lower(),
                    "is_nullable": row[2] == "YES",
                    "approx_distinct": None,
                }
                for row in cursor.fetchall()
            ]
            row_count = 0
            # F-014-02: probe per-column cardinality (subsumes the bare COUNT(*)).
            try:
                sql, sampled = _build_cardinality_sql("snowflake", schema, table, columns)
                cursor.execute(sql)
                row_count = _apply_cardinality_row(cursor.fetchone(), sampled)
            except Exception as exc:  # noqa: BLE001 — cardinality is best-effort
                logger.warning("Snowflake cardinality probe failed for %s.%s: %s",
                               schema, table, exc)
                try:
                    from shared.connector_qualify import quote_table_ref
                    qualified = quote_table_ref("snowflake", f"{schema}.{table}")
                    cursor.execute(f"SELECT COUNT(*) FROM {qualified}")
                    result = cursor.fetchone()
                    row_count = _to_int(result[0]) if result else 0
                except Exception:
                    pass
            cursor.close()
            return columns, row_count
        finally:
            conn.close()
    _audit_log("introspect.profile", f"snowflake {schema}.{table}")
    return await _bounded(_run)


async def _profile_sqlserver(creds, config, *, schema, table, **_kw):
    import aioodbc
    from shared.connector_qualify import quote_table_ref

    dsn = _build_sqlserver_dsn(creds, config)
    _audit_log("introspect.profile", f"sqlserver {schema}.{table}")

    async def _run():
        conn = await aioodbc.connect(dsn=dsn)
        try:
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE "
                "FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? "
                "ORDER BY ORDINAL_POSITION",
                (schema, table),
            )
            columns = [
                {
                    "column_name": row[0],
                    "data_type": row[1].lower(),
                    "is_nullable": row[2] == "YES",
                    "approx_distinct": None,
                }
                for row in await cursor.fetchall()
            ]
            row_count = 0
            # F-014-02: probe per-column cardinality (subsumes the bare COUNT(*)).
            try:
                sql, sampled = _build_cardinality_sql("sqlserver", schema, table, columns)
                await cursor.execute(sql)
                row_count = _apply_cardinality_row(await cursor.fetchone(), sampled)
            except Exception as exc:  # noqa: BLE001 — cardinality is best-effort
                logger.warning("SQL Server cardinality probe failed for %s.%s: %s",
                               schema, table, exc)
                try:
                    qualified = quote_table_ref("sqlserver", f"{schema}.{table}")
                    await cursor.execute(f"SELECT COUNT(*) FROM {qualified}")
                    result = await cursor.fetchone()
                    row_count = _to_int(result[0]) if result else 0
                except Exception:
                    pass
            await cursor.close()
            return columns, row_count
        finally:
            await conn.close()

    return await asyncio.wait_for(_run(), timeout=_get_source_timeout())


_PROFILE_DISPATCH = {
    "postgresql": _profile_pg,
    "bigquery": _profile_bq,
    "hadoop_spark": _profile_spark,
    "redshift": _profile_pg,
    "snowflake": _profile_snowflake,
    "sqlserver": _profile_sqlserver,
}


async def profile_table(
    conn_obj: Any,
    *,
    schema: str,
    table: str,
    tenant_session: Any = None,
) -> tuple[list[dict], int]:
    """Return column metadata and row count for a single table.

    Returns ``(columns, row_count)``. Each column entry includes
    ``approx_distinct`` (may be ``None`` if cardinality wasn't fetched).
    """
    connector = _resolve_connector(conn_obj)
    fn = _PROFILE_DISPATCH.get(connector)
    if fn is None:
        raise ValueError(f"Unsupported connector type: {connector!r}")

    creds = _decrypt(conn_obj.encrypted_credentials)
    config = conn_obj.config or {}
    return await fn(
        creds, config,
        schema=schema,
        table=table,
        tenant_session=tenant_session,
        project_id=getattr(conn_obj, "project_id", None),
    )
