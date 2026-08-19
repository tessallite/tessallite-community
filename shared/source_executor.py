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
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from shared.config.source_db import resolve_source_db_endpoint
from shared.schemas.connection_type import normalize_connection_type

logger = logging.getLogger(__name__)


class UnconfiguredConnectionError(ValueError):
    """Raised when a query targets a placeholder / unconfigured connection.

    Bug-5860: connections created during credentialless project import carry
    ``config={"unconfigured": True}``. Queries against them must fail closed
    rather than falling through to the system fallback host with empty
    credentials.
    """
    pass


def _guard_unconfigured(conn_obj: Any) -> None:
    """Raise early if the connection is a placeholder with no real credentials.

    Called at every public source-executor entry point so that queries
    against unconfigured connections are rejected deterministically instead
    of silently connecting with empty credentials.
    """
    config = getattr(conn_obj, "config", None) or {}
    if config.get("unconfigured"):
        name = getattr(conn_obj, "display_name", None) or "unknown"
        raise UnconfiguredConnectionError(
            f"Connection '{name}' is not configured (placeholder from import). "
            f"Configure real credentials before querying."
        )


# F-030-22: source-access audit tracing defaults ON. This is the bypass-detection
# control the architecture relies on ("source access is traced via [SOURCE_AUDIT]");
# leaving it opt-in meant it produced nothing unless an operator set the env var,
# so the control was effectively absent by default. An explicit
# TESSALLITE_SOURCE_AUDIT="0" is now required to silence it.
_SOURCE_AUDIT = os.environ.get("TESSALLITE_SOURCE_AUDIT", "1") != "0"


def _parse_bool_config(val: Any) -> bool:
    """Parse a config value as boolean, handling JSON booleans and strings.

    Treats ``True``, ``1``, ``"true"``, ``"1"``, ``"yes"`` as truthy.
    Everything else (``False``, ``0``, ``"false"``, ``"0"``, ``""``,
    ``None``, missing) is falsy.  Needed because JSONB stores proper
    booleans but env-var-sourced or string-typed configs may carry
    ``"false"`` which Python ``bool("false")`` evaluates as ``True``.
    """
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes")
    return False


def _resolve_bq_max_bytes(
    config: dict,
    *,
    fail_closed: bool = True,
) -> int | None:
    """Resolve the BigQuery ``maximum_bytes_billed`` cap.

    Bug-6528: the cap is ONLY active when ``bq_cost_guard`` is enabled on
    the connection (context-gated).  Normal connections that omit the guard
    are never capped, even if ``bq_max_bytes_billed`` happens to be present.

    ``bq_cost_guard`` (bool, default false):
        When truthy, the connection is a cost-guarded context (e.g. a public
        demo BQ source).  ``bq_max_bytes_billed`` is then REQUIRED.

    ``bq_max_bytes_billed`` (int or int-string):
        The cap in bytes.  Only consulted when the guard is on.

    *fail_closed* (default True):
        When True (query paths), a missing/invalid/zero cap raises
        ``ValueError``.  When False (DDL paths), returns None instead --
        DDL does not scan data, so a missing cap is harmless.

    Returns the resolved cap (positive int) or ``None`` (no cap).
    """
    cost_guard = _parse_bool_config(config.get("bq_cost_guard"))
    if not cost_guard:
        # No guard = no cap.  Normal tenants are never capped.
        # Bug-7746: log that this BQ query path is uncapped so operators
        # can audit which connections lack the cost guard. Demo-locked
        # tenants SHOULD always have bq_cost_guard set; if this log
        # appears for a demo tenant it indicates a deploy-path gap.
        logger.debug(
            "Bug-7746: BigQuery query running without bq_cost_guard "
            "(uncapped). If this is a demo tenant, ensure "
            "bq_cost_guard+bq_max_bytes_billed are set on the connection."
        )
        return None

    # Guard is on: bq_max_bytes_billed is required.
    raw = config.get("bq_max_bytes_billed")

    if not raw and raw != 0:
        if fail_closed:
            raise ValueError(
                "bq_cost_guard is enabled on this connection but "
                "bq_max_bytes_billed is not set -- refusing to execute "
                "uncapped. Set bq_max_bytes_billed in the connection config."
            )
        return None

    try:
        cap = int(raw)
    except (ValueError, TypeError):
        if fail_closed:
            raise ValueError(
                f"bq_max_bytes_billed is set to {raw!r} which "
                f"is not a valid integer -- refusing to execute uncapped. "
                f"Fix the connection config."
            )
        return None

    if cap <= 0:
        if fail_closed:
            raise ValueError(
                "bq_cost_guard is enabled but bq_max_bytes_billed is "
                f"{cap} (no cap) -- refusing to execute uncapped. "
                "Set a positive bq_max_bytes_billed value."
            )
        return None

    return cap


class QueryTimeoutError(Exception):
    """Raised when a source-database query exceeds the configured timeout."""
    pass


class SourceResultTooLargeError(Exception):
    """Raised when a routed user query returns more rows than the configured cap.

    F-014-02 (Bug-7984): the routed user-query path enforces ``result.max_rows``
    inside the shared execution gateway so the cap can never be bypassed by a
    caller that opens its own driver. The query-router adapter translates this
    into its own ``ResultTooLargeError`` so the HTTP layer's existing handling is
    unchanged.
    """
    pass


def _open_snowflake(
    creds: dict,
    config: dict,
    *,
    login_timeout: int | None = None,
    network_timeout: int | None = None,
    keep_alive: bool = False,
):
    """Open a Snowflake connection from decrypted credentials and config.

    Bug-7159: *keep_alive* enables ``client_session_keep_alive`` so long-running
    batch operations (cross-database materialisation, streaming inserts) keep the
    Snowflake session alive past the default 4-hour idle timeout. Short query-path
    calls leave it off (default) to avoid unnecessary heartbeat traffic.
    """
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
    if keep_alive:
        kwargs["client_session_keep_alive"] = True
    return snowflake.connector.connect(**kwargs)


def _escape_odbc_value(val: str) -> str:
    """Escape a value for ODBC connection string: wrap in braces if it
    contains semicolons, braces, or equals signs."""
    if any(c in val for c in (";", "=", "{", "}")):
        return "{" + val.replace("}", "}}") + "}"
    return val


def _bq_service_account_credentials(sa_info: dict):
    """Build BigQuery credentials from a tenant-uploaded service-account blob.

    Bug-6216 R2 finding 4: the blob is tenant-controlled and carries its OWN
    OAuth endpoints. ``google.oauth2.service_account.Credentials`` takes its
    token endpoint from the JSON's ``token_uri`` and POSTs the signed assertion
    there on the first refresh, so a key file with
    ``"token_uri": "http://169.254.169.254/..."`` is an outbound request from
    inside the platform network -- the same egress the host policy exists to
    bound, arriving through the credential rather than the host field.

    Every construction site goes through here so the check cannot be present on
    one path and missing on the other four.
    """
    from google.oauth2 import service_account
    from shared.security.source_host_policy import (
        assert_service_account_endpoints_allowed,
    )

    assert_service_account_endpoints_allowed(sa_info)
    return service_account.Credentials.from_service_account_info(sa_info)


def _assert_execution_host_allowed(host: str | None, connector: str) -> None:
    """Refuse to DIAL a host the egress policy blocks (Bug-6216 R4).

    Three rounds of review kept finding the same shape: the policy was added to
    the caller the finding named, and a sibling caller of the same primitive was
    left open. The introspection entry points were guarded; the EXECUTION path,
    which carries every user query, refresh and CTAS, was not -- so a connection
    saved with no host still resolved to ``source_db.fallback_host`` (default
    ``localhost``) with a tenant-chosen PORT and opened the socket that Test
    Connection correctly refuses.

    So the check lives here, at the points where a host becomes a socket, rather
    than at whichever entry point was last reported. It is deliberately the
    SYNCHRONOUS literal check: no DNS on the query hot path.

    That means a NAME is not judged here. Names are resolved and judged where a
    host ENTERS the system -- ``connections._reject_blocked_source_host`` on
    create/update -- so a name pointing at loopback cannot be persisted in the
    first place. (Until R5 the write path was literal-only too, and this
    docstring called the gap "a DNS-rebinding race"; it was not. Names like
    ``127.0.0.1.nip.io`` resolve to loopback every time, so the exposure was
    static, not a race.) What genuinely remains a race is rebinding AFTER a
    legitimate save: a host that resolved to a public address at write time and
    to a private one at query time. Pinning the validated IP into the driver
    would break TLS hostname verification against the customer's certificate,
    which is the worse trade.
    """
    from shared.security.source_host_policy import check_source_host_literal

    check_source_host_literal(host or "", connector=connector)


def _spark_connect_kwargs(creds: dict) -> dict:
    """Build the PyHive connect kwargs from decrypted Spark/Hive credentials.

    Bug-6216 R4: this existed as THREE copies (query, bulk insert, raw
    connection), each defaulting ``host`` to ``"localhost"``. A fix applied to
    one copy leaves the other two dialling the service's own container on a
    tenant-chosen port -- the same duplication-hides-a-sibling shape that made
    this lane's earlier rounds incomplete. One builder, one policy check.
    """
    host = creds.get("host")
    if not host:
        raise ValueError(
            "Spark/Hive connection is missing a host. Set the Thrift server "
            "host in the connection credentials."
        )
    _assert_execution_host_allowed(host, "hadoop_spark")
    kwargs: dict = {
        "host": host,
        "port": int(creds.get("port", 10000)),
        "database": creds.get("database", "default"),
    }
    if creds.get("auth_method", "NOSASL") == "LDAP":
        kwargs["auth"] = "LDAP"
        kwargs["username"] = creds.get("username", "")
        kwargs["password"] = creds.get("password", "")
    else:
        kwargs["auth"] = "NOSASL"
    return kwargs


def _build_sqlserver_dsn(creds: dict, config: dict) -> str:
    """Build an ODBC connection string for SQL Server."""
    driver = (
        creds.get("driver")
        or config.get("driver")
        or "ODBC Driver 17 for SQL Server"
    )
    if ";" in driver or "\x00" in driver:
        raise ValueError(f"Invalid ODBC driver name: {driver!r}")
    # Bug-6216 R2 finding 3: this used to default to "localhost", so a
    # SQL Server connection saved with no host silently dialled the service's
    # OWN container on a caller-chosen port -- an egress the host policy could
    # not see, because there was no host for it to inspect. There is no
    # operator-configured fallback for this connector (unlike postgresql /
    # redshift, which resolve through ``source_db.fallback_host``), so a
    # missing host is a misconfiguration, not a default. Fail loudly, the same
    # way ``_require_spark_host`` does.
    host = creds.get("host") or config.get("host")
    if not host:
        raise ValueError(
            "SQL Server connection is missing a host. Set the server address "
            "in the connection credentials."
        )
    _assert_execution_host_allowed(host, "sqlserver")
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


def _redact_sql_literals(sql: str) -> str:
    """Replace string and numeric literals in SQL with placeholders.

    Bug-7174: the [SOURCE_AUDIT] log must never contain raw SQL literals
    (customer values, emails, tokens, etc). This uses a lightweight regex
    approach (no parse dependency on the hot audit path) that handles
    single-quoted strings, double-quoted strings that look like values
    (not identifiers), and bare numeric literals in predicate positions.

    The goal is traceability (operation shape) without data leakage.
    """
    import re
    # Replace PostgreSQL dollar-quoted strings ($$body$$ or $tag$body$tag$).
    # Must run before single-quote replacement to avoid partial matches.
    redacted = re.sub(r'\$([A-Za-z_]*)\$.*?\$\1\$', "'?'", sql, flags=re.DOTALL)
    # Replace single-quoted string literals. Handles both backslash escapes
    # (\') and SQL-standard doubled single quotes ('O''Brien' -> '?').
    # R1-F1: use ''|[^'\\] so doubled quotes are treated as in-string escape
    # rather than a boundary, collapsing 'it''s fine' into a single '?'.
    redacted = re.sub(r"'(?:''|[^'\\]|\\.)*'", "'?'", redacted)
    # Replace numeric literals that follow operators/comparisons/commas
    # (but not identifiers like table123)
    redacted = re.sub(r'(?<=[=<>!,(\s])\s*-?\d+(?:\.\d+)?(?:\s*(?=[,)\s;]|$))', ' ?', redacted)
    return redacted


def _audit_context(
    conn_obj: Any,
    purpose: str,
    *,
    tenant_slug: str | None = None,
) -> dict[str, Any]:
    """Build the structured audit context for a source touch (F-014-04 / Bug-8038).

    Derives project, connection, and connector from *conn_obj*. ``purpose`` is
    the caller-declared reason (e.g. ``user_query``, ``aggregate_refresh``,
    ``introspect``, ``bulk_load``).

    **Tenant attribution** (Fable FINDING-1): ``ProjectConnection`` lives in a
    per-tenant schema and has no ``tenant_slug`` column, so ``conn_obj`` cannot
    supply it. Callers that know the tenant (the model-service request context,
    the query-router route handler) should pass it explicitly via *tenant_slug*.
    When omitted, the field degrades to ``None`` rather than failing.
    """
    project_id = getattr(conn_obj, "project_id", None)
    resolved_tenant = (
        tenant_slug
        or getattr(conn_obj, "tenant_slug", None)
        or getattr(conn_obj, "tenant_id", None)
        or None
    )
    return {
        "tenant": resolved_tenant,
        "project": str(project_id) if project_id is not None else None,
        "connection": (
            str(getattr(conn_obj, "id", None))
            if getattr(conn_obj, "id", None) is not None
            else None
        ),
        "connector": normalize_connection_type(
            (getattr(conn_obj, "connection_type", None) or "").lower()
        )
        or None,
        "purpose": purpose or None,
    }


def _format_context(context: dict[str, Any] | None) -> str:
    """Render a context dict into a stable ``key=value`` audit fragment."""
    if not context:
        return ""
    # Fixed key order so log parsers and grep patterns are stable.
    order = ("tenant", "project", "connection", "connector", "purpose", "outcome")
    parts = []
    for key in order:
        if key in context and context[key] is not None:
            parts.append(f"{key}={context[key]}")
    # Any extra keys (duration_ms, rows, bytes) appended after the fixed set.
    for key, val in context.items():
        if key not in order and val is not None:
            parts.append(f"{key}={val}")
    return " ".join(parts)


def _audit_log(
    operation: str,
    sql: str,
    *,
    context: dict[str, Any] | None = None,
) -> None:
    """Emit a ``[SOURCE_AUDIT]`` record for a physical source touch.

    F-014-04 (Bug-8038): the record now carries a structured *context*
    (tenant, project, connection, connector, purpose, outcome) when the caller
    supplies one, so mixed-workload provenance can be reconstructed and a
    sanctioned background access can be distinguished from a user query. The
    positional ``(operation, sql)`` signature is preserved for the many
    existing call sites; those simply omit *context*.
    """
    if not _SOURCE_AUDIT:
        return
    caller = ""
    frame = inspect.currentframe()
    if frame and frame.f_back and frame.f_back.f_back:
        f = frame.f_back.f_back
        caller = f"{f.f_globals.get('__name__', '?')}:{f.f_lineno}"
    # Bug-7174: redact literals before logging to prevent customer data
    # (emails, account numbers, tokens, security predicates) from entering
    # application logs. Log the redacted fingerprint, not the raw SQL.
    redacted = _redact_sql_literals(sql)
    sql_prefix = redacted[:120].replace("\n", " ")
    ctx = _format_context(context)
    if ctx:
        logger.info(
            "[SOURCE_AUDIT] %s %s caller=%s sql=%s",
            operation, ctx, caller, sql_prefix,
        )
    else:
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


# A large finite client-side timeout used to represent "no DDL bound" without
# falling back to the pool's short query ``command_timeout`` (Bug-8041). Kept
# under the Windows ``PY_TIMEOUT_MAX`` (~49.7 days) so ``asyncio.wait_for`` /
# ``Future.result`` never raise OverflowError; only reachable via the explicit
# non-production unbounded opt-in below.
_DDL_NO_TIMEOUT = 30 * 24 * 3600  # 30 days — effectively unbounded, platform-safe
# PostgreSQL ``statement_timeout`` is an int GUC in milliseconds; a value above
# this ceiling raises "invalid value for parameter". Clamp so an operator who
# sets a huge SOURCE_DDL_TIMEOUT_SECONDS never breaks every DDL.
_PG_MAX_STATEMENT_TIMEOUT_MS = 2147483647  # ~24.8 days

# ---------------------------------------------------------------------------
# Bug-8041 Finding 5: DDL timeout resolution is now in a NEUTRAL shared config
# module (``shared.config.ddl_timeout``) that both ``source_pool.py`` and
# ``source_executor.py`` import normally — no dynamic try/except import
# fallback, no divergent-fallback failure mode.
#
# Backward-compatible re-exports keep existing callers and monkeypatch-based
# tests working at the same attribute names.
# ---------------------------------------------------------------------------
from shared.config.ddl_timeout import (  # noqa: E402, F401 - compatibility exports
    DDL_TIMEOUT_DEFAULT_SECONDS as _DDL_TIMEOUT_DEFAULT_SECONDS,
    DDL_TIMEOUT_MAX_SECONDS as _DDL_TIMEOUT_MAX_SECONDS,
    get_ddl_timeout as _get_ddl_timeout,
    get_effective_ddl_timeout_seconds,
    reset_ddl_config_warning_state as _reset_ddl_config_warning_state,
)


def _ddl_pg_timeouts() -> tuple[int, float]:
    """Return ``(server_statement_timeout_ms, client_timeout_s)`` for a PostgreSQL
    DDL / materialisation statement (Bug-8041).

    Server ``statement_timeout`` is the authoritative, server-confirmed cancel
    (0 = unlimited), clamped to PG's int-ms ceiling; the client asyncpg timeout is
    a slightly-larger safety net so the pool's short query ``command_timeout`` can
    never fire first. When the DDL bound is disabled (<=0) both are unbounded."""
    t = _get_ddl_timeout()
    if t <= 0:
        return 0, _DDL_NO_TIMEOUT
    server_ms = min(int(t * 1000), _PG_MAX_STATEMENT_TIMEOUT_MS)
    return server_ms, float(t) + 5.0


def _ddl_client_timeout() -> float:
    """DDL/materialisation deadline as a positive float suitable for
    ``asyncio.wait_for`` / driver ``timeout=`` (disabled maps to ``_DDL_NO_TIMEOUT``)."""
    t = _get_ddl_timeout()
    return float(t) if t > 0 else _DDL_NO_TIMEOUT


async def _run_ddl_bounded(fn: Any, label: str) -> Any:
    """Run a blocking materialisation callable on a worker thread bounded by the
    DDL deadline (Bug-8041). ``<=0`` disables the bound. On expiry the worker is
    abandoned (drivers with no side-channel cancel) but a clear QueryTimeoutError
    is raised so the job does not hang a scheduler thread indefinitely."""
    _ddl = _get_ddl_timeout()
    if _ddl <= 0:
        return await asyncio.to_thread(fn)
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn), timeout=_ddl + 10)
    except asyncio.TimeoutError as exc:
        raise QueryTimeoutError(
            f"{label} exceeded {_ddl}s timeout (SOURCE_DDL_TIMEOUT_SECONDS)."
        ) from exc


def _effective_tenant_slug(tenant_slug: str | None, tenant_session: Any) -> str | None:
    """Resolve the canonical tenant slug for audit + pool scope (Bug-8039/8041).

    Prefers an explicit ``tenant_slug`` and otherwise reads it from the
    tenant-bound session (``session.info['tenant_id']``). Threaded from the public
    entrypoints so the ``[SOURCE_AUDIT]`` record can attribute a touch to a tenant
    even when the caller (e.g. the query-router dispatcher) passes only a session,
    since ``ProjectConnection`` has no tenant column of its own."""
    if tenant_slug:
        return tenant_slug
    from shared.source_pool import session_tenant_id
    return session_tenant_id(tenant_session)


def _pool_scope(
    conn_obj: Any, *, tenant_slug: str | None = None, tenant_session: Any = None,
) -> str:
    """Fail-closed tenant/connection identity for the source pool key (Bug-8039).

    The pool must NEVER hand a physical connection across tenants. The canonical
    tenant boundary is the tenant slug — supplied explicitly (``tenant_slug``) or
    read from a tenant-bound session (``session.info['tenant_id']``). It is NOT
    the ``project_id``/connection ``id``: those are UUIDs in a per-tenant
    ``{slug}_meta`` schema and can legitimately collide across tenants (an
    imported/seeded project bundle preserves them). When no canonical tenant can
    be resolved, :func:`build_pool_scope` raises rather than sharing a pool.
    """
    from shared.source_pool import build_pool_scope, session_tenant_id
    tenant = tenant_slug or session_tenant_id(tenant_session)
    return build_pool_scope(
        tenant,
        project_id=getattr(conn_obj, "project_id", None),
        conn_id=getattr(conn_obj, "id", None),
    )


async def _resolve_source_db_endpoint_scoped(
    creds: dict[str, Any],
    config: dict[str, Any],
    *,
    tenant_session: Any = None,
    project_id: Any = None,
) -> tuple[str, int, str]:
    """Resolve execution/materialisation storage with system settings in scope.

    Bug-8482: ``source_db.fallback_*`` is persisted in the system database.
    Passing only a tenant session silently substituted registry defaults, so an
    artifact fingerprint and the executor could disagree about which operator-
    configured database they addressed.

    Resolver test doubles predating the ``system_session`` parameter remain
    callable; the production resolver always advertises and receives it.
    """
    from shared.db.session import SystemSessionLocal

    kwargs = {
        "tenant_session": tenant_session,
        "project_id": project_id,
    }
    async with SystemSessionLocal() as sys_db:
        if "system_session" in inspect.signature(
            resolve_source_db_endpoint
        ).parameters:
            kwargs["system_session"] = sys_db
        host, port, database = await resolve_source_db_endpoint(
            creds, config, **kwargs
        )
    # Bug-6216 R4: judge what will actually be dialled. Every postgresql /
    # redshift execution site resolves through here, so this is the one place
    # the invariant has to hold for all of them.
    _assert_execution_host_allowed(host, "postgresql")
    return host, port, database


async def _execute_pg(
    conn_obj: Any,
    sql: str,
    *,
    tenant_session: Any = None,
    tenant_slug: str | None = None,
) -> tuple[list[dict], list[str]]:
    from shared.source_pool import acquire_source_connection
    creds = _decrypt(conn_obj.encrypted_credentials)
    config = conn_obj.config or {}
    host, port, database = await _resolve_source_db_endpoint_scoped(
        creds, config,
        tenant_session=tenant_session,
        project_id=getattr(conn_obj, "project_id", None),
    )
    user = creds.get("user") or creds.get("username", "postgres")
    password = creds.get("password", "")
    timeout_s = _get_source_timeout()
    scope = _pool_scope(conn_obj, tenant_slug=tenant_slug, tenant_session=tenant_session)
    async with acquire_source_connection(scope, host, port, database, user, password) as conn:
        await conn.execute(f"SET statement_timeout = {timeout_s * 1000}")
        # Set search_path so unqualified table names resolve to the
        # configured schema (e.g. "demo_data") rather than just "public".
        # Bug-5663: search_path takes schema identifiers, not string
        # literals — use proper identifier quoting via connector_qualify.
        schema = config.get("schema") or config.get("dataset")
        if schema:
            from shared.connector_qualify import quote_identifier as _qi
            quoted_schema = _qi("postgresql", schema)
            await conn.execute(f"SET search_path TO {quoted_schema}, public")
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


# ---------------------------------------------------------------------------
# BigQuery remote-cancellation (F-014-07 / Bug-8041)
# ---------------------------------------------------------------------------

_BQ_POLL_INTERVAL_SECONDS = 0.5
# Cap for the exponential poll backoff so a long DDL deadline does not spam
# jobs.get calls (Bug-8041).
_BQ_POLL_MAX_INTERVAL_SECONDS = 10.0


def _bq_cancel_confirm_seconds() -> float:
    """Bounded wait to confirm a BigQuery job actually stopped after cancel."""
    try:
        return float(os.getenv("SOURCE_BQ_CANCEL_CONFIRM_SECONDS", "10"))
    except (TypeError, ValueError):
        return 10.0


def _cancel_bq_job(job: Any) -> bool:
    """Issue AND confirm remote cancellation of a BigQuery job (Bug-8041).

    ``job.cancel()`` only *requests* cancellation; the previous code ignored its
    outcome, so a timed-out query could keep running (and billing) remotely. This
    reloads the job until it reaches a terminal ``DONE`` state within a bounded
    window and returns whether cancellation was CONFIRMED. The outcome is logged
    so an unconfirmed cancel (a possible runaway remote statement) is visible.
    Runs synchronously — call it via ``asyncio.to_thread``.
    """
    job_id = getattr(job, "job_id", None)
    try:
        job.cancel()
    except Exception:
        logger.warning(
            "[SOURCE_CANCEL] bigquery job=%s cancel request failed", job_id,
            exc_info=True,
        )
        return False
    deadline = time.monotonic() + _bq_cancel_confirm_seconds()
    while time.monotonic() < deadline:
        try:
            job.reload()
        except Exception:
            logger.warning(
                "[SOURCE_CANCEL] bigquery job=%s reload failed; cancellation_unconfirmed",
                job_id, exc_info=True,
            )
            return False
        if getattr(job, "state", None) == "DONE":
            logger.info(
                "[SOURCE_CANCEL] bigquery job=%s cancellation confirmed", job_id,
            )
            return True
        time.sleep(0.25)
    logger.warning(
        "[SOURCE_CANCEL] bigquery job=%s cancellation_unconfirmed "
        "(job may still be running remotely)", job_id,
    )
    return False


def _build_bq_client(conn_obj: Any) -> Any:
    """Construct a BigQuery client from the connection's service-account creds."""
    from google.cloud import bigquery
    creds = _decrypt(conn_obj.encrypted_credentials)
    sa_info = creds.get("service_account_json", creds)
    if isinstance(sa_info, str):
        sa_info = json.loads(sa_info)
    gc = _bq_service_account_credentials(sa_info)
    config = conn_obj.config or {}
    project_id = (
        config.get("project_id")
        or creds.get("project_id")
        or sa_info.get("project_id")
    )
    return bigquery.Client(
        credentials=gc,
        project=project_id,
        location=config.get("location") or creds.get("location") or None,
    )


def _bq_job_config(config: dict, *, fail_closed: bool = True) -> Any:
    """Resolve the ``maximum_bytes_billed`` cap into a QueryJobConfig (or None).

    Raises (fail-closed) when a cost guard is enabled without a valid cap and
    ``fail_closed`` is True (query paths); DDL passes ``fail_closed=False``."""
    from google.cloud import bigquery
    max_bytes = _resolve_bq_max_bytes(config, fail_closed=fail_closed)
    if max_bytes is not None:
        return bigquery.QueryJobConfig(maximum_bytes_billed=max_bytes)
    return None


def _build_bq_client_and_job(conn_obj: Any, sql: str) -> tuple[Any, Any]:
    """Construct a BigQuery client and START the query job (non-blocking submit).

    ``client.query()`` submits the job and returns a handle immediately; the job
    then runs remotely. Keeping submission separate from result-waiting lets the
    async caller poll for completion and cancel + confirm on timeout instead of
    abandoning a worker thread while the remote job keeps running (Bug-8041).
    """
    config = conn_obj.config or {}
    # Bug-6528: resolve the maximum_bytes_billed cap BEFORE building the client
    # so a fail-closed cost-guard violation never constructs (or leaks) a client.
    job_config = _bq_job_config(config, fail_closed=True)
    client = _build_bq_client(conn_obj)
    try:
        # If job submission itself fails (bad SQL, auth, immediate API error) the
        # caller never receives the client and so cannot close it — close it here
        # so a submit-time failure does not leak the HTTP session.
        job = client.query(sql, job_config=job_config)
    except BaseException:
        client.close()
        raise
    return client, job


async def _bq_wait_or_cancel(job: Any, timeout_s: float, *, label: str) -> None:
    """Poll a started BigQuery job until DONE; on timeout, cancel AND confirm.

    Bug-8041: replaces the previous ``job.result(timeout)`` inside a worker
    thread wrapped by an outer ``asyncio.wait_for`` — which abandoned the thread
    and left the remote job running. Here the deadline is enforced in async code
    that owns the job handle, so a timeout deterministically issues a confirmed
    remote cancellation before raising.
    """
    deadline = time.monotonic() + timeout_s
    # Bug-8041: exponentially back the poll interval off so a long DDL /
    # materialisation deadline does not issue thousands of jobs.get calls. Scale
    # the cap to the deadline so an INTERACTIVE query (120s) stays responsive
    # (~1s cap) while a long DDL (3600s) backs off to the 10s ceiling.
    max_interval = min(
        _BQ_POLL_MAX_INTERVAL_SECONDS,
        max(_BQ_POLL_INTERVAL_SECONDS, timeout_s / 120.0),
    )
    interval = _BQ_POLL_INTERVAL_SECONDS
    while True:
        if await asyncio.to_thread(job.done):
            return
        if time.monotonic() >= deadline:
            # Bug-8041 race fix: the job may have completed between the last
            # done()-check and crossing the deadline. Re-check the terminal state
            # BEFORE cancelling so a query that finished on time returns its
            # result (via job.result() in the caller) instead of being discarded
            # as a timeout. Only a job that is still running is cancelled.
            if await asyncio.to_thread(job.done):
                return
            confirmed = await asyncio.to_thread(_cancel_bq_job, job)
            raise QueryTimeoutError(
                f"{label} exceeded {timeout_s}s timeout; remote_cancellation="
                f"{'confirmed' if confirmed else 'unconfirmed'}."
            )
        # Do not overshoot the deadline while backing off.
        remaining = deadline - time.monotonic()
        await asyncio.sleep(max(0.0, min(interval, remaining)))
        interval = min(interval * 2 if interval > 0 else 0, max_interval)


async def _execute_bq(conn_obj: Any, sql: str) -> tuple[list[dict], list[str]]:
    timeout_s = _get_source_timeout()
    client, job = await asyncio.to_thread(_build_bq_client_and_job, conn_obj, sql)

    def _materialize() -> tuple[list[dict], list[str]]:
        result = job.result()
        columns = [f.name for f in result.schema]
        rows = [dict(row) for row in result]
        return rows, columns

    try:
        await _bq_wait_or_cancel(job, timeout_s, label="BigQuery source query")
        return await asyncio.to_thread(_materialize)
    finally:
        await asyncio.to_thread(client.close)


def _spark_connect(conn_obj: Any) -> Any:
    """Open a PyHive Thrift connection. Separated so the timeout path can hold a
    reference to the live connection (and the tests can inject a fake)."""
    from pyhive import hive  # type: ignore
    creds = _decrypt(conn_obj.encrypted_credentials)
    # Bug-6216 R4: this defaulted to "localhost" exactly as the SQL Server DSN
    # builder did, so a Spark connection with no host dialled the service's own
    # container on a tenant-chosen port.
    return hive.connect(**_spark_connect_kwargs(creds))


async def _spark_run_with_teardown(
    conn_obj: Any, work: Any, timeout_s: float | None, label: str,
) -> Any:
    """Run a blocking PyHive ``work(cursor)`` under a timeout, tearing the Thrift
    session down from THIS coroutine on timeout (Bug-7160 / Bug-8041).

    PyHive's blocking driver exposes no server-side statement timeout, so the
    query runs in a worker thread. Previously the outer ``asyncio.wait_for``
    abandoned that thread on timeout and the connection was only closed in the
    worker's ``finally`` — which never runs while the worker stays blocked in
    ``execute``/``fetchall``, so the remote operation kept running until GC. Here
    the live connection is held in ``holder`` and closed from the coroutine on
    timeout, which tears down the Thrift session and forces the server to abandon
    the operation; a ``cancel`` flag makes a worker still inside connect abort
    before issuing any statement. PyHive has no side-channel cancel safe to call
    concurrently with a blocked execute, so cancellation is reported as
    *requested* (driven by session teardown), not confirmed.
    """
    holder: dict = {"cancel": False}

    def _run() -> Any:
        conn = _spark_connect(conn_obj)
        holder["conn"] = conn
        try:
            if holder.get("cancel"):
                raise QueryTimeoutError(
                    f"{label} exceeded {timeout_s}s timeout (cancelled during connect)."
                )
            return work(conn)
        finally:
            try:
                conn.close()
            finally:
                holder.pop("conn", None)

    try:
        return await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        # Mark cancelled first so a worker still inside _spark_connect aborts on
        # completion, then tear down any established connection from here.
        holder["cancel"] = True
        conn = holder.get("conn")
        torn_down = False
        if conn is not None:
            try:
                await asyncio.to_thread(conn.close)
                torn_down = True
                logger.info(
                    "[SOURCE_CANCEL] spark session teardown requested on timeout (%s)",
                    label,
                )
            except Exception:
                logger.warning(
                    "[SOURCE_CANCEL] spark session teardown failed (%s)", label,
                    exc_info=True,
                )
        else:
            logger.warning(
                "[SOURCE_CANCEL] spark timeout before connection established (%s); "
                "worker will abort on connect completion", label,
            )
        detail = (
            "Thrift session torn down" if torn_down
            else "aborts when the pending connection completes"
        )
        raise QueryTimeoutError(
            f"{label} exceeded {timeout_s}s timeout; "
            f"remote_cancellation=requested ({detail})."
        ) from exc


async def _execute_spark(conn_obj: Any, sql: str) -> tuple[list[dict], list[str]]:
    timeout_s = _get_source_timeout()

    def _work(conn: Any) -> tuple[list[dict], list[str]]:
        cursor = conn.cursor()
        cursor.execute(sql)
        columns = [desc[0] for desc in (cursor.description or [])]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        return rows, columns

    return await _spark_run_with_teardown(
        conn_obj, _work, timeout_s, "Spark/Hive source query",
    )


async def _execute_snowflake(conn_obj: Any, sql: str) -> tuple[list[dict], list[str]]:
    # Bug-8041 (documented driver guarantee): the Snowflake connector's
    # ``cursor.execute(sql, timeout=timeout_s)`` starts a watcher that issues a
    # server-side ``SYSTEM$CANCEL_QUERY`` when the timeout elapses, so the remote
    # query IS cancelled by the driver before the outer ``wait_for`` safety net
    # (timeout_s + 10) could fire. ``conn.close()`` in ``finally`` then tears down
    # the session. No coroutine-side cancel is needed for this connector.
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
            # Bug-8041: best-effort remote cancel, then tear down. pyodbc's
            # ``Cursor.cancel()`` is the one driver call documented as safe to
            # invoke while another thread runs the statement; when unavailable
            # through aioodbc, the ``finally: conn.close()`` below still aborts
            # the in-flight statement server-side (documented ODBC guarantee), so
            # the query does not keep running after the timeout is reported.
            _cancel = getattr(cursor, "cancel", None)
            if callable(_cancel):
                try:
                    res = _cancel()
                    if asyncio.iscoroutine(res):
                        await res
                    logger.info("[SOURCE_CANCEL] sqlserver statement cancel requested on timeout")
                except Exception:
                    logger.warning("[SOURCE_CANCEL] sqlserver cancel failed", exc_info=True)
            raise QueryTimeoutError(
                f"SQL Server source query exceeded {timeout_s}s timeout; "
                "remote_cancellation=requested (statement aborted on session teardown)."
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
    purpose: str = "background_read",
    tenant_slug: str | None = None,
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
    purpose:
        Caller-declared reason for the touch (F-014-04). Threaded into the
        ``[SOURCE_AUDIT]`` record so a sanctioned background access can be
        distinguished from a user query.

    Returns
    -------
    tuple[list[dict], list[str]]
        ``(rows, column_names)`` where each row is a dict keyed by column name.

    Raises
    ------
    ValueError
        If the connector type is not supported.
    """
    _guard_unconfigured(conn_obj)
    # F-014-04: structured start + completion (outcome) audit records so the
    # touch is attributable to a tenant/project/connection/purpose and the
    # result (ok / error) is recorded, not just the attempt.
    tenant_slug = _effective_tenant_slug(tenant_slug, tenant_session)
    context = _audit_context(conn_obj, purpose, tenant_slug=tenant_slug)
    _audit_log("execute_source_sql.start", sql, context=context)
    connector = normalize_connection_type(
        (conn_obj.connection_type or "").lower()
    )
    executor = _DISPATCH.get(connector)  # type: ignore[arg-type]
    if executor is None:
        raise ValueError(f"Unsupported connector type: {connector!r}")

    started = time.monotonic()
    try:
        if connector in ("postgresql", "redshift"):
            rows, columns = await executor(
                conn_obj, sql, tenant_session=tenant_session, tenant_slug=tenant_slug,
            )
        else:
            rows, columns = await executor(conn_obj, sql)
    except BaseException as exc:
        _audit_log(
            "execute_source_sql.end", sql,
            context={
                **context,
                "outcome": "error",
                "error": type(exc).__name__,
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
        )
        raise
    _audit_log(
        "execute_source_sql.end", sql,
        context={
            **context,
            "outcome": "ok",
            "rows": len(rows),
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return rows, columns


async def resolve_connector_type(conn_obj: Any) -> str:
    """Return the canonical connector type for a ``ProjectConnection``."""
    return normalize_connection_type(
        (conn_obj.connection_type or "").lower()
    ) or "unknown"


async def _execute_bq_routed(
    conn_obj: Any, sql: str, *, max_rows: int | None = None,
) -> tuple[list[dict], int, list[str]]:
    """BigQuery routed-query executor returning ``(rows, bytes, columns)``.

    Same connection/credential/cap machinery as ``_execute_bq`` but additionally
    surfaces ``total_bytes_processed`` so the routed user-query path can bill
    scanned bytes. Kept beside ``_execute_bq`` (which the background 2-tuple path
    uses) so the driver construction lives ONLY in this shared module.

    F-014-08: when *max_rows* is set, iteration stops after ``max_rows + 1``
    rows and raises ``SourceResultTooLargeError`` without exhausting the job.
    """
    timeout_s = _get_source_timeout()
    client, job = await asyncio.to_thread(_build_bq_client_and_job, conn_obj, sql)

    def _materialize() -> tuple[list[dict], int, list[str]]:
        result = job.result()
        columns = [f.name for f in result.schema]
        rows: list[dict] = []
        for row in result:
            rows.append(dict(row))
            if max_rows is not None and len(rows) > max_rows:
                raise SourceResultTooLargeError(
                    f"Result exceeds {max_rows} rows. "
                    "Add filters or raise the result.max_rows setting."
                )
        bytes_processed = job.total_bytes_processed or 0
        return rows, bytes_processed, columns

    try:
        # Bug-8041: async deadline that owns the job handle, so a timeout issues
        # a CONFIRMED remote cancellation instead of abandoning a worker thread.
        await _bq_wait_or_cancel(job, timeout_s, label="BigQuery source query")
        return await asyncio.to_thread(_materialize)
    finally:
        await asyncio.to_thread(client.close)


def _disambiguate_columns(
    rows: list[dict], columns: list[str], raw_records: list | None = None,
) -> tuple[list[dict], list[str]]:
    """Disambiguate duplicate column names so ``dict`` conversion keeps all values.

    Bug-AGG-001: ``SELECT MIN(x), MAX(x)`` yields two ``x`` columns; a plain
    ``dict(record)`` silently drops the first. When *raw_records* (asyncpg
    ``Record`` rows) is supplied, values are re-read positionally so no value is
    lost; otherwise the (already dict-collapsed) rows are returned with a
    de-duplicated column list. Returns ``(rows, columns)``.
    """
    seen: dict[str, int] = {}
    out_cols: list[str] = []
    has_dups = False
    for name in columns:
        count = seen.get(name, 0)
        if count > 0:
            out_cols.append(f"{name}_{count}")
            has_dups = True
        else:
            out_cols.append(name)
        seen[name] = count + 1
    if not has_dups:
        return rows, columns
    if raw_records is not None:
        rebuilt = [
            {out_cols[i]: rec[i] for i in range(len(out_cols))}
            for rec in raw_records
        ]
        return rebuilt, out_cols
    return rows, out_cols


async def _execute_pg_routed(
    conn_obj: Any, sql: str, *, tenant_session: Any = None,
    max_rows: int | None = None, tenant_slug: str | None = None,
) -> tuple[list[dict], int, list[str]]:
    """PostgreSQL/Redshift routed-query executor returning ``(rows, 0, columns)``.

    Enforces the row cap and duplicate-column disambiguation the routed
    user-query path requires, using the shared pooled connection. bytes=0 (no PG
    equivalent metric).
    """
    from shared.source_pool import acquire_source_connection
    creds = _decrypt(conn_obj.encrypted_credentials)
    config = conn_obj.config or {}
    host, port, database = await _resolve_source_db_endpoint_scoped(
        creds, config,
        tenant_session=tenant_session,
        project_id=getattr(conn_obj, "project_id", None),
    )
    user = creds.get("user") or creds.get("username", "postgres")
    password = creds.get("password", "")
    timeout_s = _get_source_timeout()
    scope = _pool_scope(conn_obj, tenant_slug=tenant_slug, tenant_session=tenant_session)
    async with acquire_source_connection(scope, host, port, database, user, password) as conn:
        await conn.execute(f"SET statement_timeout = {timeout_s * 1000}")
        schema = config.get("schema") or config.get("dataset")
        if schema:
            from shared.connector_qualify import quote_identifier as _qi
            quoted_schema = _qi("postgresql", schema)
            await conn.execute(f"SET search_path TO {quoted_schema}, public")
        try:
            if max_rows is None:
                records = await conn.fetch(sql)
            else:
                # F-014-08: stream and abort — do not materialise the full
                # result then count it.
                records = []
                async with conn.transaction():
                    async for rec in conn.cursor(sql):
                        records.append(rec)
                        if len(records) > max_rows:
                            raise SourceResultTooLargeError(
                                f"Result exceeds {max_rows} rows. "
                                "Add filters or raise the result.max_rows setting."
                            )
        except SourceResultTooLargeError:
            raise
        except Exception as exc:
            if "canceling statement due to statement timeout" in str(exc):
                raise QueryTimeoutError(
                    f"Source query exceeded {timeout_s}s statement timeout."
                ) from exc
            raise
        columns = list(records[0].keys()) if records else []
        rows = [dict(r) for r in records]
        rows, columns = _disambiguate_columns(rows, columns, raw_records=records)
        return rows, 0, columns


async def execute_routed_query(
    conn_obj: Any,
    sql: str,
    *,
    tenant_session: Any = None,
    tenant_slug: str | None = None,
    max_rows: int | None = None,
) -> tuple[list[dict], int, list[str]]:
    """Execute a ROUTED USER QUERY through the single shared execution gateway.

    F-014-02 / Bug-7984: this is the ONE public entry point the query-router uses
    for physical user-query I/O, so every safety, cancellation, credential, cost
    and audit control added here covers the primary user-query path — not just
    background/control-plane callers. It preserves the query-router
    ``(rows, bytes_processed, columns)`` contract, enforces the ``result.max_rows``
    cap inside the gateway (raising ``SourceResultTooLargeError``), and threads a
    structured ``purpose=user_query`` audit context.

    Driver construction lives ONLY in this shared module; the query-router
    dispatcher no longer imports any private helper or connector driver.
    """
    _guard_unconfigured(conn_obj)
    # Bug-8039/8041: attribute the primary user-query audit record to the tenant
    # even when the caller (query-router dispatcher) passes only a session.
    tenant_slug = _effective_tenant_slug(tenant_slug, tenant_session)
    context = _audit_context(conn_obj, "user_query", tenant_slug=tenant_slug)
    _audit_log("execute_routed_query.start", sql, context=context)
    connector = normalize_connection_type((conn_obj.connection_type or "").lower())

    started = time.monotonic()
    try:
        if connector in ("postgresql", "redshift"):
            rows, bytes_processed, columns = await _execute_pg_routed(
                conn_obj, sql, tenant_session=tenant_session, max_rows=max_rows,
                tenant_slug=tenant_slug,
            )
        elif connector == "bigquery":
            rows, bytes_processed, columns = await _execute_bq_routed(
                conn_obj, sql, max_rows=max_rows,
            )
        else:
            executor = _DISPATCH.get(connector)
            if executor is None:
                raise ValueError(f"Unsupported connector type: {connector!r}")
            rows, columns = await executor(conn_obj, sql)
            if max_rows is not None and len(rows) > max_rows:
                raise SourceResultTooLargeError(
                    f"Result exceeds {max_rows} rows. "
                    "Add filters or raise the result.max_rows setting."
                )
            bytes_processed = 0
    except BaseException as exc:
        _audit_log(
            "execute_routed_query.end", sql,
            context={
                **context, "outcome": "error", "error": type(exc).__name__,
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
        )
        raise
    _audit_log(
        "execute_routed_query.end", sql,
        context={
            **context, "outcome": "ok", "rows": len(rows),
            "bytes": bytes_processed,
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return rows, bytes_processed, columns


# ---------------------------------------------------------------------------
# DDL execution (CREATE TABLE, etc.)
# ---------------------------------------------------------------------------

async def _execute_ddl_pg(
    conn_obj: Any,
    statements: list[str],
    *,
    tenant_session: Any = None,
    tenant_slug: str | None = None,
) -> None:
    from shared.source_pool import acquire_source_connection
    creds = _decrypt(conn_obj.encrypted_credentials)
    config = conn_obj.config or {}
    host, port, database = await _resolve_source_db_endpoint_scoped(
        creds, config,
        tenant_session=tenant_session,
        project_id=getattr(conn_obj, "project_id", None),
    )
    user = creds.get("user") or creds.get("username", "postgres")
    password = creds.get("password", "")
    scope = _pool_scope(conn_obj, tenant_slug=tenant_slug, tenant_session=tenant_session)
    # Bug-8041: DDL/CTAS runs far longer than an interactive query, so it must NOT
    # be capped by the pool's query-scoped ``command_timeout``. Override the
    # server-side ``statement_timeout`` (0 = no limit) — the authoritative,
    # server-confirmed cancellation for PG — and pass a matching per-statement
    # client timeout so the pool's 120s default cannot fire first.
    ddl_timeout = _get_ddl_timeout()
    server_ms, client_timeout = _ddl_pg_timeouts()
    # Bug-8416: each independently-committable statement owns one checkout.
    # Pool security retirement is bounded by checkout age, so retaining one
    # checkout across a valid sequence let retirement kill a later statement
    # even though every statement stayed inside its own DDL timeout. Releasing
    # here also makes the implicit-autocommit contract explicit: callers that
    # need an atomic sequence must use open_source_connection(transactional=True).
    for stmt in statements:
        async with acquire_source_connection(scope, host, port, database, user, password) as conn:
            await conn.execute(f"SET statement_timeout = {server_ms}")
            try:
                await conn.execute(stmt, timeout=client_timeout)
            except Exception as exc:
                if "canceling statement due to statement timeout" in str(exc):
                    raise QueryTimeoutError(
                        f"Source DDL exceeded {ddl_timeout}s timeout "
                        "(SOURCE_DDL_TIMEOUT_SECONDS)."
                    ) from exc
                raise


async def _execute_ddl_bq(conn_obj: Any, statements: list[str]) -> None:
    # Bug-8041: each DDL statement gets a (DDL-scoped, not query-scoped) deadline
    # that owns the job handle and issues + confirms remote cancellation on
    # timeout, instead of a bare blocking job.result() that could run forever.
    # A large CTAS must not be cancelled by the interactive query timeout, so the
    # bound comes from _get_ddl_timeout(); <=0 disables it (unbounded). Bug-6528:
    # cap is fail_closed=False for DDL (bills 0 bytes; system operations must not
    # be blocked by a missing cap on a guarded connection).
    ddl_timeout = _get_ddl_timeout()
    config = conn_obj.config or {}
    job_config = _bq_job_config(config, fail_closed=False)
    client = await asyncio.to_thread(_build_bq_client, conn_obj)
    try:
        for stmt in statements:
            job = await asyncio.to_thread(
                lambda s=stmt: client.query(s, job_config=job_config)
            )
            if ddl_timeout > 0:
                await _bq_wait_or_cancel(job, ddl_timeout, label="BigQuery DDL")
            await asyncio.to_thread(job.result)  # surface statement errors
    finally:
        await asyncio.to_thread(client.close)


async def _execute_ddl_spark(conn_obj: Any, statements: list[str]) -> None:
    # Bug-8041: run under the shared Spark teardown so a DDL timeout tears down
    # the Thrift session from the coroutine instead of abandoning the worker.
    # DDL uses the larger DDL deadline (not the interactive query timeout).
    ddl_timeout = _get_ddl_timeout()

    def _work(conn: Any) -> None:
        cursor = conn.cursor()
        for stmt in statements:
            cursor.execute(stmt)
        cursor.close()

    await _spark_run_with_teardown(
        conn_obj, _work, ddl_timeout if ddl_timeout > 0 else None, "Spark/Hive DDL",
    )


async def _execute_ddl_snowflake(conn_obj: Any, statements: list[str]) -> None:
    # Bug-8041: bound the DDL by the DDL deadline (0 => unbounded). Pass the
    # driver ``timeout`` so Snowflake issues a server-side SYSTEM$CANCEL_QUERY on
    # expiry, and wrap the worker with a +10s outer wait_for so a wedged thread
    # cannot be abandoned indefinitely (thread-pool exhaustion).
    _ddl = _get_ddl_timeout()

    def _run() -> None:
        creds = _decrypt(conn_obj.encrypted_credentials)
        config = conn_obj.config or {}
        # Bug-7159: DDL can be multi-statement; keep_alive for session stability.
        conn = _open_snowflake(creds, config, keep_alive=True)
        try:
            cursor = conn.cursor()
            for stmt in statements:
                if _ddl > 0:
                    cursor.execute(stmt, timeout=_ddl)
                else:
                    cursor.execute(stmt)
            cursor.close()
        finally:
            conn.close()

    if _ddl > 0:
        try:
            await asyncio.wait_for(asyncio.to_thread(_run), timeout=_ddl + 10)
        except asyncio.TimeoutError as exc:
            raise QueryTimeoutError(
                f"Snowflake DDL exceeded {_ddl}s timeout (SOURCE_DDL_TIMEOUT_SECONDS)."
            ) from exc
    else:
        await asyncio.to_thread(_run)


async def _execute_ddl_sqlserver(conn_obj: Any, statements: list[str]) -> None:
    import aioodbc

    creds = _decrypt(conn_obj.encrypted_credentials)
    config = conn_obj.config or {}
    dsn = _build_sqlserver_dsn(creds, config)
    # Bug-8041: DDL uses the DDL deadline, not the interactive query timeout, so a
    # large SELECT ... INTO materialisation is not cancelled at the query cap.
    _ddl = _get_ddl_timeout()
    ddl_timeout = _ddl if _ddl > 0 else None
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


def _split_sql_statements(ddl: str) -> list[str]:
    """Split a multi-statement DDL string on semicolons, respecting quotes.

    Bug-5862: the naive ``ddl.split(";")`` breaks when a quoted identifier
    or string literal contains a semicolon, producing runnable fragments
    that reintroduce SQL injection. This simple state machine tracks
    single-quote, double-quote, and dollar-quote regions so semicolons
    inside quoted sections are preserved.
    """
    statements: list[str] = []
    current: list[str] = []
    i = 0
    length = len(ddl)
    while i < length:
        ch = ddl[i]

        # Single-quoted string literal: consume until unescaped closing '
        if ch == "'":
            current.append(ch)
            i += 1
            while i < length:
                if ddl[i] == "'" and i + 1 < length and ddl[i + 1] == "'":
                    current.append("''")
                    i += 2
                elif ddl[i] == "'":
                    current.append("'")
                    i += 1
                    break
                else:
                    current.append(ddl[i])
                    i += 1
            continue

        # Double-quoted identifier: consume until closing "
        if ch == '"':
            current.append(ch)
            i += 1
            while i < length:
                if ddl[i] == '"' and i + 1 < length and ddl[i + 1] == '"':
                    current.append('""')
                    i += 2
                elif ddl[i] == '"':
                    current.append('"')
                    i += 1
                    break
                else:
                    current.append(ddl[i])
                    i += 1
            continue

        # PostgreSQL dollar-quoted string: $$...$$ or $tag$...$tag$
        if ch == "$":
            tag_start = i
            i += 1
            while i < length and (ddl[i].isalnum() or ddl[i] == "_"):
                i += 1
            if i < length and ddl[i] == "$":
                tag = ddl[tag_start : i + 1]  # e.g. $$ or $tag$
                current.append(tag)
                i += 1
                # Find the closing tag
                close_pos = ddl.find(tag, i)
                if close_pos == -1:
                    # Unterminated dollar-quote: consume rest
                    current.append(ddl[i:])
                    i = length
                else:
                    current.append(ddl[i:close_pos])
                    current.append(tag)
                    i = close_pos + len(tag)
            else:
                # Not a dollar-quote; the $ is just a regular character
                current.append(ddl[tag_start:i])
            continue

        # Statement separator
        if ch == ";":
            stmt = "".join(current).strip()
            if stmt:
                statements.append(stmt)
            current = []
            i += 1
            continue

        current.append(ch)
        i += 1

    # Trailing statement (no final semicolon)
    last = "".join(current).strip()
    if last:
        statements.append(last)

    return statements


async def execute_source_ddl(
    conn_obj: Any,
    ddl: str | list[str],
    *,
    tenant_session: Any = None,
    purpose: str = "ddl",
    tenant_slug: str | None = None,
) -> None:
    """Execute DDL (CREATE TABLE, etc.) against the source database.

    *ddl* may be a single SQL string (which is split on statement
    boundaries using a quote-aware splitter) or a list of individual
    statements. Callers that already hold discrete statements should
    pass a list to avoid any splitting ambiguity.

    PostgreSQL uses implicit autocommit for DDL via asyncpg (each
    ``execute`` auto-commits outside a transaction block).

    *purpose* is threaded into the ``[SOURCE_AUDIT]`` record (F-014-04).
    """
    _guard_unconfigured(conn_obj)
    ddl_fingerprint = ddl if isinstance(ddl, str) else "; ".join(ddl[:2])
    tenant_slug = _effective_tenant_slug(tenant_slug, tenant_session)
    context = _audit_context(conn_obj, purpose, tenant_slug=tenant_slug)
    _audit_log("execute_source_ddl.start", ddl_fingerprint, context=context)
    connector = normalize_connection_type(
        (conn_obj.connection_type or "").lower()
    )
    executor = _DDL_DISPATCH.get(connector)
    if executor is None:
        raise ValueError(f"Unsupported connector type: {connector!r}")

    # Bug-5862: accept a list of pre-split statements directly; when given
    # a string, use the quote-aware splitter instead of naive split(";").
    if isinstance(ddl, list):
        statements = [s.strip() for s in ddl if s.strip()]
    else:
        statements = _split_sql_statements(ddl)
    if not statements:
        return

    started = time.monotonic()
    try:
        if connector in ("postgresql", "redshift"):
            await executor(
                conn_obj, statements, tenant_session=tenant_session,
                tenant_slug=tenant_slug,
            )
        else:
            await executor(conn_obj, statements)
    except BaseException as exc:
        _audit_log(
            "execute_source_ddl.end", ddl_fingerprint,
            context={
                **context,
                "outcome": "error",
                "error": type(exc).__name__,
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
        )
        raise
    _audit_log(
        "execute_source_ddl.end", ddl_fingerprint,
        context={
            **context,
            "outcome": "ok",
            "statements": len(statements),
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )


async def execute_source_sql_scalar(
    conn_obj: Any,
    sql: str,
    *,
    tenant_session: Any = None,
    tenant_slug: str | None = None,
) -> Any:
    """Execute *sql* and return the first column of the first row, or None."""
    rows, _ = await execute_source_sql(
        conn_obj, sql, tenant_session=tenant_session, tenant_slug=tenant_slug,
    )
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
    _guard_unconfigured(conn_obj)
    # Bug-5662: all three dialect branches previously interpolated
    # schema/table names into SQL without quoting or parameterization.
    # Fix: use connector_qualify for identifier quoting and proper
    # string-literal escaping for WHERE clause comparisons.
    from shared.connector_qualify import quote_identifier as _qi

    def _sql_string(val: str, *, escape_backslash: bool = False) -> str:
        """Escape a value for safe embedding in a SQL single-quoted literal.

        When *escape_backslash* is True (required for BigQuery, which
        interprets ``\\'`` as backslash-escaped quote in standard string
        literals), backslashes are doubled before single-quote doubling.
        """
        if escape_backslash:
            val = val.replace("\\", "\\\\")
        return val.replace("'", "''")

    connector = normalize_connection_type((conn_obj.connection_type or "").lower())
    try:
        if connector in ("postgresql", "redshift"):
            # regclass resolves a dotted schema.table text representation.
            # Build it from properly quoted identifiers, then escape the
            # whole thing for embedding inside the SQL string literal.
            # Bug-5861: Redshift interprets backslash as escape in string
            # literals (standard_conforming_strings is OFF by default), so
            # \' breaks out of the quoting. escape_backslash=True for
            # Redshift; harmless on PostgreSQL (standard_conforming_strings
            # ON by default treats \\ as two backslashes, not an escape).
            q_schema = _qi("postgresql", schema)
            q_table = _qi("postgresql", table)
            ref_text = _sql_string(
                f"{q_schema}.{q_table}",
                escape_backslash=(connector == "redshift"),
            )
            sql = f"SELECT pg_total_relation_size('{ref_text}'::regclass) AS b"
            result = await execute_source_sql_scalar(
                conn_obj, sql, tenant_session=tenant_session,
            )
            return int(result) if result is not None else None

        if connector == "snowflake":
            # Snowflake also interprets backslash escapes in string
            # literals (\' = literal quote) — same treatment as BigQuery.
            safe_schema = _sql_string(schema, escape_backslash=True)
            safe_table = _sql_string(table, escape_backslash=True)
            sql = (
                "SELECT SUM(active_bytes) AS b "
                "FROM information_schema.table_storage_metrics "
                f"WHERE table_schema = '{safe_schema}' AND table_name = '{safe_table}'"
            )
            result = await execute_source_sql_scalar(conn_obj, sql, tenant_session=tenant_session)
            return int(result) if result is not None else None

        if connector == "bigquery":
            project = bq_project or (conn_obj.config or {}).get("project_id", "")
            if not project:
                return None
            q_project = _qi("bigquery", project)
            q_schema = _qi("bigquery", schema)
            # BigQuery interprets backslash as escape in string literals,
            # so \' would break out of the quoting — escape_backslash=True.
            safe_table = _sql_string(table, escape_backslash=True)
            sql = (
                f"SELECT size_bytes AS b FROM {q_project}.{q_schema}.`__TABLES__` "
                f"WHERE table_id = '{safe_table}'"
            )
            result = await execute_source_sql_scalar(conn_obj, sql, tenant_session=tenant_session)
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
    tenant_slug: str | None = None,
) -> int:
    from shared.source_pool import acquire_source_connection
    creds = _decrypt(conn_obj.encrypted_credentials)
    config = conn_obj.config or {}
    host, port, database = await _resolve_source_db_endpoint_scoped(
        creds, config,
        tenant_session=tenant_session,
        project_id=getattr(conn_obj, "project_id", None),
    )
    user = creds.get("user") or creds.get("username", "postgres")
    password = creds.get("password", "")
    scope = _pool_scope(conn_obj, tenant_slug=tenant_slug, tenant_session=tenant_session)
    # Bug-8041: a bulk materialisation load can legitimately exceed the
    # interactive query timeout, so override the pool's query-scoped bound.
    ddl_timeout = _get_ddl_timeout()
    server_ms, client_timeout = _ddl_pg_timeouts()
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

    # Bug-8416: one checkout per executemany batch. A large logical load may
    # span many healthy batches; pool-age retirement must observe progress at
    # each release instead of terminating the checkout halfway through.
    for offset in range(0, len(rows), batch_size):
        batch = rows[offset:offset + batch_size]
        if type_by_col:
            records = [
                tuple(_coerce_for_pg(row.get(c), type_by_col.get(c)) for c in columns)
                for row in batch
            ]
        else:
            records = [tuple(row.get(c) for c in columns) for row in batch]
        async with acquire_source_connection(scope, host, port, database, user, password) as conn:
            await conn.execute(f"SET statement_timeout = {server_ms}")
            try:
                await conn.executemany(insert_sql, records, timeout=client_timeout)
            except Exception as exc:
                if isinstance(exc, asyncio.TimeoutError) or (
                    "canceling statement due to statement timeout" in str(exc)
                ):
                    raise QueryTimeoutError(
                        f"Bulk load exceeded {ddl_timeout}s timeout "
                        "(SOURCE_DDL_TIMEOUT_SECONDS)."
                    ) from exc
                raise
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
        creds = _decrypt(conn_obj.encrypted_credentials)
        sa_info = creds.get("service_account_json", creds)
        if isinstance(sa_info, str):
            sa_info = json.loads(sa_info)
        gc = _bq_service_account_credentials(sa_info)
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

    return await _run_ddl_bounded(_run, "BigQuery bulk load")


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
        kwargs = _spark_connect_kwargs(creds)
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

    return await _run_ddl_bounded(_run, "Spark/Hive bulk load")


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
        # Bug-7159: bulk insert is a batch operation; keep_alive prevents
        # session expiry during long-running multi-batch inserts.
        conn = _open_snowflake(creds, config, keep_alive=True)
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

    return await _run_ddl_bounded(_run, "Snowflake bulk load")


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
            _bt = _ddl_client_timeout()  # Bug-8041: bound each batch by the DDL deadline
            for i in range(0, len(rows), batch_size):
                batch = rows[i:i + batch_size]
                records = [tuple(row.get(c) for c in columns) for row in batch]
                try:
                    await asyncio.wait_for(
                        cursor.executemany(insert_sql, records), timeout=_bt
                    )
                except asyncio.TimeoutError as exc:
                    raise QueryTimeoutError(
                        f"SQL Server bulk load exceeded {_get_ddl_timeout()}s timeout "
                        "(SOURCE_DDL_TIMEOUT_SECONDS)."
                    ) from exc
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
    tenant_slug: str | None = None,
) -> int:
    """Insert rows into a target table in batches.

    Used by the cross-database aggregate materialization path.
    Returns total rows inserted.
    """
    if not rows:
        return 0

    _guard_unconfigured(conn_obj)
    tenant_slug = _effective_tenant_slug(tenant_slug, tenant_session)
    _audit_log(
        "bulk_insert_batched", f"INSERT INTO {schema}.{table} ({len(rows)} rows)",
        context=_audit_context(conn_obj, "aggregate_materialise", tenant_slug=tenant_slug),
    )
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
            tenant_slug=tenant_slug,
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
        # Bug-8040 hardening (opus5 F1): run the live/backup RENAME swap in ONE
        # transaction so PostgreSQL/Redshift roll it back atomically if the pooled
        # connection is force-terminated (or any statement fails) mid-swap — the
        # previous manual-rollback sequence left the live table renamed to backup
        # with no live table when the connection died between the two RENAMEs.
        await sc.execute(f"DROP TABLE IF EXISTS {backup_ref}")
        live_exists = await sc.fetch_one(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = $1 AND table_name = $2",
            schema, table,
        )
        async with sc.transaction():
            if live_exists:
                await sc.execute(
                    f"ALTER TABLE {live_ref} RENAME TO "
                    f"{quote_identifier_fn(connector, backup_table)}"
                )
            await sc.execute(
                f"ALTER TABLE {staging_ref} RENAME TO "
                f"{quote_identifier_fn(connector, table)}"
            )
        # Backup dropped AFTER the swap commits (best-effort; a leftover backup is
        # harmless and the next refresh drops it).
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


async def refresh_table_atomic_swap(
    conn_obj: Any,
    schema: str,
    table: str,
    *,
    tenant_session: Any = None,
    tenant_slug: str | None = None,
    target_project: str | None = None,
) -> None:
    """Atomically replace ``schema.table`` with its ``{table}__staging`` sibling.

    Build-then-switch (Bug-3732 / Bug-8768 B8768-R1-01): the caller has already
    materialised the fresh rows into ``{table}__staging`` while the LIVE table
    kept serving the old rows. This performs only the switch — rename live to a
    backup, staging into place, drop the backup — through the connector-specific
    :func:`_staging_swap`, which runs the PostgreSQL/Redshift rename inside ONE
    transaction so a mid-swap connection loss rolls back with the live table
    intact. On any failure the live table is preserved (never dropped before the
    replacement is in place). The staging table must already exist; this function
    does not create it and does not read the source.
    """
    _guard_unconfigured(conn_obj)
    from shared.connector_qualify import quote_identifier

    connector = normalize_connection_type((conn_obj.connection_type or "").lower())
    staging_table = f"{table}__staging"
    backup_table = f"{table}__backup"

    def _build_ref(tbl: str) -> str:
        base = f"{quote_identifier(connector, schema)}.{quote_identifier(connector, tbl)}"
        if target_project and connector == "bigquery":
            return f"{quote_identifier(connector, target_project)}.{base}"
        return base

    tenant_slug = _effective_tenant_slug(tenant_slug, tenant_session)
    _audit_log(
        "refresh_table_atomic_swap", f"swap staging->{schema}.{table}",
        context=_audit_context(conn_obj, "aggregate_materialise", tenant_slug=tenant_slug),
    )
    async with open_source_connection(
        conn_obj, purpose="aggregate_materialise",
        tenant_session=tenant_session, tenant_slug=tenant_slug,
        transactional=True,
    ) as sc:
        await _staging_swap(
            sc, connector, schema, table,
            staging_table, backup_table,
            _build_ref(staging_table), _build_ref(table), _build_ref(backup_table),
            quote_identifier,
        )


async def stream_to_staging_table(
    conn_obj: Any,
    schema: str,
    table: str,
    row_batches: AsyncIterator[list[dict]],
    batch_size: int = 20_000,
    *,
    col_defs: list[tuple[str, str]] | None = None,
    staging_statements: tuple[str, ...] | list[str] | None = None,
    infer_types_fn: Any = None,
    tenant_slug: str | None = None,
    tenant_session: Any = None,
    target_project: str | None = None,
) -> int:
    """Stream rows into a staging table and atomically swap with the live table.

    This keeps memory bounded (only one batch in memory at a time) and
    preserves the live table until the full load succeeds. On failure the
    staging table is cleaned up and the live table remains intact.

    Lifecycle callers may supply ``staging_statements`` rendered by the shared
    materialisation-plan boundary. Those exact DROP/CREATE statements are
    executed instead of constructing local DDL here. If ``col_defs`` is None,
    types are inferred from the first batch using
    ``infer_types_fn(batch, col_names)`` — the staging table is created
    after the first batch arrives.

    For BigQuery targets, pass ``target_project`` to use a project override
    different from the connection default.

    Returns total rows inserted.
    """
    _guard_unconfigured(conn_obj)

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

    tenant_slug = _effective_tenant_slug(tenant_slug, tenant_session)
    _audit_log(
        "stream_to_staging_table", f"staging={schema}.{staging_table}",
        context=_audit_context(conn_obj, "aggregate_materialise", tenant_slug=tenant_slug),
    )

    async def _create_staging(defs: list[tuple[str, str]]) -> None:
        col_def_sql = ", ".join(
            f"{quote_identifier(connector, c)} {t}" for c, t in defs
        )
        async with open_source_connection(
            conn_obj, purpose="aggregate_materialise",
            tenant_session=tenant_session, tenant_slug=tenant_slug,
        ) as sc:
            await sc.execute(f"DROP TABLE IF EXISTS {staging_ref}")
            await sc.execute(f"CREATE TABLE {staging_ref} ({col_def_sql})")

    async def _widen_staging_columns(
        widened: list[tuple[int, str, str]],
    ) -> None:
        """Bug-7006: ALTER staging columns that a later batch widened.

        When types are value-inferred (``col_defs is None``), the staging table
        is created from the FIRST batch. A later batch can carry a wider value
        than the first batch implied (a Decimal after an int, a value beyond
        int64, a mixed number/text column). The inference is monotonic —
        ``infer_types_fn`` widens against the prior resolved types — so any
        change here is strictly a widening. ALTER the physical column BEFORE the
        wider batch is inserted so it cannot truncate or overflow. Only reached
        on the inference path; ``col_defs`` (authoritative) callers never widen.
        """
        if not widened:
            return
        async with open_source_connection(
            conn_obj, purpose="aggregate_materialise",
            tenant_session=tenant_session, tenant_slug=tenant_slug,
        ) as sc:
            for _idx, cname, new_type in widened:
                q = quote_identifier(connector, cname)
                await sc.execute(
                    f"ALTER TABLE {staging_ref} ALTER COLUMN {q} "
                    f"TYPE {new_type} USING {q}::{new_type}"
                )

    if staging_statements is not None:
        if not col_defs:
            raise ValueError(
                "rendered staging statements require authoritative column definitions"
            )
        await execute_source_ddl(
            conn_obj, list(staging_statements), tenant_session=tenant_session,
            tenant_slug=tenant_slug,
        )
    elif col_defs is not None:
        await _create_staging(col_defs)

    total_rows = 0
    staging_created = col_defs is not None
    resolved_defs: list[tuple[str, str]] = col_defs or []
    # Bug-7006: value inference must widen monotonically across ALL batches, not
    # just the first. Only active when types are inferred (no authoritative
    # col_defs); the aggregate/scheduler callers pass col_defs and skip this.
    # Cross-batch re-widening additionally requires an inference fn that accepts
    # prior types (the pocket _infer_col_types 4-arg form); a 2-arg fn cannot
    # widen across batches, so it keeps its first-batch types. Detect arity once
    # here rather than catching TypeError per call (which could mask a real error
    # raised INSIDE the inference fn).
    _inferring = col_defs is None and infer_types_fn is not None
    _infer_supports_prior = False
    if _inferring:
        try:
            import inspect as _inspect
            _sig = _inspect.signature(infer_types_fn)
            _infer_supports_prior = len(_sig.parameters) >= 4 or any(
                p.kind == _inspect.Parameter.VAR_POSITIONAL
                or p.kind == _inspect.Parameter.VAR_KEYWORD
                for p in _sig.parameters.values()
            )
        except (TypeError, ValueError):
            _infer_supports_prior = False

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
            elif _inferring and _infer_supports_prior:
                # Re-infer this batch, widening against the types already
                # resolved from earlier batches. Any column whose type widened
                # is ALTERed on the physical staging table before insert so the
                # wider value lands without truncation/overflow (Bug-7006). The
                # inference fn accepts prior types (arity checked above), so
                # widening is strictly monotonic — a real error inside it is NOT
                # swallowed (it propagates and aborts the load, cleaning staging).
                col_names = [c for c, _t in resolved_defs]
                new_defs = infer_types_fn(batch, col_names, None, resolved_defs)
                new_by_col = {c: t for c, t in new_defs}
                widened: list[tuple[int, str, str]] = []
                for _i, (cname, old_type) in enumerate(resolved_defs):
                    nt = new_by_col.get(cname, old_type)
                    if nt != old_type:
                        widened.append((_i, cname, nt))
                if widened:
                    await _widen_staging_columns(widened)
                    resolved_defs = [
                        (c, new_by_col.get(c, t)) for c, t in resolved_defs
                    ]

            col_names_for_insert = [c for c, _t in resolved_defs]
            inserted = await bulk_insert_batched(
                conn_obj, schema, staging_table, col_names_for_insert, batch,
                batch_size=batch_size, tenant_session=tenant_session,
                column_types=resolved_defs,
                target_project=target_project,
                tenant_slug=tenant_slug,
            )
            total_rows += inserted

        if not staging_created:
            return 0

        async with open_source_connection(
            conn_obj, purpose="aggregate_materialise",
            tenant_session=tenant_session, tenant_slug=tenant_slug,
        ) as sc:
            count_row = await sc.fetch_one(
                f"SELECT COUNT(*) AS c FROM {staging_ref}"
            )
            staged_count = int(count_row["c"]) if count_row else 0
            if staged_count != total_rows:
                raise RuntimeError(
                    f"Staging validation failed: expected {total_rows} rows, "
                    f"got {staged_count} in {staging_ref}"
                )

        # Bug-8416: validation is one read checkout; only the rename sequence
        # is a genuine multi-statement transaction and therefore keeps one
        # checkout until commit/rollback.
        async with open_source_connection(
            conn_obj, purpose="aggregate_materialise",
            tenant_session=tenant_session, tenant_slug=tenant_slug,
            transactional=True,
        ) as sc:
            await _staging_swap(
                sc, connector, schema, table,
                staging_table, backup_table,
                staging_ref, live_ref, backup_ref,
                quote_identifier,
            )
    except Exception:
        if staging_created:
            async with open_source_connection(
                conn_obj, purpose="aggregate_materialise",
                tenant_session=tenant_session, tenant_slug=tenant_slug,
            ) as sc:
                await sc.execute(f"DROP TABLE IF EXISTS {staging_ref}")
        raise

    return total_rows


class SourceConnection:
    """Thin connector-agnostic wrapper over a raw source-DB connection.

    Returned by :func:`open_source_connection`. Callers use ``fetch``,
    ``fetch_one``, ``fetch_scalar``, and ``execute`` — never branch on
    connector type.
    """

    def __init__(
        self, impl: Any, connector: str, *,
        bq_job_config: Any = None,
        audit_ctx: dict[str, Any] | None = None,
    ):
        self._impl = impl
        self._connector = connector
        # Bug-6528: optional QueryJobConfig with maximum_bytes_billed for BQ.
        self._bq_job_config = bq_job_config
        # F-014-04 (FINDING-1): structured audit context threaded from
        # open_source_connection so per-operation logs carry tenant/project.
        self._audit_ctx = audit_ctx

    def transaction(self):
        """Return an atomic transaction context for PostgreSQL/Redshift.

        Bug-8040 hardening: a pooled connection can be FORCE-terminated mid-batch
        when its pool ages out (revoked-credential bound). Multi-statement DDL that
        must be all-or-nothing (e.g. the staging live/backup RENAME swap) runs
        inside this transaction so PostgreSQL/Redshift roll it back on an aborted
        connection instead of leaving the aggregate with no live table. Only the
        pooled asyncpg connectors support transactional DDL here."""
        if self._connector in ("postgresql", "redshift"):
            return self._impl.transaction()
        raise ValueError(
            f"transaction() is only supported for pooled PostgreSQL/Redshift "
            f"connections, not {self._connector!r}"
        )

    def _batch_timeout(self) -> float:
        """Deadline (seconds) for this batch connection's operations (Bug-8041).

        ``open_source_connection`` is the batch / materialisation entrypoint —
        its reads (``fetch``, ``fetch_batched``) and writes (``execute``: CTAS,
        ALTER, staging swap) are materialisation work that legitimately runs far
        longer than an interactive query, so they use the DDL bound, never the
        interactive query timeout. Always > 0 (disabled maps to ``_DDL_NO_TIMEOUT``)
        so it is safe to pass straight to ``asyncio.wait_for`` / asyncpg ``timeout=``.
        """
        return _ddl_client_timeout()

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        # Bug-7175: audit each operation individually, not just the open.
        _audit_log(f"SourceConnection.fetch[{self._connector}]", sql, context=self._audit_ctx)
        timeout_s = self._batch_timeout()
        if self._connector in ("postgresql", "redshift"):
            # Bug-8041: materialisation reads (e.g. staging validation counts) can
            # exceed the query timeout; the pool's server-side statement_timeout was
            # already raised to the DDL bound at open, and this client net matches.
            records = await self._impl.fetch(sql, *args, timeout=timeout_s)
            return [dict(r) for r in records]
        if self._connector == "bigquery":
            # Bug-8041: own the job handle so a timeout issues + confirms remote
            # cancellation instead of abandoning the worker thread (billing leak).
            job = await asyncio.to_thread(
                lambda: self._impl.query(sql, job_config=self._bq_job_config)
            )
            await _bq_wait_or_cancel(job, timeout_s, label="BigQuery fetch")
            return await asyncio.to_thread(lambda: [dict(r) for r in job.result()])
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
        # Bug-7175: audit each operation individually.
        _audit_log(f"SourceConnection.execute[{self._connector}]", sql, context=self._audit_ctx)
        # Bug-8041: execute carries DDL / staging writes (CREATE TABLE AS, ALTER,
        # staging swap) — materialisation work bounded by the DDL deadline on ALL
        # connectors, not the interactive query timeout.
        timeout_s = self._batch_timeout()
        if self._connector in ("postgresql", "redshift"):
            # Server-side statement_timeout was set to the DDL bound at open; this
            # client net matches. asyncpg cancels the statement server-side on
            # expiry; map the client timeout to a clear QueryTimeoutError.
            try:
                await self._impl.execute(sql, timeout=timeout_s)
            except Exception as exc:
                if isinstance(exc, asyncio.TimeoutError) or (
                    "canceling statement due to statement timeout" in str(exc)
                ):
                    raise QueryTimeoutError(
                        f"Source DDL exceeded {_get_ddl_timeout()}s timeout "
                        "(SOURCE_DDL_TIMEOUT_SECONDS)."
                    ) from exc
                raise
        elif self._connector == "bigquery":
            # Bug-8041: confirmed remote cancellation on timeout (see fetch()).
            job = await asyncio.to_thread(
                lambda: self._impl.query(sql, job_config=self._bq_job_config)
            )
            await _bq_wait_or_cancel(job, timeout_s, label="BigQuery execute")
            await asyncio.to_thread(job.result)
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
        # Bug-7175: audit the batched read start.
        _audit_log(f"SourceConnection.fetch_batched[{self._connector}]", sql, context=self._audit_ctx)
        if self._connector in ("postgresql", "redshift"):
            # Bug-8041: a materialisation SELECT (e.g. a GROUP BY aggregate feeding
            # a cross-DB build) can block the first fetch for the whole aggregation,
            # far beyond the interactive query timeout — bound it by the DDL
            # deadline, not the pool's query-scoped command_timeout.
            batch_timeout = self._batch_timeout()
            async with self._impl.transaction():
                cur = await self._impl.cursor(sql)
                while True:
                    batch = await cur.fetch(batch_size, timeout=batch_timeout)
                    if not batch:
                        break
                    yield [dict(r) for r in batch]
        elif self._connector == "bigquery":
            # Bug-8041: expose the running job so an early stop (timeout or
            # consumer break) can issue + confirm remote cancellation instead of
            # leaving the BQ job running and billing.
            job_holder: dict = {}

            async def _cancel_batch_job() -> None:
                job = job_holder.get("job")
                if job is not None:
                    await asyncio.to_thread(_cancel_bq_job, job)

            async for batch in self._threaded_batch_iter(
                lambda s, b: self._bq_fetch_batched_gen(s, b, job_holder),
                sql, batch_size, on_early_stop=_cancel_batch_job,
            ):
                yield batch
        elif self._connector in ("hadoop_spark", "snowflake"):
            async for batch in self._threaded_batch_iter(
                self._cursor_fetch_batched_gen, sql, batch_size
            ):
                yield batch
        elif self._connector == "sqlserver":
            # Bug-8041: bound the initial execute and each page by the DDL deadline
            # so a large materialisation read cannot hang unbounded.
            _bt = self._batch_timeout()
            await asyncio.wait_for(self._impl.execute(sql), timeout=_bt)
            if self._impl.description is None:
                return
            cols = [d[0] for d in self._impl.description]
            while True:
                rows = await asyncio.wait_for(self._impl.fetchmany(batch_size), timeout=_bt)
                if not rows:
                    break
                yield [dict(zip(cols, row)) for row in rows]
        else:
            raise ValueError(f"Unsupported connector: {self._connector!r}")

    async def _threaded_batch_iter(
        self, sync_gen_fn, sql: str, batch_size: int, *, on_early_stop=None,
    ) -> AsyncIterator[list[dict]]:
        """Bridge a synchronous batch generator to an async iterator via a queue.

        The sync generator runs in a thread and puts one batch at a time.
        The async side awaits each batch before the thread fetches the next,
        keeping at most one batch in memory at a time.

        ``on_early_stop`` (Bug-8041): an async callback invoked ONLY when the
        iteration stops before the producer signalled completion — a batch-read
        timeout or a consumer-side break/exception. Used to cancel + confirm a
        still-running remote job so it does not keep executing (and billing). It
        is NOT called on normal completion, so the happy path pays no cancel cost.
        """
        import threading

        # Bug-8041: batched materialisation reads use the DDL deadline (a large
        # aggregate's first batch can far exceed the interactive query timeout).
        timeout_s = self._batch_timeout()
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        sentinel = object()
        stop_event = threading.Event()

        def _put(item, put_timeout: float) -> None:
            # Bug-8041: the queue has maxsize=1, so this put blocks until the
            # CONSUMER takes the previous batch — and the consumer is a heavyweight
            # remote write (bulk load into the target). Bounding the handoff by a
            # fixed 60s cancelled a healthy large cross-DB build whose per-batch
            # write took longer; bound it by the materialisation deadline instead,
            # and CANCEL the abandoned future on expiry so it cannot resolve later.
            fut = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
            try:
                fut.result(timeout=put_timeout)
            except Exception:
                fut.cancel()
                raise

        def _producer():
            try:
                for batch in sync_gen_fn(sql, batch_size):
                    if stop_event.is_set():
                        break
                    _put(batch, timeout_s)
                if not stop_event.is_set():
                    _put(sentinel, timeout_s)
            except Exception as exc:
                if not stop_event.is_set():
                    try:
                        _put(exc, min(timeout_s, 10.0))
                    except Exception:
                        pass

        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, _producer)

        completed = False
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=timeout_s)
                except asyncio.TimeoutError:
                    raise QueryTimeoutError(
                        f"Batched source read timed out after {timeout_s}s"
                    )
                if item is sentinel:
                    completed = True
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            stop_event.set()
            # Bug-8041: only cancel the remote job when we stopped EARLY (timeout
            # or consumer break/exception) — not on normal completion.
            if not completed and on_early_stop is not None:
                try:
                    await on_early_stop()
                except Exception:
                    logger.warning(
                        "[SOURCE_CANCEL] batched-read early-stop cancel failed",
                        exc_info=True,
                    )
            try:
                await asyncio.wait_for(asyncio.wrap_future(future), timeout=5)
            except Exception:
                pass

    def _bq_fetch_batched_gen(self, sql: str, batch_size: int, job_holder=None):
        timeout_s = self._batch_timeout()  # Bug-8041: DDL/materialisation bound
        job = self._impl.query(sql, job_config=self._bq_job_config)
        if job_holder is not None:
            # Publish the job handle so the async side can cancel it on an
            # early stop (Bug-8041).
            job_holder["job"] = job
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
        # Bug-8041: route through _cursor_exec so Snowflake gets the driver
        # timeout (server-side SYSTEM$CANCEL_QUERY); Spark gets no kwarg.
        self._cursor_exec(sql)
        if self._impl.description is None:
            return
        cols = [d[0] for d in self._impl.description]
        while True:
            rows = self._impl.fetchmany(batch_size)
            if not rows:
                break
            yield [dict(zip(cols, row)) for row in rows]

    def _cursor_exec(self, sql: str) -> None:
        # Bug-8041: for Snowflake, pass the driver ``timeout`` so the connector
        # issues a server-side ``SYSTEM$CANCEL_QUERY`` on expiry (same guarantee
        # as ``_execute_snowflake``). PyHive (Spark) exposes no such argument and
        # relies on session teardown when the enclosing context exits.
        if self._connector == "snowflake":
            self._impl.execute(sql, timeout=int(self._batch_timeout()))
        else:
            self._impl.execute(sql)

    def _cursor_fetch(self, sql: str) -> list[dict]:
        self._cursor_exec(sql)
        if self._impl.description is None:
            return []
        cols = [d[0] for d in self._impl.description]
        return [dict(zip(cols, row)) for row in self._impl.fetchall()]

    def _cursor_execute(self, sql: str) -> None:
        self._cursor_exec(sql)

    async def _aioodbc_fetch(self, sql: str, *args: Any) -> list[dict]:
        if args:
            await self._impl.execute(sql, args)
        else:
            await self._impl.execute(sql)
        if self._impl.description is None:
            return []
        cols = [d[0] for d in self._impl.description]
        return [dict(zip(cols, row)) for row in await self._impl.fetchall()]


class _PooledSourceConnection:
    """Operation-scoped PostgreSQL/Redshift batch facade (Bug-8416).

    The public ``open_source_connection`` context remains connector-neutral,
    but pooled connectors release after each ``fetch``/``execute`` operation.
    A streaming ``fetch_batched`` retains one checkout because its cursor is one
    SQL statement. Atomic multi-statement code must opt into the explicit
    ``transactional=True`` context instead of relying on incidental persistence.
    """

    def __init__(
        self,
        connector: str,
        acquire_args: tuple[Any, ...],
        *,
        audit_ctx: dict[str, Any] | None = None,
    ) -> None:
        self._connector = connector
        self._acquire_args = acquire_args
        self._audit_ctx = audit_ctx

    @asynccontextmanager
    async def _operation(self):
        from shared.source_pool import acquire_source_connection

        async with acquire_source_connection(*self._acquire_args) as raw:
            server_ms, _ = _ddl_pg_timeouts()
            await raw.execute(f"SET statement_timeout = {server_ms}")
            yield SourceConnection(raw, self._connector, audit_ctx=self._audit_ctx)

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        async with self._operation() as operation:
            return await operation.fetch(sql, *args)

    async def fetch_one(self, sql: str, *args: Any) -> dict | None:
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def fetch_scalar(self, sql: str, *args: Any) -> Any:
        row = await self.fetch_one(sql, *args)
        return next(iter(row.values())) if row else None

    async def execute(self, sql: str) -> None:
        async with self._operation() as operation:
            await operation.execute(sql)

    async def fetch_batched(
        self, sql: str, batch_size: int = 20_000,
    ) -> AsyncIterator[list[dict]]:
        async with self._operation() as operation:
            async for batch in operation.fetch_batched(sql, batch_size=batch_size):
                yield batch

    def transaction(self):
        raise RuntimeError(
            "transaction() requires open_source_connection(..., "
            "transactional=True); operation-scoped connections cannot retain "
            "a checkout across statements"
        )


@asynccontextmanager
async def open_source_connection(
    conn_obj: Any,
    *,
    purpose: str,
    tenant_session: Any = None,
    tenant_slug: str | None = None,
    transactional: bool = False,
):
    """Open a connector-neutral source-DB facade for batch operations.

    *purpose* is required (F-014-11) so every checkout is attributable.

    Usage::

        async with open_source_connection(conn_obj, purpose="aggregate_materialise") as sc:
            rows = await sc.fetch("SELECT COUNT(*) AS c FROM ...")
            await sc.execute("CREATE TABLE ...")

    Credential decryption, connection construction, and cleanup are handled
    internally. PostgreSQL/Redshift acquire and release per operation by
    default. ``transactional=True`` is reserved for a genuinely atomic
    multi-statement sequence and keeps one pooled checkout through the context.
    Callers never branch on connector type.
    """
    if not str(purpose or "").strip():
        raise ValueError("open_source_connection requires a non-empty purpose=")
    _guard_unconfigured(conn_obj)
    tenant_slug = _effective_tenant_slug(tenant_slug, tenant_session)
    _ctx = _audit_context(conn_obj, purpose, tenant_slug=tenant_slug)
    _audit_log(
        "open_source_connection",
        "(transaction checkout)" if transactional else "(operation scoped)",
        context=_ctx,
    )
    connector = normalize_connection_type(
        (conn_obj.connection_type or "").lower()
    )
    if connector in ("postgresql", "redshift"):
        from shared.source_pool import acquire_source_connection
        creds = _decrypt(conn_obj.encrypted_credentials)
        config = conn_obj.config or {}
        host, port, database = await _resolve_source_db_endpoint_scoped(
            creds, config,
            tenant_session=tenant_session,
            project_id=getattr(conn_obj, "project_id", None),
        )
        user = creds.get("user") or creds.get("username", "postgres")
        password = creds.get("password", "")
        scope = _pool_scope(conn_obj, tenant_slug=tenant_slug, tenant_session=tenant_session)
        acquire_args = (scope, host, port, database, user, password)
        if not transactional:
            yield _PooledSourceConnection(
                connector, acquire_args, audit_ctx=_ctx,
            )
            return
        async with acquire_source_connection(*acquire_args) as raw:
            # Bug-8041: this is the batch / materialisation entrypoint, so raise the
            # server-side statement_timeout to the DDL bound for the whole session
            # (0 = unlimited). This governs long staging reads (fetch_batched) and
            # writes (CREATE TABLE AS, ALTER) even against a role whose default
            # statement_timeout is short; the pooled connection is reset on release.
            _server_ms, _ = _ddl_pg_timeouts()
            await raw.execute(f"SET statement_timeout = {_server_ms}")
            yield SourceConnection(raw, connector, audit_ctx=_ctx)

    elif connector == "bigquery":
        from google.cloud import bigquery
        creds = _decrypt(conn_obj.encrypted_credentials)
        sa_info = creds.get("service_account_json", creds)
        if isinstance(sa_info, str):
            sa_info = json.loads(sa_info)
        gc = _bq_service_account_credentials(sa_info)
        config = conn_obj.config or {}
        project_id = (
            config.get("project_id")
            or creds.get("project_id")
            or sa_info.get("project_id")
        )
        client = bigquery.Client(
            credentials=gc,
            project=project_id,
            location=config.get("location") or creds.get("location") or None,
        )
        # Bug-6528: resolve the byte cap and thread it into SourceConnection
        # so all BQ query paths (fetch, execute, batched) are capped.
        bq_job_config = None
        max_bytes = _resolve_bq_max_bytes(config)
        if max_bytes is not None:
            bq_job_config = bigquery.QueryJobConfig(
                maximum_bytes_billed=max_bytes,
            )
        try:
            yield SourceConnection(client, "bigquery", bq_job_config=bq_job_config, audit_ctx=_ctx)
        finally:
            client.close()

    elif connector == "hadoop_spark":
        from pyhive import hive  # type: ignore
        creds = _decrypt(conn_obj.encrypted_credentials)
        kwargs = _spark_connect_kwargs(creds)
        raw = hive.connect(**kwargs)
        try:
            yield SourceConnection(raw.cursor(), "hadoop_spark", audit_ctx=_ctx)
        finally:
            raw.close()

    elif connector == "snowflake":
        creds = _decrypt(conn_obj.encrypted_credentials)
        config = conn_obj.config or {}
        # Bug-7159: persistent connections are used for batch operations;
        # keep_alive prevents session expiry.
        raw = _open_snowflake(creds, config, keep_alive=True)
        try:
            yield SourceConnection(raw.cursor(), "snowflake", audit_ctx=_ctx)
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
                yield SourceConnection(cursor, "sqlserver", audit_ctx=_ctx)
            finally:
                await cursor.close()
        finally:
            await raw_conn.close()

    else:
        raise ValueError(f"Unsupported connector type: {connector!r}")
