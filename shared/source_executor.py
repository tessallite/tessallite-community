"""Connector-agnostic source-database executor.

Every site that needs to run raw SQL against a tenant's source database
should use ``execute_source_sql`` instead of building a connector-specific
code path.  Connector dispatch happens once here; callers never branch on
connector type.

Supported connectors: ``postgresql``, ``bigquery``, ``hadoop_spark``,
``redshift``, ``snowflake``, ``sqlserver``.

Redshift is PostgreSQL wire-compatible; queries and DDL use the same
asyncpg executor as PostgreSQL.

SQL Server uses ``aioodbc`` (async wrapper over pyodbc/ODBC) with bracket
identifier quoting (``[identifier]``).
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from shared.config.source_db import resolve_source_db_endpoint
from shared.schemas.connection_type import normalize_connection_type

logger = logging.getLogger(__name__)

# F-030-22: source-access audit tracing defaults ON. This is the bypass-detection
# control the architecture relies on ("source access is traced via [SOURCE_AUDIT]");
# leaving it opt-in meant it produced nothing unless an operator set the env var,
# so the control was effectively absent by default. An explicit
# TESSALLITE_SOURCE_AUDIT="0" is now required to silence it.
_SOURCE_AUDIT = os.environ.get("TESSALLITE_SOURCE_AUDIT", "1") != "0"


class QueryTimeoutError(Exception):
    """Raised when a source-database query exceeds the configured timeout."""
    pass


def _open_snowflake(
    creds: dict,
    config: dict,
    *,
    login_timeout: int | None = None,
    network_timeout: int | None = None,
):
    """Open a Snowflake connection from decrypted credentials and config."""
    import snowflake.connector  # type: ignore[import-untyped]

    kwargs: dict[str, Any] = dict(
        account=creds.get("account", ""),
        user=creds.get("username", ""),
        password=creds.get("password", ""),
        database=creds.get("database", config.get("database", "")),
        schema=creds.get("schema", config.get("schema", "")),
        warehouse=creds.get("warehouse", config.get("warehouse", "")),
        role=creds.get("role", config.get("role", "")),
    )
    if login_timeout is not None:
        kwargs["login_timeout"] = login_timeout
    if network_timeout is not None:
        kwargs["network_timeout"] = network_timeout
    return snowflake.connector.connect(**kwargs)


def _escape_odbc_value(val: str) -> str:
    """Escape a value for ODBC connection string: wrap in braces if it
    contains semicolons, braces, or equals signs."""
    if any(c in val for c in (";", "=", "{", "}")):
        return "{" + val.replace("}", "}}") + "}"
    return val


def _build_sqlserver_dsn(creds: dict, config: dict) -> str:
    """Build an ODBC connection string for SQL Server."""
    driver = (
        creds.get("driver")
        or config.get("driver")
        or "ODBC Driver 17 for SQL Server"
    )
    if ";" in driver or "\x00" in driver:
        raise ValueError(f"Invalid ODBC driver name: {driver!r}")
    host = creds.get("host", "localhost")
    port = int(creds.get("port", 1433))
    database = creds.get("database") or config.get("database", "")
    user = creds.get("username") or creds.get("user", "")
    password = creds.get("password", "")

    safe_driver = driver.replace("}", "}}")
    parts = [
        f"DRIVER={{{safe_driver}}}",
        f"SERVER={_escape_odbc_value(host)},{port}",
        f"DATABASE={_escape_odbc_value(database)}",
        f"UID={_escape_odbc_value(user)}",
        f"PWD={_escape_odbc_value(password)}",
    ]
    encrypt = config.get("encrypt")
    if encrypt is not None:
        parts.append(f"Encrypt={'yes' if encrypt else 'no'}")
    trust_cert = config.get("trust_server_certificate")
    if trust_cert is not None:
        parts.append(f"TrustServerCertificate={'yes' if trust_cert else 'no'}")
    return ";".join(parts)


def _audit_log(operation: str, sql: str) -> None:
    if not _SOURCE_AUDIT:
        return
    caller = ""
    frame = inspect.currentframe()
    if frame and frame.f_back and frame.f_back.f_back:
        f = frame.f_back.f_back
        caller = f"{f.f_globals.get('__name__', '?')}:{f.f_lineno}"
    sql_prefix = sql[:80].replace("\n", " ")
    logger.info("[SOURCE_AUDIT] %s caller=%s sql=%s", operation, caller, sql_prefix)


def _decrypt(encrypted: bytes) -> dict:
    from shared.security.credential_crypto import decrypt_json
    return decrypt_json(encrypted)


def _get_source_timeout() -> int:
    """Return the source statement timeout in seconds from config snapshot."""
    try:
        from shared.config.bootstrap import system_snapshot_get
        return int(system_snapshot_get("query.statement_timeout_seconds"))
    except Exception:
        return 120


async def _execute_pg(
    conn_obj: Any,
    sql: str,
    *,
    tenant_session: Any = None,
) -> tuple[list[dict], list[str]]:
    from shared.source_pool import acquire_source_connection
    creds = _decrypt(conn_obj.encrypted_credentials)
    config = conn_obj.config or {}
    host, port, database = await resolve_source_db_endpoint(
        creds, config,
        tenant_session=tenant_session,
        project_id=getattr(conn_obj, "project_id", None),
    )
    user = creds.get("user") or creds.get("username", "postgres")
    password = creds.get("password", "")
    timeout_s = _get_source_timeout()
    async with acquire_source_connection(host, port, database, user, password) as conn:
        await conn.execute(f"SET statement_timeout = {timeout_s * 1000}")
        # Set search_path so unqualified table names resolve to the
        # configured schema (e.g. "demo_data") rather than just "public".
        schema = config.get("schema") or config.get("dataset")
        if schema:
            safe_schema = schema.replace("'", "''")
            await conn.execute(f"SET search_path TO '{safe_schema}', public")
        try:
            records = await conn.fetch(sql)
        except Exception as exc:
            if "canceling statement due to statement timeout" in str(exc):
                raise QueryTimeoutError(
                    f"Source query exceeded {timeout_s}s statement timeout."
                ) from exc
            raise
        columns = list(records[0].keys()) if records else []
        rows = [dict(r) for r in records]
        return rows, columns


async def _execute_bq(conn_obj: Any, sql: str) -> tuple[list[dict], list[str]]:
    timeout_s = _get_source_timeout()

    def _run() -> tuple[list[dict], list[str]]:
        from google.cloud import bigquery
        from google.oauth2 import service_account
        creds = _decrypt(conn_obj.encrypted_credentials)
        sa_info = creds.get("service_account_json", creds)
        if isinstance(sa_info, str):
            sa_info = json.loads(sa_info)
        gc = service_account.Credentials.from_service_account_info(sa_info)
        project_id = (
            (conn_obj.config or {}).get("project_id")
            or creds.get("project_id")
            or sa_info.get("project_id")
        )
        client = bigquery.Client(
            credentials=gc,
            project=project_id,
            location=(conn_obj.config or {}).get("location") or creds.get("location") or None,
        )
        try:
            job = client.query(sql)
            result = job.result(timeout=timeout_s)
            columns = [f.name for f in result.schema]
            rows = [dict(row) for row in result]
            return rows, columns
        finally:
            client.close()

    try:
        return await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout_s + 10)
    except asyncio.TimeoutError as exc:
        raise QueryTimeoutError(
            f"BigQuery source query exceeded {timeout_s}s timeout."
        ) from exc


async def _execute_spark(conn_obj: Any, sql: str) -> tuple[list[dict], list[str]]:
    timeout_s = _get_source_timeout()

    def _run() -> tuple[list[dict], list[str]]:
        from pyhive import hive  # type: ignore
        creds = _decrypt(conn_obj.encrypted_credentials)
        kwargs: dict = {
            "host": creds.get("host", "localhost"),
            "port": int(creds.get("port", 10000)),
            "database": creds.get("database", "default"),
        }
        auth_method = creds.get("auth_method", "NOSASL")
        if auth_method == "LDAP":
            kwargs["auth"] = "LDAP"
            kwargs["username"] = creds.get("username", "")
            kwargs["password"] = creds.get("password", "")
        else:
            kwargs["auth"] = "NOSASL"
        conn = hive.connect(**kwargs)
        try:
            cursor = conn.cursor()
            cursor.execute(sql)
            columns = [desc[0] for desc in (cursor.description or [])]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
            cursor.close()
            return rows, columns
        finally:
            conn.close()

    try:
        return await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        raise QueryTimeoutError(
            f"Spark/Hive source query exceeded {timeout_s}s timeout."
        ) from exc


async def _execute_snowflake(conn_obj: Any, sql: str) -> tuple[list[dict], list[str]]:
    timeout_s = _get_source_timeout()

    def _run() -> tuple[list[dict], list[str]]:
        creds = _decrypt(conn_obj.encrypted_credentials)
        config = conn_obj.config or {}
        conn = _open_snowflake(creds, config, login_timeout=min(timeout_s, 30), network_timeout=timeout_s)
        try:
            cursor = conn.cursor()
            cursor.execute(sql, timeout=timeout_s)
            columns = [desc[0] for desc in (cursor.description or [])]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
            cursor.close()
            return rows, columns
        finally:
            conn.close()

    try:
        return await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout_s + 10)
    except asyncio.TimeoutError as exc:
        raise QueryTimeoutError(
            f"Snowflake source query exceeded {timeout_s}s timeout."
        ) from exc


async def _execute_sqlserver(conn_obj: Any, sql: str) -> tuple[list[dict], list[str]]:
    import aioodbc

    timeout_s = _get_source_timeout()
    creds = _decrypt(conn_obj.encrypted_credentials)
    config = conn_obj.config or {}
    dsn = _build_sqlserver_dsn(creds, config)

    try:
        conn = await asyncio.wait_for(aioodbc.connect(dsn=dsn), timeout=30)
    except asyncio.TimeoutError as exc:
        raise QueryTimeoutError(
            "SQL Server connection timed out after 30s."
        ) from exc
    try:
        cursor = await conn.cursor()
        try:
            await asyncio.wait_for(cursor.execute(sql), timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            raise QueryTimeoutError(
                f"SQL Server source query exceeded {timeout_s}s timeout."
            ) from exc
        columns = [desc[0] for desc in (cursor.description or [])]
        rows = [dict(zip(columns, row)) for row in await cursor.fetchall()]
        await cursor.close()
        return rows, columns
    finally:
        await conn.close()


_DISPATCH = {
    "postgresql": _execute_pg,
    "bigquery": _execute_bq,
    "hadoop_spark": _execute_spark,
    "redshift": _execute_pg,
    "snowflake": _execute_snowflake,
    "sqlserver": _execute_sqlserver,
}


async def execute_source_sql(
    conn_obj: Any,
    sql: str,
    *,
    tenant_session: Any = None,
) -> tuple[list[dict], list[str]]:
    """Execute *sql* against the source database behind *conn_obj*.

    Parameters
    ----------
    conn_obj:
        ``ProjectConnection`` ORM object (needs ``connection_type``,
        ``encrypted_credentials``, ``config``, ``project_id``).
    sql:
        Raw SQL string.  Callers are responsible for using
        ``shared.connector_qualify`` for table/column quoting.
    tenant_session:
        Async SQLAlchemy session (needed by PostgreSQL for fallback host
        resolution; optional for other connectors).

    Returns
    -------
    tuple[list[dict], list[str]]
        ``(rows, column_names)`` where each row is a dict keyed by column name.

    Raises
    ------
    ValueError
        If the connector type is not supported.
    """
    _audit_log("execute_source_sql", sql)
    connector = normalize_connection_type(
        (conn_obj.connection_type or "").lower()
    )
    executor = _DISPATCH.get(connector)  # type: ignore[arg-type]
    if executor is None:
        raise ValueError(f"Unsupported connector type: {connector!r}")

    if connector in ("postgresql", "redshift"):
        return await executor(conn_obj, sql, tenant_session=tenant_session)
    return await executor(conn_obj, sql)


async def resolve_connector_type(conn_obj: Any) -> str:
    """Return the canonical connector type for a ``ProjectConnection``."""
    return normalize_connection_type(
        (conn_obj.connection_type or "").lower()
    ) or "unknown"


# ---------------------------------------------------------------------------
# DDL execution (CREATE TABLE, etc.)
# ---------------------------------------------------------------------------

async def _execute_ddl_pg(
    conn_obj: Any,
    statements: list[str],
    *,
    tenant_session: Any = None,
) -> None:
    from shared.source_pool import acquire_source_connection
    creds = _decrypt(conn_obj.encrypted_credentials)
    config = conn_obj.config or {}
    host, port, database = await resolve_source_db_endpoint(
        creds, config,
        tenant_session=tenant_session,
        project_id=getattr(conn_obj, "project_id", None),
    )
    user = creds.get("user") or creds.get("username", "postgres")
    password = creds.get("password", "")
    async with acquire_source_connection(host, port, database, user, password) as conn:
        for stmt in statements:
            await conn.execute(stmt)


async def _execute_ddl_bq(conn_obj: Any, statements: list[str]) -> None:
    def _run() -> None:
        from google.cloud import bigquery
        from google.oauth2 import service_account
        creds = _decrypt(conn_obj.encrypted_credentials)
        sa_info = creds.get("service_account_json", creds)
        if isinstance(sa_info, str):
            sa_info = json.loads(sa_info)
        gc = service_account.Credentials.from_service_account_info(sa_info)
        project_id = (
            (conn_obj.config or {}).get("project_id")
            or creds.get("project_id")
            or sa_info.get("project_id")
        )
        client = bigquery.Client(
            credentials=gc,
            project=project_id,
            location=(conn_obj.config or {}).get("location") or creds.get("location") or None,
        )
        try:
            for stmt in statements:
                job = client.query(stmt)
                job.result()
        finally:
            client.close()

    await asyncio.to_thread(_run)


async def _execute_ddl_spark(conn_obj: Any, statements: list[str]) -> None:
    def _run() -> None:
        from pyhive import hive  # type: ignore
        creds = _decrypt(conn_obj.encrypted_credentials)
        kwargs: dict = {
            "host": creds.get("host", "localhost"),
            "port": int(creds.get("port", 10000)),
            "database": creds.get("database", "default"),
        }
        auth_method = creds.get("auth_method", "NOSASL")
        if auth_method == "LDAP":
            kwargs["auth"] = "LDAP"
            kwargs["username"] = creds.get("username", "")
            kwargs["password"] = creds.get("password", "")
        else:
            kwargs["auth"] = "NOSASL"
        conn = hive.connect(**kwargs)
        try:
            cursor = conn.cursor()
            for stmt in statements:
                cursor.execute(stmt)
            cursor.close()
        finally:
            conn.close()

    await asyncio.to_thread(_run)


async def _execute_ddl_snowflake(conn_obj: Any, statements: list[str]) -> None:
    def _run() -> None:
        creds = _decrypt(conn_obj.encrypted_credentials)
        config = conn_obj.config or {}
        conn = _open_snowflake(creds, config)
        try:
            cursor = conn.cursor()
            for stmt in statements:
                cursor.execute(stmt)
            cursor.close()
        finally:
            conn.close()

    await asyncio.to_thread(_run)


async def _execute_ddl_sqlserver(conn_obj: Any, statements: list[str]) -> None:
    import aioodbc

    creds = _decrypt(conn_obj.encrypted_credentials)
    config = conn_obj.config or {}
    dsn = _build_sqlserver_dsn(creds, config)
    ddl_timeout = _get_source_timeout()
    conn = await asyncio.wait_for(aioodbc.connect(dsn=dsn), timeout=30)
    try:
        cursor = await conn.cursor()
        try:
            for stmt in statements:
                try:
                    await asyncio.wait_for(
                        cursor.execute(stmt), timeout=ddl_timeout
                    )
                except asyncio.TimeoutError as exc:
                    raise QueryTimeoutError(
                        f"SQL Server DDL exceeded {ddl_timeout}s timeout."
                    ) from exc
        finally:
            await cursor.close()
        await conn.commit()
    finally:
        await conn.close()


_DDL_DISPATCH = {
    "postgresql": _execute_ddl_pg,
    "bigquery": _execute_ddl_bq,
    "hadoop_spark": _execute_ddl_spark,
    "redshift": _execute_ddl_pg,
    "snowflake": _execute_ddl_snowflake,
    "sqlserver": _execute_ddl_sqlserver,
}

# F-016-20: the single source of truth for which connector dialects can run
# DDL through this executor. Callers (e.g. calendar auto-create) gate on this
# set instead of maintaining a parallel hardcoded literal that drifts out of
# step with the wired executors.
DDL_CAPABLE_CONNECTORS = frozenset(_DDL_DISPATCH)


async def execute_source_ddl(
    conn_obj: Any,
    ddl: str,
    *,
    tenant_session: Any = None,
) -> None:
    """Execute DDL (CREATE TABLE, etc.) against the source database.

    The DDL string may contain multiple semicolon-separated statements.
    PostgreSQL uses implicit autocommit for DDL via asyncpg (each
    ``execute`` auto-commits outside a transaction block).
    """
    _audit_log("execute_source_ddl", ddl)
    connector = normalize_connection_type(
        (conn_obj.connection_type or "").lower()
    )
    executor = _DDL_DISPATCH.get(connector)
    if executor is None:
        raise ValueError(f"Unsupported connector type: {connector!r}")

    statements = [s.strip() for s in ddl.split(";") if s.strip()]
    if not statements:
        return

    if connector in ("postgresql", "redshift"):
        await executor(conn_obj, statements, tenant_session=tenant_session)
    else:
        await executor(conn_obj, statements)


async def execute_source_sql_scalar(
    conn_obj: Any,
    sql: str,
    *,
    tenant_session: Any = None,
) -> Any:
    """Execute *sql* and return the first column of the first row, or None."""
    rows, _ = await execute_source_sql(conn_obj, sql, tenant_session=tenant_session)
    if not rows:
        return None
    first = rows[0]
    return next(iter(first.values())) if first else None


async def table_storage_bytes(
    conn_obj: Any,
    schema: str,
    table: str,
    *,
    bq_project: str | None = None,
    tenant_session: Any = None,
) -> int | None:
    """Best-effort physical storage size (bytes) for a table on the target.

    F-012-21: this is the single per-dialect capability for table storage size.
    Callers must not branch on connector type or hand-build the size query — the
    per-dialect SQL lives here. Returns ``None`` when the connector does not
    expose a table-size metric or the query fails (size is informational, never
    fatal to a refresh).

    Supported:
      - PostgreSQL / Redshift: ``pg_total_relation_size`` (Redshift exposes the
        Postgres-compatible catalog function).
      - Snowflake: ``information_schema.table_storage_metrics`` ACTIVE_BYTES.
      - BigQuery: ``__TABLES__.size_bytes`` metadata.
    Spark/Hive expose no portable byte metric → ``None``.
    """
    connector = normalize_connection_type((conn_obj.connection_type or "").lower())
    try:
        if connector in ("postgresql", "redshift"):
            # ``regclass`` resolves the dotted schema.table reference; identifiers
            # are validated upstream (resolved schema + physical_table_name).
            sql = f"SELECT pg_total_relation_size('{schema}.{table}'::regclass) AS b"
            result = await execute_source_sql_scalar(
                conn_obj, sql, tenant_session=tenant_session,
            )
            return int(result) if result is not None else None

        if connector == "snowflake":
            sql = (
                "SELECT SUM(active_bytes) AS b "
                "FROM information_schema.table_storage_metrics "
                f"WHERE table_schema = '{schema}' AND table_name = '{table}'"
            )
            result = await execute_source_sql_scalar(conn_obj, sql)
            return int(result) if result is not None else None

        if connector == "bigquery":
            project = bq_project or (conn_obj.config or {}).get("project_id", "")
            if not project:
                return None
            sql = (
                f"SELECT size_bytes AS b FROM `{project}.{schema}.__TABLES__` "
                f"WHERE table_id = '{table}'"
            )
            result = await execute_source_sql_scalar(conn_obj, sql)
            return int(result) if result is not None else None

        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Persistent connection for batch operations (stats probes, multi-query)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Bulk insert for cross-database aggregate materialization
# ---------------------------------------------------------------------------

_PG_TEXT_TYPES = frozenset({"TEXT", "VARCHAR", "CHAR", "CHARACTER VARYING"})


def _coerce_for_pg(value: Any, pg_type: str | None) -> Any:
    if value is None:
        return None
    if pg_type and pg_type.upper() in _PG_TEXT_TYPES:
        return str(value)
    return value


async def _bulk_insert_pg(
    conn_obj: Any,
    schema: str,
    table: str,
    columns: list[str],
    rows: list[dict],
    batch_size: int,
    *,
    tenant_session: Any = None,
    column_types: list[tuple[str, str]] | None = None,
) -> int:
    from shared.source_pool import acquire_source_connection
    creds = _decrypt(conn_obj.encrypted_credentials)
    config = conn_obj.config or {}
    host, port, database = await resolve_source_db_endpoint(
        creds, config,
        tenant_session=tenant_session,
        project_id=getattr(conn_obj, "project_id", None),
    )
    user = creds.get("user") or creds.get("username", "postgres")
    password = creds.get("password", "")
    async with acquire_source_connection(host, port, database, user, password) as conn:
        from shared.connector_qualify import quote_identifier, quote_table_ref
        total = 0
        placeholders = ", ".join(f"${i+1}" for i in range(len(columns)))
        # F-014-11: quote via connector_qualify (escapes embedded quotes) rather
        # than hand-rolled f'"{c}"'.
        col_names = ", ".join(quote_identifier("postgresql", c) for c in columns)
        qualified = quote_table_ref("postgresql", f"{schema}.{table}")
        insert_sql = f'INSERT INTO {qualified} ({col_names}) VALUES ({placeholders})'

        type_by_col: dict[str, str] = {}
        if column_types:
            type_by_col = {name: typ for name, typ in column_types}

        for offset in range(0, len(rows), batch_size):
            batch = rows[offset:offset + batch_size]
            if type_by_col:
                records = [
                    tuple(_coerce_for_pg(row.get(c), type_by_col.get(c)) for c in columns)
                    for row in batch
                ]
            else:
                records = [tuple(row.get(c) for c in columns) for row in batch]
            await conn.executemany(insert_sql, records)
            total += len(batch)
        return total


async def _bulk_insert_bq(
    conn_obj: Any,
    schema: str,
    table: str,
    columns: list[str],
    rows: list[dict],
    batch_size: int,
    target_project: str | None = None,
) -> int:
    def _run() -> int:
        from google.cloud import bigquery
        from google.oauth2 import service_account
        creds = _decrypt(conn_obj.encrypted_credentials)
        sa_info = creds.get("service_account_json", creds)
        if isinstance(sa_info, str):
            sa_info = json.loads(sa_info)
        gc = service_account.Credentials.from_service_account_info(sa_info)
        project_id = (
            target_project
            or (conn_obj.config or {}).get("project_id")
            or creds.get("project_id")
            or sa_info.get("project_id")
        )
        client = bigquery.Client(
            credentials=gc,
            project=project_id,
            location=(conn_obj.config or {}).get("location") or creds.get("location") or None,
        )
        try:
            table_ref = f"{project_id}.{schema}.{table}" if project_id else f"{schema}.{table}"
            total = 0
            for offset in range(0, len(rows), batch_size):
                batch = rows[offset:offset + batch_size]
                json_rows = [{c: row.get(c) for c in columns} for row in batch]
                errors = client.insert_rows_json(table_ref, json_rows)
                if errors:
                    raise RuntimeError(f"BigQuery insert errors: {errors[:3]}")
                total += len(batch)
            return total
        finally:
            client.close()

    return await asyncio.to_thread(_run)


async def _bulk_insert_spark(
    conn_obj: Any,
    schema: str,
    table: str,
    columns: list[str],
    rows: list[dict],
    batch_size: int,
) -> int:
    def _run() -> int:
        from pyhive import hive  # type: ignore
        creds = _decrypt(conn_obj.encrypted_credentials)
        kwargs: dict = {
            "host": creds.get("host", "localhost"),
            "port": int(creds.get("port", 10000)),
            "database": creds.get("database", "default"),
        }
        auth_method = creds.get("auth_method", "NOSASL")
        if auth_method == "LDAP":
            kwargs["auth"] = "LDAP"
            kwargs["username"] = creds.get("username", "")
            kwargs["password"] = creds.get("password", "")
        else:
            kwargs["auth"] = "NOSASL"
        conn = hive.connect(**kwargs)
        try:
            cursor = conn.cursor()
            from shared.connector_qualify import quote_identifier
            col_names = ", ".join(quote_identifier("hadoop_spark", c) for c in columns)
            total = 0
            for offset in range(0, len(rows), batch_size):
                batch = rows[offset:offset + batch_size]
                values_list = []
                for row in batch:
                    vals = []
                    for c in columns:
                        v = row.get(c)
                        if v is None:
                            vals.append("NULL")
                        elif isinstance(v, (int, float)):
                            vals.append(str(v))
                        elif isinstance(v, bool):
                            vals.append("TRUE" if v else "FALSE")
                        elif isinstance(v, str):
                            vals.append("'" + v.replace("\\", "\\\\").replace("'", "''") + "'")
                        else:
                            vals.append("'" + str(v).replace("\\", "\\\\").replace("'", "''") + "'")
                    values_list.append("(" + ", ".join(vals) + ")")
                from shared.connector_qualify import quote_table_ref
                qualified_table = quote_table_ref("hadoop_spark", f"{schema}.{table}")
                insert_sql = (
                    f"INSERT INTO {qualified_table} ({col_names}) VALUES "
                    + ", ".join(values_list)
                )
                cursor.execute(insert_sql)
                total += len(batch)
            cursor.close()
            return total
        finally:
            conn.close()

    return await asyncio.to_thread(_run)


async def _bulk_insert_snowflake(
    conn_obj: Any,
    schema: str,
    table: str,
    columns: list[str],
    rows: list[dict],
    batch_size: int,
) -> int:
    def _run() -> int:
        creds = _decrypt(conn_obj.encrypted_credentials)
        config = conn_obj.config or {}
        conn = _open_snowflake(creds, config)
        try:
            from shared.connector_qualify import quote_identifier, quote_table_ref
            cursor = conn.cursor()
            # F-014-11: quote via connector_qualify (escapes embedded quotes).
            col_names = ", ".join(quote_identifier("snowflake", c) for c in columns)
            qualified = quote_table_ref("snowflake", f"{schema}.{table}")
            placeholders = ", ".join(["%s"] * len(columns))
            insert_sql = f'INSERT INTO {qualified} ({col_names}) VALUES ({placeholders})'
            total = 0
            for offset in range(0, len(rows), batch_size):
                batch = rows[offset:offset + batch_size]
                records = [tuple(row.get(c) for c in columns) for row in batch]
                cursor.executemany(insert_sql, records)
                total += len(batch)
            cursor.close()
            return total
        finally:
            conn.close()

    return await asyncio.to_thread(_run)


async def _bulk_insert_sqlserver(
    conn_obj: Any,
    schema: str,
    table: str,
    columns: list[str],
    rows: list[dict],
    batch_size: int,
) -> int:
    import aioodbc

    from shared.connector_qualify import quote_identifier, quote_table_ref

    creds = _decrypt(conn_obj.encrypted_credentials)
    config = conn_obj.config or {}
    dsn = _build_sqlserver_dsn(creds, config)
    conn = await aioodbc.connect(dsn=dsn)
    try:
        cursor = await conn.cursor()
        try:
            col_names = ", ".join(quote_identifier("sqlserver", c) for c in columns)
            qualified_table = quote_table_ref("sqlserver", f"{schema}.{table}")
            placeholders = ", ".join(["?"] * len(columns))
            insert_sql = f"INSERT INTO {qualified_table} ({col_names}) VALUES ({placeholders})"
            total = 0
            for i in range(0, len(rows), batch_size):
                batch = rows[i:i + batch_size]
                records = [tuple(row.get(c) for c in columns) for row in batch]
                await cursor.executemany(insert_sql, records)
                total += len(batch)
        finally:
            await cursor.close()
        await conn.commit()
        return total
    finally:
        await conn.close()


_BULK_INSERT_DISPATCH = {
    "postgresql": _bulk_insert_pg,
    "bigquery": _bulk_insert_bq,
    "hadoop_spark": _bulk_insert_spark,
    "redshift": _bulk_insert_pg,
    "snowflake": _bulk_insert_snowflake,
    "sqlserver": _bulk_insert_sqlserver,
}


_ENSURE_SCHEMA_SQLGLOT: dict[str, str] = {
    "postgresql": "postgres",
    "redshift": "redshift",
    "bigquery": "bigquery",
    "hadoop_spark": "spark",
    "snowflake": "snowflake",
}


async def ensure_target_schema(
    conn_obj: Any,
    schema: str,
    *,
    tenant_session: Any = None,
) -> None:
    """Ensure the target schema/dataset exists, creating it if not.

    Connector-agnostic: writes canonical PostgreSQL DDL and transpiles
    to the target dialect via sqlglot. SQL Server requires dynamic SQL
    due to lack of IF NOT EXISTS support.
    """
    if not schema:
        return

    import sqlglot

    connector = normalize_connection_type(
        (conn_obj.connection_type or "").lower()
    )

    if connector == "sqlserver":
        # F-01: SQL Server DDL exemption — bracket-quoted identifiers and
        # dynamic SQL are inherently connector-specific. sqlglot cannot
        # transpile IF NOT EXISTS ... EXEC(...) patterns.
        from shared.connector_qualify import quote_identifier
        safe_schema = schema.replace("'", "''")
        quoted = quote_identifier("sqlserver", schema)
        # Inside EXEC('...'), single quotes in the quoted identifier must be doubled.
        quoted_for_exec = quoted.replace("'", "''")
        ddl = (
            f"IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = N'{safe_schema}') "
            f"EXEC('CREATE SCHEMA {quoted_for_exec}')"
        )
    else:
        target = _ENSURE_SCHEMA_SQLGLOT.get(connector)
        if target is None:
            raise ValueError(f"ensure_target_schema: unsupported connector {connector!r}")
        # F-014-11: quote the schema identifier via connector_qualify (PostgreSQL
        # quoting, embedded-quote safe) before transpiling to the target dialect.
        from shared.connector_qualify import quote_identifier
        pg_ddl = f'CREATE SCHEMA IF NOT EXISTS {quote_identifier("postgresql", schema)}'
        ddl = sqlglot.transpile(pg_ddl, read="postgres", write=target)[0]

    await execute_source_ddl(conn_obj, ddl, tenant_session=tenant_session)


async def bulk_insert_batched(
    conn_obj: Any,
    schema: str,
    table: str,
    columns: list[str],
    rows: list[dict],
    batch_size: int = 20_000,
    *,
    tenant_session: Any = None,
    column_types: list[tuple[str, str]] | None = None,
    target_project: str | None = None,
) -> int:
    """Insert rows into a target table in batches.

    Used by the cross-database aggregate materialization path.
    Returns total rows inserted.
    """
    if not rows:
        return 0

    _audit_log("bulk_insert_batched", f"INSERT INTO {schema}.{table} ({len(rows)} rows)")
    connector = normalize_connection_type(
        (conn_obj.connection_type or "").lower()
    )
    executor = _BULK_INSERT_DISPATCH.get(connector)
    if executor is None:
        raise ValueError(f"Unsupported connector type for bulk insert: {connector!r}")

    if connector in ("postgresql", "redshift"):
        return await executor(
            conn_obj, schema, table, columns, rows, batch_size,
            tenant_session=tenant_session,
            column_types=column_types,
        )
    if connector == "bigquery":
        return await executor(
            conn_obj, schema, table, columns, rows, batch_size,
            target_project=target_project,
        )
    return await executor(conn_obj, schema, table, columns, rows, batch_size)


async def _staging_swap(
    sc, connector: str, schema: str, table: str,
    staging_table: str, backup_table: str,
    staging_ref: str, live_ref: str, backup_ref: str,
    quote_identifier_fn,
) -> None:
    """Connector-specific safe swap: staging → live with rollback on failure."""
    if connector == "bigquery":
        # BigQuery: CREATE TABLE ... COPY for backup, then DROP + RENAME.
        # BigQuery RENAME takes a bare table name (not fully qualified).
        await sc.execute(f"DROP TABLE IF EXISTS {backup_ref}")
        try:
            await sc.execute(f"CREATE TABLE {backup_ref} COPY {live_ref}")
            live_existed = True
        except Exception:
            live_existed = False
        if live_existed:
            await sc.execute(f"DROP TABLE {live_ref}")
        try:
            await sc.execute(
                f"ALTER TABLE {staging_ref} RENAME TO "
                f"{quote_identifier_fn(connector, table)}"
            )
        except Exception:
            if live_existed:
                await sc.execute(
                    f"ALTER TABLE {backup_ref} RENAME TO "
                    f"{quote_identifier_fn(connector, table)}"
                )
            raise
        if live_existed:
            await sc.execute(f"DROP TABLE IF EXISTS {backup_ref}")
    elif connector in ("hadoop_spark", "snowflake"):
        # Spark/Hive/Snowflake: ALTER TABLE ... RENAME TO requires fully qualified name.
        schema_prefix = f"{quote_identifier_fn(connector, schema)}."
        await sc.execute(f"DROP TABLE IF EXISTS {backup_ref}")
        try:
            await sc.execute(
                f"ALTER TABLE {live_ref} RENAME TO "
                f"{schema_prefix}{quote_identifier_fn(connector, backup_table)}"
            )
            live_existed = True
        except Exception:
            live_existed = False
        try:
            await sc.execute(
                f"ALTER TABLE {staging_ref} RENAME TO "
                f"{schema_prefix}{quote_identifier_fn(connector, table)}"
            )
        except Exception:
            if live_existed:
                await sc.execute(
                    f"ALTER TABLE {backup_ref} RENAME TO "
                    f"{schema_prefix}{quote_identifier_fn(connector, table)}"
                )
            raise
        if live_existed:
            await sc.execute(f"DROP TABLE IF EXISTS {backup_ref}")
    elif connector in ("postgresql", "redshift"):
        await sc.execute(f"DROP TABLE IF EXISTS {backup_ref}")
        live_exists = await sc.fetch_one(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = $1 AND table_name = $2",
            schema, table,
        )
        if live_exists:
            await sc.execute(
                f"ALTER TABLE {live_ref} RENAME TO "
                f"{quote_identifier_fn(connector, backup_table)}"
            )
        try:
            await sc.execute(
                f"ALTER TABLE {staging_ref} RENAME TO "
                f"{quote_identifier_fn(connector, table)}"
            )
        except Exception:
            if live_exists:
                await sc.execute(
                    f"ALTER TABLE {backup_ref} RENAME TO "
                    f"{quote_identifier_fn(connector, table)}"
                )
            raise
        if live_exists:
            await sc.execute(f"DROP TABLE IF EXISTS {backup_ref}")
    elif connector == "sqlserver":
        # F-02: SQL Server staging swap exemption — sp_rename requires string
        # literal arguments, not bracket-quoted identifiers. This is inherently
        # connector-specific DDL that cannot be transpiled via sqlglot.
        def _sq(name: str) -> str:
            """Escape a name for use inside a sp_rename string literal."""
            return name.replace("'", "''")

        await sc.execute(f"DROP TABLE IF EXISTS {backup_ref}")
        live_exists = await sc.fetch_one(
            "SELECT 1 FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
            schema, table,
        )
        if live_exists:
            await sc.execute(
                f"EXEC sp_rename '{_sq(schema)}.{_sq(table)}', '{_sq(backup_table)}'"
            )
        try:
            await sc.execute(
                f"EXEC sp_rename '{_sq(schema)}.{_sq(staging_table)}', '{_sq(table)}'"
            )
        except Exception:
            if live_exists:
                await sc.execute(
                    f"EXEC sp_rename '{_sq(schema)}.{_sq(backup_table)}', '{_sq(table)}'"
                )
            raise
        if live_exists:
            await sc.execute(f"DROP TABLE IF EXISTS {backup_ref}")
    else:
        raise ValueError(f"atomic_swap_table: unsupported connector {connector!r}")


async def stream_to_staging_table(
    conn_obj: Any,
    schema: str,
    table: str,
    row_batches: AsyncIterator[list[dict]],
    batch_size: int = 20_000,
    *,
    col_defs: list[tuple[str, str]] | None = None,
    infer_types_fn: Any = None,
    tenant_session: Any = None,
    target_project: str | None = None,
) -> int:
    """Stream rows into a staging table and atomically swap with the live table.

    This keeps memory bounded (only one batch in memory at a time) and
    preserves the live table until the full load succeeds. On failure the
    staging table is cleaned up and the live table remains intact.

    If ``col_defs`` is None, types are inferred from the first batch using
    ``infer_types_fn(batch, col_names)`` — the staging table is created
    after the first batch arrives.

    For BigQuery targets, pass ``target_project`` to use a project override
    different from the connection default.

    Returns total rows inserted.
    """
    from shared.connector_qualify import quote_identifier

    connector = normalize_connection_type(
        (conn_obj.connection_type or "").lower()
    )

    staging_table = f"{table}__staging"
    backup_table = f"{table}__backup"

    def _build_ref(tbl: str) -> str:
        base = f"{quote_identifier(connector, schema)}.{quote_identifier(connector, tbl)}"
        if target_project and connector == "bigquery":
            return f"{quote_identifier(connector, target_project)}.{base}"
        return base

    staging_ref = _build_ref(staging_table)
    live_ref = _build_ref(table)
    backup_ref = _build_ref(backup_table)

    _audit_log("stream_to_staging_table", f"staging={schema}.{staging_table}")

    async def _create_staging(defs: list[tuple[str, str]]) -> None:
        col_def_sql = ", ".join(
            f"{quote_identifier(connector, c)} {t}" for c, t in defs
        )
        async with open_source_connection(conn_obj, tenant_session=tenant_session) as sc:
            await sc.execute(f"DROP TABLE IF EXISTS {staging_ref}")
            await sc.execute(f"CREATE TABLE {staging_ref} ({col_def_sql})")

    if col_defs is not None:
        await _create_staging(col_defs)

    total_rows = 0
    staging_created = col_defs is not None
    resolved_defs: list[tuple[str, str]] = col_defs or []

    try:
        async for batch in row_batches:
            if not batch:
                continue

            if not staging_created:
                col_names = list(batch[0].keys())
                if infer_types_fn:
                    resolved_defs = infer_types_fn(batch, col_names)
                else:
                    resolved_defs = [(c, "TEXT") for c in col_names]
                await _create_staging(resolved_defs)
                staging_created = True

            col_names_for_insert = [c for c, _t in resolved_defs]
            inserted = await bulk_insert_batched(
                conn_obj, schema, staging_table, col_names_for_insert, batch,
                batch_size=batch_size, tenant_session=tenant_session,
                column_types=resolved_defs,
                target_project=target_project,
            )
            total_rows += inserted

        if not staging_created:
            return 0

        async with open_source_connection(conn_obj, tenant_session=tenant_session) as sc:
            count_row = await sc.fetch_one(
                f"SELECT COUNT(*) AS c FROM {staging_ref}"
            )
            staged_count = int(count_row["c"]) if count_row else 0
            if staged_count != total_rows:
                raise RuntimeError(
                    f"Staging validation failed: expected {total_rows} rows, "
                    f"got {staged_count} in {staging_ref}"
                )

            await _staging_swap(
                sc, connector, schema, table,
                staging_table, backup_table,
                staging_ref, live_ref, backup_ref,
                quote_identifier,
            )
    except Exception:
        if staging_created:
            async with open_source_connection(conn_obj, tenant_session=tenant_session) as sc:
                await sc.execute(f"DROP TABLE IF EXISTS {staging_ref}")
        raise

    return total_rows


class SourceConnection:
    """Thin connector-agnostic wrapper over a raw source-DB connection.

    Returned by :func:`open_source_connection`. Callers use ``fetch``,
    ``fetch_one``, ``fetch_scalar``, and ``execute`` — never branch on
    connector type.
    """

    def __init__(self, impl: Any, connector: str):
        self._impl = impl
        self._connector = connector

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        timeout_s = _get_source_timeout()
        if self._connector in ("postgresql", "redshift"):
            records = await self._impl.fetch(sql, *args)
            return [dict(r) for r in records]
        if self._connector == "bigquery":
            try:
                return await asyncio.wait_for(asyncio.to_thread(self._bq_fetch, sql), timeout=timeout_s)
            except asyncio.TimeoutError as exc:
                raise QueryTimeoutError(f"BigQuery fetch exceeded {timeout_s}s timeout.") from exc
        if self._connector in ("hadoop_spark", "snowflake"):
            try:
                return await asyncio.wait_for(asyncio.to_thread(self._cursor_fetch, sql), timeout=timeout_s)
            except asyncio.TimeoutError as exc:
                raise QueryTimeoutError(f"{self._connector} fetch exceeded {timeout_s}s timeout.") from exc
        if self._connector == "sqlserver":
            try:
                return await asyncio.wait_for(self._aioodbc_fetch(sql, *args), timeout=timeout_s)
            except asyncio.TimeoutError as exc:
                raise QueryTimeoutError(f"SQL Server fetch exceeded {timeout_s}s timeout.") from exc
        raise ValueError(f"Unsupported connector: {self._connector!r}")

    async def fetch_one(self, sql: str, *args: Any) -> dict | None:
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def fetch_scalar(self, sql: str, *args: Any) -> Any:
        row = await self.fetch_one(sql, *args)
        if row is None:
            return None
        return next(iter(row.values()))

    async def execute(self, sql: str) -> None:
        timeout_s = _get_source_timeout()
        if self._connector in ("postgresql", "redshift"):
            await self._impl.execute(sql)
        elif self._connector == "bigquery":
            try:
                await asyncio.wait_for(asyncio.to_thread(self._bq_execute, sql), timeout=timeout_s)
            except asyncio.TimeoutError as exc:
                raise QueryTimeoutError(f"BigQuery execute exceeded {timeout_s}s timeout.") from exc
        elif self._connector in ("hadoop_spark", "snowflake"):
            try:
                await asyncio.wait_for(asyncio.to_thread(self._cursor_execute, sql), timeout=timeout_s)
            except asyncio.TimeoutError as exc:
                raise QueryTimeoutError(f"{self._connector} execute exceeded {timeout_s}s timeout.") from exc
        elif self._connector == "sqlserver":
            try:
                await asyncio.wait_for(self._impl.execute(sql), timeout=timeout_s)
            except asyncio.TimeoutError as exc:
                raise QueryTimeoutError(f"SQL Server execute exceeded {timeout_s}s timeout.") from exc
        else:
            raise ValueError(f"Unsupported connector: {self._connector!r}")

    async def fetch_batched(
        self, sql: str, batch_size: int = 20_000
    ) -> AsyncIterator[list[dict]]:
        """Yield row batches from a query without loading all rows into memory.

        Each yielded list contains up to ``batch_size`` rows. Callers can
        process or insert each batch before fetching the next, keeping memory
        bounded regardless of total result size.
        """
        if self._connector in ("postgresql", "redshift"):
            async with self._impl.transaction():
                cur = await self._impl.cursor(sql)
                while True:
                    batch = await cur.fetch(batch_size)
                    if not batch:
                        break
                    yield [dict(r) for r in batch]
        elif self._connector == "bigquery":
            async for batch in self._threaded_batch_iter(
                self._bq_fetch_batched_gen, sql, batch_size
            ):
                yield batch
        elif self._connector in ("hadoop_spark", "snowflake"):
            async for batch in self._threaded_batch_iter(
                self._cursor_fetch_batched_gen, sql, batch_size
            ):
                yield batch
        elif self._connector == "sqlserver":
            await self._impl.execute(sql)
            if self._impl.description is None:
                return
            cols = [d[0] for d in self._impl.description]
            while True:
                rows = await self._impl.fetchmany(batch_size)
                if not rows:
                    break
                yield [dict(zip(cols, row)) for row in rows]
        else:
            raise ValueError(f"Unsupported connector: {self._connector!r}")

    async def _threaded_batch_iter(
        self, sync_gen_fn, sql: str, batch_size: int
    ) -> AsyncIterator[list[dict]]:
        """Bridge a synchronous batch generator to an async iterator via a queue.

        The sync generator runs in a thread and puts one batch at a time.
        The async side awaits each batch before the thread fetches the next,
        keeping at most one batch in memory at a time.
        """
        import threading

        timeout_s = _get_source_timeout()
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        sentinel = object()
        stop_event = threading.Event()

        def _producer():
            try:
                for batch in sync_gen_fn(sql, batch_size):
                    if stop_event.is_set():
                        break
                    asyncio.run_coroutine_threadsafe(
                        queue.put(batch), loop
                    ).result(timeout=60)
                if not stop_event.is_set():
                    asyncio.run_coroutine_threadsafe(
                        queue.put(sentinel), loop
                    ).result(timeout=60)
            except Exception as exc:
                if not stop_event.is_set():
                    try:
                        asyncio.run_coroutine_threadsafe(
                            queue.put(exc), loop
                        ).result(timeout=10)
                    except Exception:
                        pass

        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, _producer)

        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=timeout_s)
                except asyncio.TimeoutError:
                    raise QueryTimeoutError(
                        f"Batched source read timed out after {timeout_s}s"
                    )
                if item is sentinel:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            stop_event.set()
            try:
                await asyncio.wait_for(asyncio.wrap_future(future), timeout=5)
            except (asyncio.TimeoutError, Exception):
                pass

    def _bq_fetch_batched_gen(self, sql: str, batch_size: int):
        timeout_s = _get_source_timeout()
        job = self._impl.query(sql)
        result = job.result(page_size=batch_size, timeout=timeout_s)
        batch: list[dict] = []
        for row in result:
            batch.append(dict(row))
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    def _cursor_fetch_batched_gen(self, sql: str, batch_size: int):
        self._impl.execute(sql)
        if self._impl.description is None:
            return
        cols = [d[0] for d in self._impl.description]
        while True:
            rows = self._impl.fetchmany(batch_size)
            if not rows:
                break
            yield [dict(zip(cols, row)) for row in rows]

    def _bq_fetch(self, sql: str) -> list[dict]:
        job = self._impl.query(sql)
        result = job.result()
        return [dict(row) for row in result]

    def _bq_execute(self, sql: str) -> None:
        job = self._impl.query(sql)
        job.result()

    def _cursor_fetch(self, sql: str) -> list[dict]:
        self._impl.execute(sql)
        if self._impl.description is None:
            return []
        cols = [d[0] for d in self._impl.description]
        return [dict(zip(cols, row)) for row in self._impl.fetchall()]

    def _cursor_execute(self, sql: str) -> None:
        self._impl.execute(sql)

    async def _aioodbc_fetch(self, sql: str, *args: Any) -> list[dict]:
        if args:
            await self._impl.execute(sql, args)
        else:
            await self._impl.execute(sql)
        if self._impl.description is None:
            return []
        cols = [d[0] for d in self._impl.description]
        return [dict(zip(cols, row)) for row in await self._impl.fetchall()]


@asynccontextmanager
async def open_source_connection(
    conn_obj: Any,
    *,
    tenant_session: Any = None,
):
    """Open a persistent source-DB connection for batch operations.

    Usage::

        async with open_source_connection(conn_obj) as sc:
            rows = await sc.fetch("SELECT COUNT(*) AS c FROM ...")
            await sc.execute("CREATE TABLE ...")

    Credential decryption, connection construction, and cleanup are
    handled internally. Callers never branch on connector type.
    """
    _audit_log("open_source_connection", "(persistent connection)")
    connector = normalize_connection_type(
        (conn_obj.connection_type or "").lower()
    )
    if connector in ("postgresql", "redshift"):
        from shared.source_pool import acquire_source_connection
        creds = _decrypt(conn_obj.encrypted_credentials)
        config = conn_obj.config or {}
        host, port, database = await resolve_source_db_endpoint(
            creds, config,
            tenant_session=tenant_session,
            project_id=getattr(conn_obj, "project_id", None),
        )
        user = creds.get("user") or creds.get("username", "postgres")
        password = creds.get("password", "")
        async with acquire_source_connection(host, port, database, user, password) as raw:
            yield SourceConnection(raw, connector)

    elif connector == "bigquery":
        from google.cloud import bigquery
        from google.oauth2 import service_account
        creds = _decrypt(conn_obj.encrypted_credentials)
        sa_info = creds.get("service_account_json", creds)
        if isinstance(sa_info, str):
            sa_info = json.loads(sa_info)
        gc = service_account.Credentials.from_service_account_info(sa_info)
        project_id = (
            (conn_obj.config or {}).get("project_id")
            or creds.get("project_id")
            or sa_info.get("project_id")
        )
        client = bigquery.Client(
            credentials=gc,
            project=project_id,
            location=(conn_obj.config or {}).get("location") or creds.get("location") or None,
        )
        try:
            yield SourceConnection(client, "bigquery")
        finally:
            client.close()

    elif connector == "hadoop_spark":
        from pyhive import hive  # type: ignore
        creds = _decrypt(conn_obj.encrypted_credentials)
        kwargs: dict = {
            "host": creds.get("host", "localhost"),
            "port": int(creds.get("port", 10000)),
            "database": creds.get("database", "default"),
        }
        auth_method = creds.get("auth_method", "NOSASL")
        if auth_method == "LDAP":
            kwargs["auth"] = "LDAP"
            kwargs["username"] = creds.get("username", "")
            kwargs["password"] = creds.get("password", "")
        else:
            kwargs["auth"] = "NOSASL"
        raw = hive.connect(**kwargs)
        try:
            yield SourceConnection(raw.cursor(), "hadoop_spark")
        finally:
            raw.close()

    elif connector == "snowflake":
        creds = _decrypt(conn_obj.encrypted_credentials)
        config = conn_obj.config or {}
        raw = _open_snowflake(creds, config)
        try:
            yield SourceConnection(raw.cursor(), "snowflake")
        finally:
            raw.close()

    elif connector == "sqlserver":
        import aioodbc
        creds = _decrypt(conn_obj.encrypted_credentials)
        config = conn_obj.config or {}
        dsn = _build_sqlserver_dsn(creds, config)
        raw_conn = await aioodbc.connect(dsn=dsn)
        try:
            cursor = await raw_conn.cursor()
            try:
                yield SourceConnection(cursor, "sqlserver")
            finally:
                await cursor.close()
        finally:
            await raw_conn.close()

    else:
        raise ValueError(f"Unsupported connector type: {connector!r}")
