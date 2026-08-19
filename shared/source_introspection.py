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
from shared.source_pool import PoolScopeError, session_tenant_id
from shared.source_executor import (
    _audit_context,
    _audit_log,
    _build_sqlserver_dsn,
    _get_source_timeout,
    _open_snowflake,
    _resolve_bq_max_bytes,
)

logger = logging.getLogger(__name__)

# F-014-05 / G-014-02: truncation is a structured flag, never a fake table row.
TRUNCATION_SCHEMA = "__tessallite__"
TRUNCATION_TABLE = "__truncated__"


def is_truncation_marker(row: Any) -> bool:
    """True for the reserved Tessallite truncation sentinel, never a real table.

    Bug-9290: a customer table named ``__truncated__`` in a normal schema
    (e.g. ``public``) is selectable. Match the reserved schema
    ``__tessallite__``, plus ``type==NOTICE`` which is not a real catalogue
    ``table_type`` (PG/BQ/Snowflake/Spark use BASE TABLE / VIEW / TABLE).
    """
    if not isinstance(row, dict):
        return False
    return row.get("schema") == TRUNCATION_SCHEMA or row.get("type") == "NOTICE"


def is_truncation_profile_ref(schema: str | None, table: str | None) -> bool:
    """Profile must refuse only the reserved namespace (Bug-9290 / F-CP07-R1-01).

    *table* is accepted so callers pass the pair; the name ``__truncated__``
    alone is not a sentinel.
    """
    del table
    return schema == TRUNCATION_SCHEMA


def normalize_discover_payload(raw: Any) -> dict[str, Any]:
    """Return ``{tables, truncated}`` and strip any leftover sentinel row."""
    if isinstance(raw, tuple) and len(raw) == 2:
        tables_raw, truncated_flag = raw
        original = list(tables_raw or [])
        truncated = bool(truncated_flag) or any(is_truncation_marker(t) for t in original)
        tables = [t for t in original if isinstance(t, dict) and not is_truncation_marker(t)]
        return {"tables": tables, "truncated": truncated}
    if isinstance(raw, dict) and "tables" in raw:
        original = list(raw.get("tables") or [])
        truncated = bool(raw.get("truncated")) or any(is_truncation_marker(t) for t in original)
        tables = [t for t in original if isinstance(t, dict) and not is_truncation_marker(t)]
        return {"tables": tables, "truncated": truncated}
    if isinstance(raw, list):
        truncated = any(is_truncation_marker(t) for t in raw)
        tables = [t for t in raw if isinstance(t, dict) and not is_truncation_marker(t)]
        return {"tables": tables, "truncated": truncated}
    return {"tables": [], "truncated": False}


def _redact_secrets_in_error(detail: str, creds: dict | None) -> str:
    """Strip known credential VALUES from a driver error (F-014-10).

    Keeps the rest of the message so Test Connection stays honest (C10). Live
    drivers were not shown to embed passwords on this tip; this fires only when
    the secret string actually appears (SQL Server DSN ``PWD=`` is the plausible
    case).
    """
    if not detail:
        return detail
    redacted = detail
    from shared.schemas.domains.tenants_projects import _is_sensitive_key

    def _walk(node: Any) -> list[str]:
        found: list[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                if _is_sensitive_key(str(key)) and isinstance(value, str) and len(value) >= 2:
                    found.append(value)
                found.extend(_walk(value))
        elif isinstance(node, list):
            for item in node:
                found.extend(_walk(item))
        return found

    for secret in _walk(creds or {}):
        if secret and secret in redacted:
            redacted = redacted.replace(secret, "***")
    import re
    redacted = re.sub(r"(?i)\b(PWD|PASSWORD)\s*=\s*[^;]+", r"\1=***", redacted)
    return redacted


def _decrypt(encrypted: bytes) -> dict:
    from shared.security.credential_crypto import decrypt_json
    return decrypt_json(encrypted)


def _resolve_connector(conn_obj: Any) -> str:
    return normalize_connection_type((conn_obj.connection_type or "").lower()) or "unknown"


def _introspect_scope(tenant_session: Any, project_id: Any) -> str:
    """Fail-closed tenant scope for pooled introspection connections (Bug-8039).

    The canonical tenant boundary is the tenant slug read from the tenant-bound
    session (``session.info['tenant_id']``) — NOT ``project_id``, which is a
    per-tenant-schema UUID that can collide across tenants. Introspection is
    always invoked under an authenticated, tenant-scoped session (the model- and
    query-router endpoints use ``get_tenant_db``), so the tenant is available
    even for a pre-save draft test-before-save. A missing tenant raises
    :class:`PoolScopeError` rather than sharing a pool across tenants.
    """
    from shared.source_pool import build_pool_scope, session_tenant_id
    return build_pool_scope(
        session_tenant_id(tenant_session),
        project_id=project_id,
        conn_id="introspect",
    )


def _bq_sa_info(creds: dict) -> dict:
    """Extract the parsed service-account dict from stored BigQuery credentials.

    Accepts both the nested ``{"service_account_json": {...}}`` shape and a
    flattened service-account dict, and parses a JSON string form.
    """
    sa_info = (creds or {}).get("service_account_json", creds)
    if isinstance(sa_info, str):
        sa_info = json.loads(sa_info)
    sa_info = sa_info or {}
    # Bug-6216 R2 finding 4: this is the single parse point every BigQuery
    # introspection path funnels through, so the tenant-controlled OAuth
    # endpoints inside the blob are policed here rather than at each caller.
    from shared.security.source_host_policy import (
        assert_service_account_endpoints_allowed,
    )
    assert_service_account_endpoints_allowed(sa_info)
    return sa_info


def _bq_project(creds: dict, config: dict, sa_info: dict) -> str | None:
    """Resolve the BigQuery project id with the full three-way fallback.

    F-014-03 (Bug-8037): the introspection copies previously resolved
    ``creds.get("project_id", config.get("project_id"))`` and omitted the
    ``service_account_json.project_id`` fallback, so a connection whose project
    lives only inside the service-account key produced live requests under
    ``projects/None/...`` and table discovery returned HTTP 502. This mirrors
    the resolution order in ``query-router BigQueryExecutor`` and shared
    ``_execute_bq`` (config -> creds -> service-account) so a connection that
    tests green can also discover, describe, and profile tables.
    """
    return (
        (config or {}).get("project_id")
        or (creds or {}).get("project_id")
        or (sa_info or {}).get("project_id")
    )


def _open_bq_client(creds: dict, config: dict):
    """Open a BigQuery client and return ``(client, project)``.

    Single normalization point for all introspection paths (test excluded, as
    it does not need the project). Callers must ``client.close()``.
    """
    from google.cloud import bigquery
    sa_info = _bq_sa_info(creds)
    project = _bq_project(creds, config, sa_info)
    client = bigquery.Client.from_service_account_info(
        sa_info,
        project=project,
        location=(config or {}).get("location") or (creds or {}).get("location") or None,
    )
    return client, project


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


import time as _time
from contextlib import asynccontextmanager as _asynccontextmanager


@_asynccontextmanager
async def _audited_introspection(
    conn_obj: Any, operation: str, *, tenant_slug: str | None = None,
):
    """Emit a structured start + completion ``[SOURCE_AUDIT]`` record around a
    public introspection call (F-014-04 / Bug-8038).

    Unlike the per-leaf SQL-shape trace, this carries the full attributable
    context (tenant, project, connection, connector, ``purpose=introspect``)
    and the outcome (ok / error) so an introspection touch can be tied to a
    tenant during an incident. ``operation`` is e.g. ``introspect.discover_tables``.
    """
    context = _audit_context(conn_obj, "introspect", tenant_slug=tenant_slug)
    _audit_log(f"{operation}.start", "", context=context)
    started = _time.monotonic()
    try:
        yield
    except BaseException as exc:
        _audit_log(
            f"{operation}.end", "",
            context={
                **context,
                "outcome": "error",
                "error": type(exc).__name__,
                "duration_ms": int((_time.monotonic() - started) * 1000),
            },
        )
        raise
    _audit_log(
        f"{operation}.end", "",
        context={
            **context,
            "outcome": "ok",
            "duration_ms": int((_time.monotonic() - started) * 1000),
        },
    )


# ---------------------------------------------------------------------------
# Connection testing
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Egress policy (Bug-6216)
# ---------------------------------------------------------------------------

async def _endpoint_host(
    connector: str, creds: dict, config: dict, *,
    tenant_session=None, project_id=None,
) -> str | None:
    """Return the host this connector would actually dial, or ``None`` when the
    connector has no tenant-supplied endpoint at all (see
    ``EXPLICITLY_HOSTLESS_CONNECTORS``).

    R2 briefly exempted the ``source_db.fallback_host`` system setting on the
    reasoning that the policy should not second-guess an address the OPERATOR
    configured. R3 removed that carve-out, because it was a live hole rather
    than a nicety: the fallback supplies only the HOST. ``postgresql`` and
    ``redshift`` still take the PORT and DATABASE from the tenant's own
    credentials, so omitting ``host`` bought a tenant precisely the
    ``localhost:<port-of-my-choosing>`` connection that typing
    ``host: localhost`` is refused for -- the port-scan oracle Bug-6216 exists
    to remove, restored for the default connector.

    A deployment whose legitimate source really does sit on loopback names it
    in ``SOURCE_HOST_ALLOWLIST``. That is what the allowlist is for, and it
    makes the exemption an explicit operator act instead of a silent bypass.
    """
    from shared.security.source_host_policy import HOST_BEARING_CONNECTORS

    if connector not in HOST_BEARING_CONNECTORS:
        return None

    tenant_host = (creds or {}).get("host") or (config or {}).get("host")
    if tenant_host:
        return str(tenant_host)

    if connector in ("postgresql", "redshift"):
        # Whatever this resolves to IS what gets dialled, so it is what the
        # policy has to judge.
        #
        # Bug-7172 regression guard: none of this module's introspection call
        # sites pass a ``system_session`` (they run ad-hoc, off a tenant
        # request), so ``resolve_source_db_endpoint`` can never PROVE a
        # ``source_db.fallback_host`` override exists here and now always
        # raises ``MissingConnectionHostError`` on a hostless connection — a
        # SUPPORTED persisted state (model-service accepts a connection with
        # no host). Before Bug-7172 this branch silently returned the
        # registry's compiled-in "localhost" default; the caller
        # (``assert_introspection_endpoint_allowed``) already treats a
        # ``None`` host as "nothing to dial" and raises the clean, existing
        # ``SourceHostBlockedError`` — so translate the new fail-loud error
        # into that same signal instead of letting it escape as a bare 500.
        from shared.config.source_db import MissingConnectionHostError

        try:
            host, _port, _database = await resolve_source_db_endpoint(
                creds, config, tenant_session=tenant_session, project_id=project_id,
            )
        except MissingConnectionHostError:
            return None
        return str(host) if host else None

    # hadoop_spark / sqlserver: no fallback exists; the driver would invent one.
    return None


async def assert_introspection_endpoint_allowed(
    connector: str, creds: dict, config: dict, *,
    tenant_session=None, project_id=None,
) -> None:
    """Refuse to introspect an endpoint the platform must not dial (Bug-6216).

    Every public entry point in this module goes through here. These are the
    endpoints that accept an ad-hoc, never-persisted host from a form and open
    a real socket with it, and whose error text tells the caller exactly what
    is listening -- the SSRF/port-scan surface the guard exists for.

    Fails CLOSED on a host-bearing connector that names no host: the drivers
    supply their own defaults (SQL Server's ODBC builder used to default
    ``SERVER`` to ``localhost``), so "no host" was previously read as "nothing
    to check" while the connection still dialled the service's own container.
    """
    from shared.security.source_host_policy import (
        EXPLICITLY_HOSTLESS_CONNECTORS,
        SourceHostBlockedError,
        assert_service_account_endpoints_allowed,
        assert_source_host_allowed,
    )

    # R2 finding 4: BigQuery has no host FIELD, but the uploaded service-account
    # JSON carries its own OAuth endpoints, which are tenant-controlled
    # outbound URLs. Police those before the client is constructed.
    if connector == "bigquery":
        try:
            assert_service_account_endpoints_allowed(_bq_sa_info(creds))
        except (ValueError, TypeError) as exc:
            if isinstance(exc, SourceHostBlockedError):
                raise
            raise SourceHostBlockedError(
                f"The BigQuery service-account key could not be read: {exc}"
            ) from exc
        return

    if connector in EXPLICITLY_HOSTLESS_CONNECTORS:
        return

    host = await _endpoint_host(
        connector, creds, config,
        tenant_session=tenant_session, project_id=project_id,
    )
    if not host:
        raise SourceHostBlockedError(
            f"The {connector} connection does not specify a host. Set the "
            "server address in the connection credentials."
        )
    await assert_source_host_allowed(host, connector=connector)


async def _test_pg(creds, config, *, tenant_session=None, project_id=None):
    from shared.source_pool import acquire_source_connection
    host, port, database = await resolve_source_db_endpoint(
        creds, config, tenant_session=tenant_session, project_id=project_id,
    )
    user = creds.get("user") or creds.get("username", "postgres")
    password = creds.get("password", "")
    _audit_log("introspect.test", "postgresql SELECT 1")
    async with acquire_source_connection(_introspect_scope(tenant_session, project_id), host, port, database, user, password) as conn:
        await conn.fetch("SELECT 1")


async def _test_bq(creds, config):
    def _run():
        from google.cloud import bigquery
        # R3 finding 8: this duplicated the parse instead of using the single
        # parse point, so the "every construction site goes through here" claim
        # was true only because an upstream entry-point guard happened to run
        # first. One funnel, no second copy to forget.
        sa_info = _bq_sa_info(creds)
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
    async with _audited_introspection(conn_obj, "introspect.test", tenant_slug=session_tenant_id(tenant_session)):
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

    # Bug-6216: refuse before the socket is opened, and surface the policy
    # reason as the user-visible detail so the operator understands why.
    from shared.security.source_host_policy import SourceHostBlockedError
    try:
        await assert_introspection_endpoint_allowed(
            connector, creds, config,
            tenant_session=tenant_session, project_id=project_id,
        )
    except SourceHostBlockedError as exc:
        return False, str(exc)

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
    except PoolScopeError:
        # Bug-8039: an internal fail-closed guard (no tenant scope) is not a
        # connectivity failure — do not surface its developer-facing text as the
        # user's "Test Connection" detail. This should not be reachable (callers
        # pass a tenant-bound session) but is caught defensively.
        logger.error("Connection test invoked without a tenant scope", exc_info=True)
        return False, (
            "Connection test could not run in this context. Please retry from the "
            "connection editor."
        )
    except Exception as exc:
        return False, _redact_secrets_in_error(str(exc), creds)

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
    async with acquire_source_connection(_introspect_scope(tenant_session, project_id), host, port, database, user, password) as conn:
        sql = (
            "SELECT table_schema, table_name, table_type "
            "FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
        )
        args = []
        if schema:
            sql += "AND table_schema = $1 "
            args.append(schema)
        sql += "ORDER BY table_schema, table_name LIMIT 501"
        rows = await conn.fetch(sql, *args)
        # Bug-7169: detect truncation and warn. The extra +1 row over the
        # display cap is fetched solely to detect the boundary.
        truncated = len(rows) > 500
        if truncated:
            logger.warning(
                "Bug-7169: PostgreSQL/Redshift table discovery returned "
                "500+ results (schema=%s); listing is truncated.",
                schema or "(all)",
            )
            rows = rows[:500]
        result = [
            {"schema": r["table_schema"], "table": r["table_name"], "type": r["table_type"]}
            for r in rows
        ]
        return result, truncated


async def _discover_tables_bq(creds, config, *, schema=None, **_kw):
    def _run():
        # F-014-03: resolve project via config -> creds -> service-account.
        client, project = _open_bq_client(creds, config)
        truncated = False
        if not schema:
            datasets = list(client.list_datasets(project=project, max_results=51))
            # Bug-7169: detect dataset-level truncation.
            if len(datasets) > 50:
                logger.warning(
                    "Bug-7169: BigQuery dataset discovery returned 50+ "
                    "datasets (project=%s); listing is truncated.",
                    project,
                )
                datasets = datasets[:50]
                truncated = True
        else:
            bq_dataset = extract_dataset("bigquery", schema)
            datasets = [client.dataset(bq_dataset, project=project)]
        tables = []
        for ds_ref in datasets:
            ds_id = ds_ref.dataset_id if hasattr(ds_ref, "dataset_id") else str(ds_ref)
            ds_tables = list(client.list_tables(f"{project}.{ds_id}", max_results=201))
            if len(ds_tables) > 200:
                logger.warning(
                    "Bug-7169: BigQuery table discovery in dataset %s "
                    "returned 200+ tables; listing is truncated.",
                    ds_id,
                )
                truncated = True
                ds_tables = ds_tables[:200]
            for tbl in ds_tables:
                tables.append({
                    "schema": ds_id,
                    "table": tbl.table_id,
                    "type": tbl.table_type,
                })
        client.close()
        return tables, truncated
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
        return tables, False
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
                    "ORDER BY table_schema, table_name LIMIT 501",
                    (schema,),
                )
            else:
                cursor.execute(
                    "SELECT table_schema, table_name, table_type "
                    "FROM information_schema.tables "
                    "WHERE table_schema NOT IN ('INFORMATION_SCHEMA') "
                    "ORDER BY table_schema, table_name LIMIT 501"
                )
            rows = cursor.fetchall()
            # Bug-7169: detect truncation (fetch 501, display 500).
            truncated = len(rows) > 500
            if truncated:
                logger.warning(
                    "Bug-7169: Snowflake table discovery returned 500+ "
                    "results (schema=%s); listing is truncated.",
                    schema or "(all)",
                )
                rows = rows[:500]
            tables = [
                {"schema": row[0], "table": row[1], "type": row[2]}
                for row in rows
            ]
            cursor.close()
            return tables, truncated
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
                    "SELECT TOP 501 TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE "
                    "FROM INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_SCHEMA = ? "
                    "ORDER BY TABLE_SCHEMA, TABLE_NAME",
                    (schema,),
                )
            else:
                await cursor.execute(
                    "SELECT TOP 501 TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE "
                    "FROM INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA') "
                    "ORDER BY TABLE_SCHEMA, TABLE_NAME"
                )
            rows = await cursor.fetchall()
            truncated = len(rows) > 500
            if truncated:
                logger.warning(
                    "Bug-7169: SQL Server table discovery returned 500+ "
                    "results (schema=%s); listing is truncated.",
                    schema or "(all)",
                )
                rows = rows[:500]
            tables = [
                {"schema": row[0], "table": row[1], "type": row[2]}
                for row in rows
            ]
            await cursor.close()
            return tables, truncated
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
) -> dict[str, Any]:
    """Return tables from the source database.

    Response: ``{"tables": [{"schema", "table", "type"}], "truncated": bool}``.
    Truncation is a flag (F-014-05 / G-014-02), never a selectable sentinel row.
    """
    connector = _resolve_connector(conn_obj)
    fn = _DISCOVER_TABLES_DISPATCH.get(connector)
    if fn is None:
        raise ValueError(f"Unsupported connector type: {connector!r}")

    creds = _decrypt(conn_obj.encrypted_credentials)
    config = conn_obj.config or {}
    # Bug-6216: policy check before any socket is opened.
    await assert_introspection_endpoint_allowed(
        connector, creds, config,
        tenant_session=tenant_session,
        project_id=getattr(conn_obj, "project_id", None),
    )
    async with _audited_introspection(conn_obj, "introspect.discover_tables", tenant_slug=session_tenant_id(tenant_session)):
        raw = await fn(
            creds, config,
            schema=schema,
            tenant_session=tenant_session,
            project_id=getattr(conn_obj, "project_id", None),
        )
        return normalize_discover_payload(raw)


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
    async with acquire_source_connection(_introspect_scope(tenant_session, project_id), host, port, database, user, password) as conn:
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
        # F-014-03: resolve project via config -> creds -> service-account.
        client, project = _open_bq_client(creds, config)
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


# ---------------------------------------------------------------------------
# Primary-key discovery (Bug-8618)
# ---------------------------------------------------------------------------
#
# ``ModelColumn.is_primary_key`` existed for a long time with no producer: no
# connector's column discovery read the source catalogue's key metadata, so
# the flag was only ever true if a human ticked it by hand. Measured on the
# running acme-demo stack, 2 of 713 columns carried it, and the deployed
# ``modely`` snapshot carried none across all 212 of its columns even though
# every one of its dimension tables is keyed in the source DDL. Every
# consumer — the pocket row-population proof's non-duplication leg, the JDBC
# catalogue's index metadata, the LookML export's ``primary_key`` — was
# reasoning from a field that was almost always False. Contract invariant 5:
# "a model with no declared keys is a data gap to close via introspection, not
# a permanent unknown."
#
# Discovery is stamped in ONE place — the public ``discover_columns`` /
# ``profile_table`` wrappers below — rather than inside each of the ten
# per-connector readers, so a connector cannot be silently left out and the
# two entry points cannot disagree about the same table.
#
# Scope: PRIMARY KEY constraints only. A single-column UNIQUE constraint is
# equally good evidence of non-duplication, but ``is_primary_key`` is read by
# consumers that mean "primary key" literally (LookML ``primary_key: yes``,
# JDBC ``getPrimaryKeys``), so widening it here would make those lie. A
# separate unique-key field is tracked as its own issue.
#
# Failure is SOFT: a catalogue that cannot be read leaves the flag untouched
# rather than failing the discovery call. An undiscovered key costs a forgone
# acceleration; a failed discovery call costs the modeller their columns.

_PK_SQL_INFORMATION_SCHEMA = (
    "SELECT kcu.column_name AS column_name "
    "FROM information_schema.table_constraints tc "
    "JOIN information_schema.key_column_usage kcu "
    "  ON kcu.constraint_name = tc.constraint_name "
    " AND kcu.constraint_schema = tc.constraint_schema "
    " AND kcu.table_name = tc.table_name "
    "WHERE tc.constraint_type = 'PRIMARY KEY' "
)


async def _pk_columns_pg(creds, config, *, schema, table,
                         tenant_session=None, project_id=None) -> set[str]:
    from shared.source_pool import acquire_source_connection
    host, port, database = await resolve_source_db_endpoint(
        creds, config, tenant_session=tenant_session, project_id=project_id,
    )
    user = creds.get("user") or creds.get("username", "postgres")
    password = creds.get("password", "")
    async with acquire_source_connection(
        _introspect_scope(tenant_session, project_id),
        host, port, database, user, password,
    ) as conn:
        rows = await conn.fetch(
            _PK_SQL_INFORMATION_SCHEMA
            + "AND tc.table_schema = $1 AND tc.table_name = $2",
            schema, table,
        )
        return {r["column_name"] for r in rows}


async def _pk_columns_bq(creds, config, *, schema, table, **_kw) -> set[str]:
    def _run():
        client, project = _open_bq_client(creds, config)
        try:
            # BigQuery exposes PRIMARY KEY as an UNENFORCED constraint through
            # the dataset-scoped INFORMATION_SCHEMA views. Older datasets /
            # regions do not have them at all, which raises and is handled by
            # the soft-failure path in the caller.
            # The dataset path is built through the shared qualifier, not
            # interpolated raw: ``schema`` is a modeller-supplied dataset name
            # and this is the one place in the PK readers that has to embed an
            # identifier in SQL text (the table name is a bound parameter).
            from shared.connector_qualify import quote_table_ref
            constraints = quote_table_ref(
                "bigquery",
                qualify_table_name(
                    "bigquery",
                    "INFORMATION_SCHEMA.TABLE_CONSTRAINTS",
                    schema=schema,
                    project_id=project,
                ),
            )
            key_usage = quote_table_ref(
                "bigquery",
                qualify_table_name(
                    "bigquery",
                    "INFORMATION_SCHEMA.KEY_COLUMN_USAGE",
                    schema=schema,
                    project_id=project,
                ),
            )
            sql = (
                f"SELECT kcu.column_name AS column_name "
                f"FROM {constraints} tc "
                f"JOIN {key_usage} kcu "
                f"  ON tc.constraint_name = kcu.constraint_name "
                f" AND tc.table_name = kcu.table_name "
                f"WHERE tc.constraint_type = 'PRIMARY KEY' "
                f"  AND tc.table_name = @tbl"
            )
            from google.cloud import bigquery  # type: ignore
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("tbl", "STRING", table),
                ]
            )
            return {r["column_name"] for r in client.query(sql, job_config=job_config)}
        finally:
            client.close()
    return await _bounded(_run)


async def _pk_columns_spark(creds, config, *, schema, table, **_kw) -> set[str] | None:
    # Hive/Spark Thrift exposes no portable primary-key catalogue: DESCRIBE
    # returns column names and types only, and constraint support varies by
    # metastore.
    #
    # This returns ``None`` — "cannot know" — NOT an empty set. An empty set is
    # a positive claim that the source declares no primary key, and
    # ``sync_columns`` acts on that claim by CLEARING any stored flag. On a
    # connector that simply cannot be asked, that would wipe a modeller's
    # hand-declared keys on every schema re-sync and quietly withdraw pocket
    # acceleration the population proof had already granted. ``None`` makes
    # ``_stamp_primary_keys`` omit the field, which is the same treatment a
    # failed catalogue read gets.
    return None


async def _pk_columns_snowflake(creds, config, *, schema, table, **_kw) -> set[str]:
    def _run():
        conn = _open_snowflake(
            creds, config, login_timeout=_get_source_timeout(),
            network_timeout=_get_source_timeout(),
        )
        try:
            cursor = conn.cursor()
            # Snowflake's INFORMATION_SCHEMA.KEY_COLUMN_USAGE does not list
            # primary keys; SHOW PRIMARY KEYS is the supported read. The
            # identifiers are quoted through the shared qualifier rather than
            # interpolated raw.
            from shared.connector_qualify import quote_table_ref
            ref = quote_table_ref("snowflake", f"{schema}.{table}")
            cursor.execute(f"SHOW PRIMARY KEYS IN TABLE {ref}")
            description = [d[0].lower() for d in (cursor.description or [])]
            try:
                idx = description.index("column_name")
            except ValueError:
                return set()
            names = {row[idx] for row in cursor.fetchall() if row[idx]}
            cursor.close()
            return names
        finally:
            conn.close()
    return await _bounded(_run)


async def _pk_columns_sqlserver(creds, config, *, schema, table, **_kw) -> set[str]:
    import aioodbc
    dsn = _build_sqlserver_dsn(creds, config)

    async def _run():
        conn = await aioodbc.connect(dsn=dsn)
        try:
            cursor = await conn.cursor()
            await cursor.execute(
                _PK_SQL_INFORMATION_SCHEMA
                + "AND tc.table_schema = ? AND tc.table_name = ?",
                (schema, table),
            )
            names = {row[0] for row in await cursor.fetchall() if row[0]}
            await cursor.close()
            return names
        finally:
            await conn.close()

    return await asyncio.wait_for(_run(), timeout=_get_source_timeout())


_PK_COLUMNS_DISPATCH = {
    "postgresql": _pk_columns_pg,
    "bigquery": _pk_columns_bq,
    "hadoop_spark": _pk_columns_spark,
    "redshift": _pk_columns_pg,
    "snowflake": _pk_columns_snowflake,
    "sqlserver": _pk_columns_sqlserver,
}


async def _stamp_primary_keys(
    columns: list[dict],
    *,
    connector: str,
    creds: dict,
    config: dict,
    schema: str,
    table: str,
    tenant_session: Any = None,
    project_id: Any = None,
) -> list[dict]:
    """Set ``is_primary_key`` on each discovered column from the source.

    Every returned column carries the key EXPLICITLY (``False`` when the table
    has no primary key), so a consumer can distinguish "not a key" from "this
    payload does not describe keys" by the presence of the field. When the
    catalogue read fails the field is OMITTED entirely rather than written as
    ``False`` — a failed read must not be mistaken for a proven absence, and
    ``sync_columns`` treats an absent field as "leave the stored value alone".
    """
    fn = _PK_COLUMNS_DISPATCH.get(connector)
    if fn is None or not columns:
        return columns
    try:
        pk_names = await fn(
            creds, config,
            schema=schema, table=table,
            tenant_session=tenant_session, project_id=project_id,
        )
    except Exception as exc:  # noqa: BLE001 — soft failure, see module note
        logger.warning(
            "Bug-8618: primary-key discovery failed for %s %s.%s (%s); "
            "is_primary_key is left at its stored value for these columns.",
            connector, schema, table, exc,
        )
        return columns
    if pk_names is None:
        # The connector has no key catalogue to ask (Hive/Spark). Indistinguishable
        # from a failed read as far as the consumer is concerned: say nothing.
        logger.debug(
            "Bug-8618: %s exposes no primary-key catalogue; is_primary_key is "
            "left at its stored value for %s.%s.", connector, schema, table,
        )
        return columns
    for col in columns:
        col["is_primary_key"] = col.get("column_name") in pk_names
    return columns


async def discover_columns(
    conn_obj: Any,
    *,
    schema: str,
    table: str,
    tenant_session: Any = None,
    with_primary_keys: bool = True,
) -> list[dict]:
    """Return columns for a single table.

    Each entry: ``{"column_name": str, "data_type": str, "is_nullable": bool,
    "is_primary_key": bool}``. ``is_primary_key`` is read from the source
    catalogue's PRIMARY KEY constraints (Bug-8618) and is OMITTED when that
    read fails, so "unknown" is distinguishable from "not a key".

    ``with_primary_keys=False`` skips that second catalogue read entirely
    (Bug-8795). The key probe is a SEPARATE round trip — on BigQuery a real,
    scanned-bytes-billed ``client.query()`` against
    ``INFORMATION_SCHEMA.TABLE_CONSTRAINTS`` — so a caller that only needs the
    column list should not pay for it, and should not inherit its failure
    surface either. The default is ``True`` so every existing caller is
    byte-identical.
    """
    connector = _resolve_connector(conn_obj)
    fn = _DISCOVER_COLUMNS_DISPATCH.get(connector)
    if fn is None:
        raise ValueError(f"Unsupported connector type: {connector!r}")

    creds = _decrypt(conn_obj.encrypted_credentials)
    config = conn_obj.config or {}
    project_id = getattr(conn_obj, "project_id", None)
    # Bug-6216: policy check before any socket is opened.
    await assert_introspection_endpoint_allowed(
        connector, creds, config,
        tenant_session=tenant_session,
        project_id=project_id,
    )
    async with _audited_introspection(conn_obj, "introspect.discover_columns", tenant_slug=session_tenant_id(tenant_session)):
        columns = await fn(
            creds, config,
            schema=schema,
            table=table,
            tenant_session=tenant_session,
            project_id=project_id,
        )
        if not with_primary_keys:
            return columns
        return await _stamp_primary_keys(
            columns, connector=connector, creds=creds, config=config,
            schema=schema, table=table,
            tenant_session=tenant_session, project_id=project_id,
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
    scope = _introspect_scope(tenant_session, project_id)
    _audit_log("introspect.profile", f"postgresql {schema}.{table}")
    # Bug-8416 caller audit: column discovery and cardinality are independent
    # catalogue/data statements. Release after discovery so checkout-age
    # retirement measures each operation, not their combined wall-clock time.
    async with acquire_source_connection(
        scope, host, port, database, user, password,
    ) as conn:
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

    async with acquire_source_connection(
        scope, host, port, database, user, password,
    ) as conn:
        row_count = await _cardinality_pg(conn, schema, table, columns)
    return columns, row_count


def _to_int(val) -> int:
    if val is None:
        return 0
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return 0


def _profile_max_distinct_columns() -> int:
    """Maximum columns profiled for cardinality per table (Bug-7157).

    Each ``COUNT(DISTINCT col)`` in the cardinality probe adds a hash
    aggregation to the single scan query. On a wide, high-row-count table
    (e.g. BigQuery fact tables) this controls the fan-out width.
    Override with ``PROFILE_MAX_DISTINCT_COLUMNS`` env var.
    A value <= 0 disables the cap (reverts to legacy 40-column behaviour).
    """
    import os
    try:
        return int(os.getenv("PROFILE_MAX_DISTINCT_COLUMNS", "20"))
    except (TypeError, ValueError):
        return 20


def _build_cardinality_sql(connector: str, schema: str, table: str,
                           columns: list[dict]) -> tuple[str, list[dict], bool]:
    """Build a cardinality probe ``SELECT`` for *connector*.

    PostgreSQL / Redshift / Snowflake / SQL Server keep exact
    ``COUNT(*)`` + ``COUNT(DISTINCT col)``. BigQuery uses
    ``APPROX_COUNT_DISTINCT`` with **no** ``COUNT(*)`` (row count comes from
    ``__TABLES__`` metadata — F-014-09). Spark uses ``approx_count_distinct``
    plus ``COUNT(*)``.

    Returns ``(sql, sampled_columns, count_star_leading)`` where
    ``count_star_leading`` is True when ``row[0]`` is the table row count.
    """
    from shared.connector_qualify import quote_table_ref
    max_cols = _profile_max_distinct_columns()
    cap = max_cols if max_cols > 0 else 40
    sample = columns[:cap]
    qualified = quote_table_ref(connector, f"{schema}.{table}")
    if connector == "bigquery":
        parts = [
            f"APPROX_COUNT_DISTINCT({quote_identifier(connector, col['column_name'])}) AS _cd_{i}"
            for i, col in enumerate(sample)
        ]
        sql = f"SELECT {', '.join(parts) if parts else '1'} FROM {qualified}"
        return sql, sample, False
    if connector == "hadoop_spark":
        parts = ["COUNT(*) AS _row_count"]
        for i, col in enumerate(sample):
            qi = quote_identifier(connector, col["column_name"])
            parts.append(f"approx_count_distinct({qi}) AS _cd_{i}")
        sql = f'SELECT {", ".join(parts)} FROM {qualified}'
        return sql, sample, True
    parts = ["COUNT(*) AS _row_count"]
    for i, col in enumerate(sample):
        qi = quote_identifier(connector, col["column_name"])
        parts.append(f"COUNT(DISTINCT {qi}) AS _cd_{i}")
    sql = f'SELECT {", ".join(parts)} FROM {qualified}'
    return sql, sample, True


def _apply_cardinality_row(row, sampled: list[dict], *, count_star_leading: bool = True) -> int:
    """Populate ``approx_distinct`` on *sampled* from a cardinality result row.

    When *count_star_leading* is True, ``row[0]`` is ``COUNT(*)`` and distinct
    counts follow. When False (BigQuery APPROX probe), every column is a
    distinct count and the function returns 0 for row_count (caller uses
    ``__TABLES__`` / table metadata).
    """
    if row is None:
        return 0
    offset = 1 if count_star_leading else 0
    row_count = _to_int(row[0]) if count_star_leading else 0
    for i, col in enumerate(sampled):
        col["approx_distinct"] = _to_int(row[i + offset])
    return row_count


async def _cardinality_pg(conn, schema: str, table: str, columns: list[dict]) -> int:
    """Query COUNT(*) and COUNT(DISTINCT col) for up to 40 columns."""
    if not columns:
        return 0
    sql, sample, count_star_leading = _build_cardinality_sql("postgresql", schema, table, columns)
    row = await conn.fetchrow(sql)
    return _apply_cardinality_row(row, sample, count_star_leading=count_star_leading)


def _bq_tables_row_count_sql(project: str, schema: str) -> str:
    """Metadata SELECT against dataset ``__TABLES__`` (F-014-09). Table id is bound."""
    from shared.connector_qualify import quote_identifier as qi
    dataset = extract_dataset("bigquery", schema)
    q_project = qi("bigquery", project)
    q_dataset = qi("bigquery", dataset)
    return (
        f"SELECT row_count FROM {q_project}.{q_dataset}.`__TABLES__` "
        f"WHERE table_id = @table_id"
    )


def _bq_tables_row_count(client, project: str, schema: str, table: str) -> int | None:
    """Read ``row_count`` from dataset ``__TABLES__`` (metadata, not a scan)."""
    from google.cloud import bigquery
    sql = _bq_tables_row_count_sql(project, schema)
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("table_id", "STRING", table),
        ],
    )
    rows = list(client.query(sql, job_config=job_config).result())
    if not rows:
        return None
    return _to_int(rows[0][0])


async def _profile_bq(creds, config, *, schema, table, **_kw):
    def _run():
        from google.cloud import bigquery
        # F-014-03: resolve project via config -> creds -> service-account.
        client, project = _open_bq_client(creds, config)
        if "." in table:
            table_ref = f"{schema}.{table}"
        else:
            table_ref = qualify_table_name("bigquery", table, schema=schema, project_id=project)
        bq_table = client.get_table(table_ref)
        row_count = bq_table.num_rows or 0
        try:
            meta_count = _bq_tables_row_count(client, project, schema, table)
            if meta_count is not None:
                row_count = meta_count
        except Exception as exc:  # noqa: BLE001 — metadata is best-effort
            logger.warning(
                "BigQuery __TABLES__ row_count failed for %s.%s: %s",
                schema, table, exc,
            )
        columns = [
            {
                "column_name": f.name,
                "data_type": f.field_type.lower(),
                "is_nullable": f.mode != "REQUIRED",
                "approx_distinct": None,
            }
            for f in bq_table.schema
        ]
        # F-014-09: APPROX_COUNT_DISTINCT — never COUNT(*) / COUNT(DISTINCT)
        # on BigQuery profile. Row count comes from __TABLES__ / num_rows.
        try:
            sql, sampled, count_star_leading = _build_cardinality_sql(
                "bigquery", schema, table, columns,
            )
            profile_job_config = None
            max_bytes = _resolve_bq_max_bytes(config or {})
            if max_bytes is not None:
                profile_job_config = bigquery.QueryJobConfig(
                    maximum_bytes_billed=max_bytes,
                )
            result = list(client.query(sql, job_config=profile_job_config).result())
            if result:
                _apply_cardinality_row(
                    result[0], sampled, count_star_leading=count_star_leading,
                )
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
            sql, sampled, count_star_leading = _build_cardinality_sql("hadoop_spark", schema, table, columns)
            cursor.execute(sql)
            row_count = _apply_cardinality_row(
                cursor.fetchone(), sampled, count_star_leading=count_star_leading,
            )
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
                sql, sampled, count_star_leading = _build_cardinality_sql("snowflake", schema, table, columns)
                cursor.execute(sql)
                row_count = _apply_cardinality_row(
                    cursor.fetchone(), sampled, count_star_leading=count_star_leading,
                )
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
                sql, sampled, count_star_leading = _build_cardinality_sql("sqlserver", schema, table, columns)
                await cursor.execute(sql)
                row_count = _apply_cardinality_row(
                    await cursor.fetchone(), sampled, count_star_leading=count_star_leading,
                )
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
    ``approx_distinct`` (may be ``None`` if cardinality wasn't fetched) and
    ``is_primary_key`` from the source catalogue (Bug-8618) — the same
    stamping ``discover_columns`` applies, so the "add classified tables"
    flow and the "sync columns" button persist the same key metadata for the
    same table instead of only one of them carrying it.
    """
    connector = _resolve_connector(conn_obj)
    if is_truncation_profile_ref(schema, table):
        raise ValueError(
            "Truncation markers are not real tables and cannot be profiled "
            "(F-014-05)."
        )
    fn = _PROFILE_DISPATCH.get(connector)
    if fn is None:
        raise ValueError(f"Unsupported connector type: {connector!r}")

    creds = _decrypt(conn_obj.encrypted_credentials)
    config = conn_obj.config or {}
    project_id = getattr(conn_obj, "project_id", None)
    # Bug-6216: policy check before any socket is opened.
    await assert_introspection_endpoint_allowed(
        connector, creds, config,
        tenant_session=tenant_session,
        project_id=project_id,
    )
    async with _audited_introspection(conn_obj, "introspect.profile", tenant_slug=session_tenant_id(tenant_session)):
        columns, row_count = await fn(
            creds, config,
            schema=schema,
            table=table,
            tenant_session=tenant_session,
            project_id=project_id,
        )
        columns = await _stamp_primary_keys(
            columns, connector=connector, creds=creds, config=config,
            schema=schema, table=table,
            tenant_session=tenant_session, project_id=project_id,
        )
        return columns, row_count
