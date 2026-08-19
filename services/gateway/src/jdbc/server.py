"""
PostgreSQL wire protocol TCP server.

Listens on JDBC_PORT (default 5433). JDBC clients (psycopg2, pgJDBC, DBeaver)
connect using:

  host=gateway, port=5433, database=<tenant_slug>, user=<email>, password=<password>

Optional startup parameter: model_id=<uuid> (passed via JDBC URL properties).

Authentication: the password field is treated as a plain password. The gateway
exchanges it for a JWT via model-service /auth/login using the email (user field)
and tenant_slug (database field). For backwards compatibility, if the password
looks like a JWT (starts with "ey"), it is validated directly without a login call.

Protocol subset supported:
  - SSLRequest → negotiate TLS when enabled, otherwise deny ('N')
  - StartupMessage → AuthOK + ParameterStatus + BackendKeyData + ReadyForQuery
  - Query ('Q') → RowDescription + DataRow* + CommandComplete + ReadyForQuery
  - Terminate ('X') → close connection

Unknown frontend messages → ErrorResponse + ReadyForQuery (no disconnect).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import ssl
import struct
import threading
import time
from decimal import Decimal, InvalidOperation
from typing import Optional

import sqlglot
from sqlglot import exp

from shared.config.bootstrap import (
    resolve_system_env_default_int,
    system_snapshot_get,
)
from shared.config.settings import get_settings
from src.auth.base import validate_session_upstream, verify_jwt_token
from src.jdbc.catalogue import CatalogueDB, CatalogueQueryError
from src.jdbc import protocol as proto
from src.jdbc.throttle import get_governor
from src.router_client import (
    GatewayQueryRateLimitExceeded,
    QueryByteCeilingExceeded,
    QueryRouterError,
    execute_query,
    fetch_model_metadata,
    login_for_token,
)

logger = logging.getLogger(__name__)
settings = get_settings()


class SessionRevokedError(Exception):
    """Raised mid-connection when an already-open JDBC session has been revoked.

    G2 / grok F-001-01: a long-lived pooled JDBC connection reuses the JWT it
    authenticated with for every query. If the user is later deactivated,
    demoted, or their token_version is bumped, the connection must stop serving
    queries. The query paths write a SQLSTATE 28000 ErrorResponse and then raise
    this so ``handle_client`` closes the socket (fail-closed + connection close),
    mirroring how XMLA re-validates inside each SOAP request handler.
    """


_ssl_context: ssl.SSLContext | None = None

# Connection counter — used as fake PID for BackendKeyData
_conn_counter = 0
_conn_counter_lock = threading.Lock()  # M-06 fix: protect concurrent increment

# Bug-5188: registry of in-flight query tasks keyed by (pid, cancel_secret).
# CancelRequest on a fresh socket looks up the target connection's task and
# cancels it. Protected by _conn_counter_lock (reuse; always short-held).
_inflight_tasks: dict[tuple[int, int], asyncio.Task] = {}

_PARAM_PLACEHOLDER_RE = re.compile(r"\$(\d+)")

# Bug-8108: portal row-buffer cap. ``_execute_for_extended`` materialises the
# full result before Describe/Execute can page it out — PortalSuspended bounds
# how many rows go on the wire per Execute call, not how many the gateway
# buffers in memory to serve those calls. Cap the buffer and fail closed with
# SQLSTATE 54000 (program_limit_exceeded) rather than accept unbounded memory
# growth. The cap is a CONFIG value, never a hard-coded literal: same
# three-tier resolution (hot-reloadable system setting, then env var, then
# this constant) as ``router_client._query_byte_ceiling``.
_DEFAULT_PORTAL_ROW_BUFFER_CAP = 50_000


def _portal_row_buffer_cap() -> int:
    """Resolve the JDBC portal row-buffer cap (Bug-8108).

    Three-tier resolution: an explicitly-stored hot-reloadable
    ``system_settings`` value first, then the
    ``GATEWAY_JDBC_PORTAL_ROW_BUFFER_CAP`` env/Settings field, then the
    registry default (50,000).

    Bug-8108 F3: the previous implementation read ``system_snapshot_get``,
    which folds the registry default in for an ABSENT stored value — so the
    function always returned at the first tier and the advertised env var was
    unreachable. ``resolve_system_env_default_int`` consults the EXPLICIT
    stored value, so an unset system row falls through to the env tier, and it
    rejects a negative env value instead of treating it as "disabled".
    """
    return resolve_system_env_default_int(
        "gateway.jdbc_portal_row_buffer_cap",
        settings.GATEWAY_JDBC_PORTAL_ROW_BUFFER_CAP,
        _DEFAULT_PORTAL_ROW_BUFFER_CAP,
    )


_UNREACHABLE_MEASURE_RE = re.compile(
    r"^(.+?)\s+(?:depends on fields that are not reachable|has no compatible dimensions)"
    r"|^There is no aggregation path between\s+(.+?)\s+and\s+"
    r"|^Cannot resolve hierarchy path\.",
)

# Sentinel for "could not interpret this node" in the $KPIs shaper (F-001-09).
_UNSUPPORTED = object()
_LOOKER_WINDOW_UNSUPPORTED = (
    "Looker symmetric aggregates not yet supported by Tessallite — use "
    "Looker's sql_distinct_key directive or simplify the explore"
)
_LOOKER_COMPLEX_MULTI_RELATION_UNSUPPORTED = (
    "Looker complex SQL over multiple semantic relations is not yet supported "
    "by Tessallite — use a single generated view or simplify the explore"
)
_LOOKER_CLIENT_KINDS = frozenset({"looker_cloud"})
_LOOKER_DISABLED = "Looker gateway support is disabled on this deployment."


def _client_kind_from_application_name(application_name: str | None) -> str | None:
    """Map declared PostgreSQL client application names to telemetry labels."""
    normalized = (application_name or "").strip().lower()
    if "looker studio" in normalized or "data studio" in normalized:
        return "looker_studio"
    if "looker" in normalized:
        return "looker_cloud"
    return None


def _client_kind_from_sql(
    sql: str,
    generated_relations: set[str] | None = None,
) -> str | None:
    """Detect only SQL signatures owned by this integration.

    Table-scoped ``<model>__<table>`` relations are emitted exclusively for
    LookML projects, so this is a stable Cloud Core signal. Studio probe
    signatures remain unset until live capture supplies evidence.
    """
    candidates = {
        name.lower()
        for name in re.findall(
            r'\b(?:from|join)\s+(?:(?:"?\w+"?)\.)?"?([\w]+__[\w]+)"?',
            sql,
            re.IGNORECASE,
        )
    }
    if generated_relations is not None:
        candidates.intersection_update({name.lower() for name in generated_relations})
    return "looker_cloud" if candidates else None


def _count_param_placeholders(sql: str) -> int:
    """Return the number of distinct ``$N`` parameter placeholders in *sql*."""
    matches = _PARAM_PLACEHOLDER_RE.findall(sql)
    return max(int(m) for m in matches) if matches else 0


def _router_error_sqlstate(exc: QueryRouterError) -> str:
    """SQLSTATE for a query-router error surfaced on the JDBC wire.

    Wave C #5: a 403 from the query-router execute path is an ACCESS DENIAL
    (persona / CLS / RLS), including a query that references a CLS-blocked column.
    Map it to SQLSTATE 42501 (insufficient_privilege) so the uniform CLS contract
    holds across surfaces (REST 403 / JDBC 42501 / XMLA access-denied) and a
    blocked-column query is refused wholesale, never partially served. An explicit
    SQLSTATE from the router still wins; otherwise fall back to 42601 as before.
    """
    if exc.sqlstate:
        return exc.sqlstate
    if getattr(exc, "status_code", None) == 403:
        return "42501"
    return "42601"


def _strip_one_terminator(sql: str) -> str:
    """Strip a single trailing ``;`` statement terminator (Bug-6592 F4).

    Drivers such as psql append ``;`` to housekeeping commands. The
    transaction-command classifiers match on exact strings (``COMMIT``,
    ``ROLLBACK``, ``BEGIN``); without normalising the terminator a ``COMMIT;``
    would not match and transaction-scoped ``SET LOCAL`` state would leak past
    the boundary. Exactly one terminator is removed (an interior ``;`` in a
    literal is never touched — only a lone trailing one).
    """
    stripped = sql.strip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
    return stripped


def _is_side_effecting_housekeeping(sql: str) -> bool:
    """True for driver-housekeeping commands whose ``_execute_for_extended``
    handling MUTATES connection state (Bug-6592 F4).

    ``SET``/``SET LOCAL`` capture a session var, ``COMMIT``/``ROLLBACK`` clear
    LOCAL scope and close the explicit transaction, ``DISCARD``/``RESET`` clear
    session state, ``BEGIN`` opens the explicit transaction. A portal Describe
    must be metadata-only, so it must NOT run these through the side-effecting
    execution helper — it returns NoData for them and lets the later Execute
    perform the actual command. ``SHOW`` and real SELECTs are side-effect free
    and are deliberately excluded.
    """
    u = _strip_one_terminator(sql).upper()
    return (
        u.startswith("SET ")
        or u in ("BEGIN", "COMMIT", "ROLLBACK")
        or u.startswith("SAVEPOINT ")
        or u.startswith("DEALLOCATE")
        or u.startswith("DISCARD")
        or u.startswith("RESET ")
        or u == "RESET"
    )


# ---------------------------------------------------------------------------
# sqlglot-based SQL helpers (primary path; regex is the fallback)
# ---------------------------------------------------------------------------

_TABLE_NAME_REGEX = re.compile(
    r'(?:from|join)\s+'
    r'(?:(?:"[^"]+"|[\w]+)\.'
    r'(?:"([^"]+)"|(\w+))'
    r'|"([^"]+)"'
    r'|(\w+))',
    re.IGNORECASE,
)


def _parse_database_param(raw_db: str) -> tuple[str, str | None, str | None] | None:
    """Parse the startup ``database`` param (Bug-5878).

    Accepted forms: ``<tenant>``, ``<tenant>/<model>``,
    ``<tenant>/<project>/<model>``. Returns ``(tenant_slug, project_hint,
    model_hint)``, or ``None`` when the value is malformed (empty segment or
    more than three parts) so the caller can reject it with a clear message.
    """
    if "/" not in raw_db:
        return raw_db, None, None
    parts = raw_db.split("/")
    if len(parts) == 2 and all(parts):
        return parts[0], None, parts[1]
    if len(parts) == 3 and all(parts):
        return parts[0], parts[1], parts[2]
    return None


def _normalize_bi_sql(sql: str) -> str:
    """Normalize BI-tool-generated SQL for the query-router.

    The query-router does not accept table aliases, table-qualified columns,
    or column aliases. BI tools (Power BI, Tableau, Looker) generate SQL with
    all of these. This function strips them so the query-router sees plain::

        SELECT col1, col2 FROM "schema"."table"

    Bug-6918: the normalizer only fires for single-relation SELECTs (no
    joins, no subqueries, no CTEs). For multi-table queries, qualifiers are
    necessary to disambiguate columns and are preserved by the early bail-out
    guard (Bug-5881). Within the single-table scope, all qualifiers are
    safely redundant and are stripped to match the router's unqualified-column
    binding model. The original SQL is preserved as ``original_sql`` at the
    call site so audit logging (Bug-6921) records the pre-normalization form.
    """
    try:
        parsed = sqlglot.parse(sql, read="postgres")
        if not parsed or parsed[0] is None:
            return sql
        stmt = parsed[0]
        if not isinstance(stmt, exp.Select):
            return sql
        # Bug-5881: stripping aliases and qualifiers is only safe for the
        # single-relation SELECT shape BI tools emit. With CTEs, subqueries,
        # or joins, removing them makes columns ambiguous ("st.source_system"
        # -> "source_system") or duplicates relation names (CTE self-joins).
        if any(s is not stmt for s in stmt.find_all(exp.Select)):
            return sql
        if len(list(stmt.find_all(exp.Table))) != 1:
            return sql
        changed = False
        for table in stmt.find_all(exp.Table):
            if table.args.get("alias"):
                table.set("alias", None)
                changed = True
        for column in stmt.find_all(exp.Column):
            if column.table:
                column.set("table", None)
                changed = True
        for col_expr in stmt.expressions:
            if isinstance(col_expr, exp.Alias) and isinstance(
                col_expr.this, exp.Column
            ):
                col_expr.replace(col_expr.this)
                changed = True
        if not changed:
            return sql
        return stmt.sql(dialect="postgres")
    except Exception:
        return sql


def _projection_alias_map(sql: str) -> dict[str, str]:
    """Map direct projected column names to user-visible aliases."""
    try:
        parsed = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        return {}
    if not isinstance(parsed, exp.Select):
        return {}
    aliases: dict[str, str] = {}
    for proj in parsed.expressions:
        if not isinstance(proj, exp.Alias) or not isinstance(proj.this, exp.Column):
            continue
        source = proj.this.name
        alias = proj.alias
        if source and alias and source != alias:
            aliases[source.lower()] = alias
    return aliases


def _apply_projection_aliases(
    sql: str, columns_meta: list, rows_data: list
) -> tuple[list, list]:
    """Restore direct column aliases in JDBC output metadata and dict rows."""
    aliases = _projection_alias_map(sql)
    if not aliases:
        return columns_meta, rows_data

    source_to_alias: dict[str, str] = {}
    aliased_columns: list = []
    for col in columns_meta:
        if isinstance(col, dict):
            name = col.get("name")
            alias = aliases.get(str(name).lower()) if name is not None else None
            if alias:
                updated = dict(col)
                updated["name"] = alias
                aliased_columns.append(updated)
                source_to_alias[str(name)] = alias
            else:
                aliased_columns.append(col)
            continue
        name = str(col)
        alias = aliases.get(name.lower())
        if alias:
            aliased_columns.append(alias)
            source_to_alias[name] = alias
        else:
            aliased_columns.append(col)

    if not source_to_alias:
        return columns_meta, rows_data

    aliased_rows: list = []
    for row in rows_data:
        if not isinstance(row, dict):
            aliased_rows.append(row)
            continue
        updated = dict(row)
        for source, alias in source_to_alias.items():
            if source in updated:
                updated[alias] = updated.pop(source)
        aliased_rows.append(updated)
    return aliased_columns, aliased_rows


def _is_constant_select_sqlglot(sql: str) -> bool:
    """Check via sqlglot AST whether the query has zero Table nodes (constant SELECT)."""
    try:
        parsed = sqlglot.parse(sql, read="postgres")
        if not parsed or parsed[0] is None:
            return False
        return (
            isinstance(parsed[0], exp.Select)
            and not list(parsed[0].find_all(exp.Table))
        )
    except Exception:
        return False


def _classify_ungrouped_query(
    sql: str, table_columns: dict[str, list[dict]] | None = None,
) -> str | None:
    """Classify an ungrouped SELECT for raw-route eligibility.

    Returns:
      ``"raw"``    — no GROUP BY, SELECT list is plain columns (or ``*``) with
                     at least one non-measure column: a detail-row query the
                     raw route can render.
      ``"source"`` — no GROUP BY and every SELECT item is an aggregate
                     function or a flat measure column.
      ``None``     — anything else (GROUP BY, subqueries, window functions,
                     DISTINCT, expressions, mixed column+aggregate, unparsable):
                     use the normal flow.

    Bug-5879: the raw builder (``raw_sql.py``) renders only bound semantic
    fields — it cannot reproduce window functions, subqueries, DISTINCT, or
    scalar expressions, and a mixed plain-column + explicit-aggregate SELECT
    needs GROUP BY injection, not per-row output. All such shapes must fall
    through to the normal flow that served them before the raw route existed.
    """
    try:
        parsed = sqlglot.parse(sql, read="postgres")
        if not parsed or not isinstance(parsed[0], exp.Select):
            return None
        stmt = parsed[0]
        if stmt.find(exp.Group):
            return None

        tables = list(stmt.find_all(exp.Table))
        if not tables:
            return None

        if any(s is not stmt for s in stmt.find_all(exp.Select)):
            return None  # subquery / CTE / derived table anywhere
        if stmt.find(exp.Window):
            return None
        if stmt.args.get("distinct"):
            return None

        agg_types = (exp.Sum, exp.Avg, exp.Count, exp.Min, exp.Max)
        exprs = stmt.expressions
        if not exprs:
            return None

        if isinstance(exprs[0], exp.Star):
            return "raw"

        table_name = tables[0].name
        measure_names: set[str] = set()
        if table_columns:
            cols_meta = (
                table_columns.get(table_name)
                or table_columns.get(table_name.lower())
            )
            if cols_meta:
                measure_names = {
                    c["name"].lower() for c in cols_meta
                    if c.get("kind") == "measure"
                }

        saw_dim_column = False
        saw_measure_column = False
        saw_aggregate_fn = False
        for col_expr in exprs:
            inner = col_expr.this if isinstance(col_expr, exp.Alias) else col_expr
            if isinstance(inner, agg_types):
                saw_aggregate_fn = True
                continue
            if isinstance(inner, exp.Anonymous):
                fn_name = (inner.this or "").upper()
                if fn_name in ("SUM", "AVG", "COUNT", "MIN", "MAX"):
                    saw_aggregate_fn = True
                    continue
                return None
            if isinstance(inner, exp.Column):
                if inner.name.lower() in measure_names:
                    saw_measure_column = True
                else:
                    saw_dim_column = True
                continue
            return None  # CASE, COALESCE, arithmetic, literals, casts, ...

        if saw_aggregate_fn:
            # Pure explicit aggregation → source; aggregate mixed with plain
            # columns needs GROUP BY injection → normal flow.
            if saw_dim_column or saw_measure_column:
                return None
            return "source"
        if saw_dim_column:
            return "raw"
        return "source"  # only flat measure columns
    except Exception:
        return None


class PGWireServer:
    """
    Asyncio-based PostgreSQL wire protocol handler.

    One instance per accepted TCP connection.
    """

    def __init__(self) -> None:
        global _conn_counter
        # M-06 fix: atomic increment with lock to prevent duplicate PIDs
        with _conn_counter_lock:
            _conn_counter += 1
            self._pid = _conn_counter
        self._tenant_slug: str = ""
        self._model_id: Optional[str] = None
        self._jwt_token: str = ""
        self._model_names: list[str] = []
        self._table_columns: dict[str, list[dict]] = {}
        self._table_model_id: dict[str, str] = {}  # table_name → model_id
        self._table_descriptions: dict[str, str] = {}  # table_name → model.description
        self._table_trust_meta: dict[str, dict] = {}  # table_name → freshness/source/owner
        self._table_persona_id: dict[str, Optional[str]] = {}  # table_name → persona_id | None
        self._table_include_hidden: dict[str, bool] = {}  # table_name → includes_hidden_columns
        self._table_query_name: dict[str, str] = {}  # exposed relation → canonical model slug
        self._table_foreign_keys: dict[str, list[dict[str, str]]] = {}
        self._table_row_estimates: dict[str, int | float | None] = {}
        self._looker_relations: set[str] = set()
        self._table_project_slug: dict[str, str] = {}  # table_name → project_slug
        self._statements: dict[str, str] = {}  # statement_name → raw SQL (with $N placeholders)
        self._statement_param_oids: dict[str, list[int]] = {}  # statement_name → declared param OIDs
        self._session_vars: dict[str, str] = {}  # app.<name> → value (for parameterized filters)
        # Bug-6592: SET LOCAL app.* is transaction-scoped and must NOT share
        # _session_vars' connection lifetime. Captured here separately and
        # cleared at transaction end (COMMIT/ROLLBACK) and on the full-reset
        # commands (DISCARD/RESET), never on BEGIN/SAVEPOINT.
        self._session_vars_local: dict[str, str] = {}  # app.<name> → value (SET LOCAL only)
        # Bug-6592 F4: is an EXPLICIT transaction block open (client issued
        # BEGIN and has not yet COMMIT/ROLLBACK)? SET LOCAL scope ends at the
        # transaction boundary: an implicit transaction ends at each Sync, an
        # explicit one spans Syncs until COMMIT/ROLLBACK. This flag is what
        # tells the Sync handler which case it is in.
        self._in_explicit_txn: bool = False
        self._catalogue: CatalogueDB | None = None
        self._catalogue_built_at: float = 0.0  # monotonic timestamp of last catalogue build
        self._tls_active = False
        self._peer_ip: str = "unknown"  # F-001-07: per-IP governance key
        self._client_kind: str | None = None
        # F-001-12: a real per-connection cancel key so the advertised
        # BackendKeyData is not a misleading all-zero secret.
        self._cancel_secret = int.from_bytes(os.urandom(4), "big")

    async def handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        # F-001-07: peer IP drives the per-IP connection cap / auth throttle.
        self._peer_ip = peer[0] if isinstance(peer, (tuple, list)) and peer else "unknown"
        logger.info("New JDBC connection from %s (pid=%d)", peer, self._pid)

        # F-001-07: admit the connection through the per-IP governor before
        # any work. A refusal (concurrency cap or active auth-failure throttle)
        # is reported with a FATAL ErrorResponse and the socket is closed —
        # fail-closed for a publicly exposed pre-auth surface.
        governor = get_governor()
        admitted, deny_reason = governor.try_acquire(self._peer_ip)
        if not admitted:
            logger.warning(
                "JDBC connection refused from %s (pid=%d): %s",
                peer, self._pid, deny_reason,
            )
            try:
                writer.write(
                    proto.error_response(
                        "Connection refused.", severity="FATAL", code="53300"
                    )
                )
                await writer.drain()
            except Exception:
                pass
            writer.close()
            return

        try:
            await self._run(reader, writer)
        except asyncio.IncompleteReadError:
            pass  # client disconnected cleanly
        except SessionRevokedError as exc:
            # G2 / grok F-001-01: the query path already reported 28000 to the
            # client; the raise reaches here so the finally closes the socket.
            # This is an expected fail-closed path, not a handler error.
            logger.info(
                "JDBC connection closed on session revocation (pid=%d): %s",
                self._pid, exc,
            )
        except proto.FrameTooLargeError as exc:
            logger.warning("JDBC oversize frame from %s (pid=%d): %s", peer, self._pid, exc)
        except Exception as exc:
            logger.error("JDBC handler error (pid=%d): %s", self._pid, exc)
        finally:
            governor.release(self._peer_ip)
            if self._catalogue is not None:
                self._catalogue.close()
            writer.close()
            logger.info("JDBC connection closed (pid=%d)", self._pid)

    # ------------------------------------------------------------------
    # Startup handshake
    # ------------------------------------------------------------------

    async def _run(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        # Phase 1: startup negotiation (may start with SSLRequest)
        startup = await proto.read_startup(reader)
        if startup["type"] == "cancel":
            # Bug-5188: CancelRequest on a fresh socket — look up the target
            # connection's in-flight query task and cancel it.
            cancel_pid = startup.get("pid", 0)
            cancel_secret = startup.get("secret", 0)
            cancel_key = (cancel_pid, cancel_secret)
            with _conn_counter_lock:
                task = _inflight_tasks.get(cancel_key)
            if task is not None and not task.done():
                task.cancel()
                logger.info(
                    "JDBC CancelRequest: cancelled in-flight query (pid=%s)",
                    cancel_pid,
                )
            else:
                logger.info(
                    "JDBC CancelRequest received (pid=%s) — no matching in-flight query",
                    cancel_pid,
                )
            return
        if startup["type"] == "ssl":
            if _ssl_context is not None:
                writer.write(proto.ssl_accept())
                await writer.drain()
                try:
                    transport = writer.transport
                    protocol = transport.get_protocol()
                    loop = asyncio.get_event_loop()
                    new_transport = await loop.start_tls(
                        transport, protocol, _ssl_context, server_side=True,
                    )
                    writer._transport = new_transport  # noqa: SLF001
                    self._tls_active = True
                except Exception as exc:
                    logger.error("TLS handshake failed (pid=%d): %s", self._pid, exc)
                    return
            else:
                writer.write(proto.ssl_deny())
                await writer.drain()
            startup = await proto.read_startup(reader)

        if startup["type"] != "startup":
            writer.write(proto.error_response("Unexpected startup message"))
            await writer.drain()
            return

        # F-001-08: require-TLS switch. When enabled, the gateway refuses any
        # startup that is not running over a negotiated TLS channel — a client
        # configured ``sslmode=disable``, or one downgraded by an active
        # attacker who answered the SSLRequest with 'N', would otherwise send
        # the tenant password in cleartext. Fail-closed with SQLSTATE 28000
        # before the auth challenge so no credential ever crosses the wire.
        if settings.GATEWAY_SSL_REQUIRED and not self._tls_active:
            logger.warning(
                "JDBC plaintext startup refused (pid=%d): TLS is required", self._pid
            )
            writer.write(
                proto.error_response(
                    "This server requires an SSL/TLS connection; "
                    "reconnect with sslmode=require.",
                    severity="FATAL",
                    code="28000",
                )
            )
            await writer.drain()
            return

        params = startup["params"]
        logger.info("Startup params (pid=%d): database=%r user=%r application_name=%r",
                     self._pid, params.get("database"), params.get("user"),
                     params.get("application_name"))
        self._client_kind = _client_kind_from_application_name(params.get("application_name"))
        if self._client_kind in _LOOKER_CLIENT_KINDS and not settings.LOOKER_GATEWAY_ENABLED:
            writer.write(proto.error_response(_LOOKER_DISABLED, severity="FATAL", code="0A000"))
            await writer.drain()
            return
        if self._client_kind in _LOOKER_CLIENT_KINDS and not self._tls_active:
            writer.write(
                proto.error_response(
                    "Looker connections require TLS; configure SSL mode=require.",
                    severity="FATAL",
                    code="08004",
                )
            )
            await writer.drain()
            return
        authenticated = await self._authenticate(params, reader, writer)
        if not authenticated:
            return  # error already sent

        # Fetch model metadata once at connection time (best-effort), BEFORE
        # the startup confirmation so an invalid model/project hint can be
        # rejected as a standard FATAL the client actually displays (an error
        # sent after ReadyForQuery is lost when the connection closes).
        # If no model_id was given, all enabled models for the tenant are loaded
        # so DBeaver can browse tables without specifying a model upfront.
        (
            self._model_names,
            self._table_columns,
            self._table_model_id,
            self._table_descriptions,
            self._table_trust_meta,
            self._table_persona_id,
            self._table_include_hidden,
            self._table_query_name,
            self._table_foreign_keys,
            self._table_row_estimates,
            self._looker_relations,
            self._table_project_slug,
        ) = await fetch_model_metadata(
            self._model_id,
            self._tenant_slug,
            self._jwt_token,
            project_slug=getattr(self, "_project_hint", None),
        )
        logger.info(
            "Metadata loaded (pid=%d): tenant=%s model_id=%s tables=%s",
            self._pid, self._tenant_slug, self._model_id, self._model_names,
        )
        # Bug-5878: a model (and optional project) hint that resolved to no
        # relations means the connection can never serve a query — fail now
        # with a clear message instead of "Cannot determine model" per query.
        if self._model_id and not self._model_names:
            _scope = (
                f"project {self._project_hint!r}"
                if getattr(self, "_project_hint", None)
                else f"tenant {self._tenant_slug!r}"
            )
            writer.write(
                proto.error_response(
                    f"Unknown model {str(self._model_id)!r} in {_scope}. "
                    "Connect with dbname <tenant>, <tenant>/<model>, or "
                    "<tenant>/<project>/<model> using the model's slug.",
                    severity="FATAL",
                    code="3D000",
                )
            )
            await writer.drain()
            return
        # Normalize a slug hint to the resolved model UUID so downstream
        # fallbacks (_resolve_model_id_for_sql) hand the router a real id.
        if self._model_id and self._table_model_id:
            _resolved_ids = set(self._table_model_id.values())
            if len(_resolved_ids) == 1 and str(self._model_id) not in _resolved_ids:
                self._model_id = next(iter(_resolved_ids))

        # Phase 2: send startup confirmation
        writer.write(proto.startup_sequence(pid=self._pid, secret=self._cancel_secret))
        await writer.drain()
        self._catalogue = CatalogueDB(
            model_names=self._model_names,
            table_columns=self._table_columns,
            table_descriptions=self._table_descriptions,
            table_trust_meta=self._table_trust_meta,
            table_model_id=self._table_model_id,
            table_foreign_keys=self._table_foreign_keys,
            table_row_estimates=self._table_row_estimates,
            looker_enabled=settings.LOOKER_GATEWAY_ENABLED,
            looker_relations=self._looker_relations,
            tenant_slug=self._tenant_slug,
            table_project_slug=self._table_project_slug,
        )
        self._catalogue_built_at = time.monotonic()
        # Phase 3: query loop
        await self._query_loop(reader, writer)

    async def _authenticate(
        self,
        params: dict[str, str],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> bool:
        """
        Authenticate using a PostgreSQL-standard challenge-response flow.

        Startup params:
          database  → tenant_slug
          user      → email
          model_id  → optional model UUID
          password  → ignored from startup params (some clients send it here,
                      but we always issue the cleartext challenge so that
                      standard clients like DBeaver work correctly)

        Flow:
          1. Send AuthenticationCleartextPassword challenge
          2. Read PasswordMessage from client
          3. If password looks like a JWT (starts with "ey"), validate directly
          4. Otherwise exchange (tenant_slug, email, password) for a JWT
        """
        raw_db = params.get("database", "default")
        # Bug-6933: acquire the governor BEFORE any early-return path so
        # pre-auth rejections (malformed dbname, empty password) are counted
        # toward the per-IP brute-force throttle.
        governor = get_governor()
        # Bug-5878: the dbname accepts <tenant>, <tenant>/<model>, or
        # <tenant>/<project>/<model>. Anything else is rejected here with a
        # clear message instead of leaking a "project/model" string into the
        # query-router's UUID parsing.
        parsed_db = _parse_database_param(raw_db)
        if parsed_db is None:
            governor.record_auth_failure(self._peer_ip)
            writer.write(
                proto.error_response(
                    f"Invalid database name {raw_db!r}. Use <tenant>, "
                    "<tenant>/<model>, or <tenant>/<project>/<model>.",
                    severity="FATAL",
                    code="3D000",
                )
            )
            await writer.drain()
            return False
        self._tenant_slug, self._project_hint, model_hint = parsed_db
        if not self._model_id and model_hint:
            self._model_id = model_hint
        self._model_id = self._model_id or params.get("model_id") or None
        email = params.get("user", "")

        # Always issue the cleartext challenge — this is what DBeaver and
        # every standard PostgreSQL client expects.
        writer.write(proto.authentication_cleartext_password())
        await writer.drain()

        # Read the PasswordMessage the client sends in response.
        try:
            password = await proto.read_password_message(reader)
        except Exception as exc:
            # F-001-11: never leak internal detail to an unauthenticated
            # client; log the cause server-side, send a fixed message.
            logger.warning("JDBC password read failed (pid=%d): %s", self._pid, exc)
            await self._deny_auth(writer, governor)
            return False

        if not password:
            # Bug-6933: count empty-password rejections toward the throttle.
            governor.record_auth_failure(self._peer_ip)
            writer.write(
                proto.error_response(
                    "Password required. Connect with user=<email> password=<password>.",
                    severity="FATAL",
                    code="28000",
                )
            )
            await writer.drain()
            return False

        # Backwards compat: if the caller already has a JWT, accept it directly.
        if password.startswith("ey"):
            try:
                payload = verify_jwt_token(password)
            except Exception as exc:
                logger.info("JDBC direct-JWT validation failed (pid=%d): %s", self._pid, exc)
                payload = None
            if payload is not None:
                # Bug-7322 (gateway consumer half): verify that the session
                # has not been revoked (user deactivated, demoted, or
                # token_version bumped) before accepting the connection.
                try:
                    await validate_session_upstream(password)
                except ValueError as exc:
                    logger.info(
                        "JDBC direct-JWT session revoked (pid=%d): %s",
                        self._pid, exc,
                    )
                    await self._deny_auth(writer, governor)
                    return False
                # F-001-10: the database startup param is client-controlled and
                # only cosmetic (it drives current_database()/pg_database). The
                # JWT is the authoritative tenant. Reject a connection whose JWT
                # tenant does not match the requested database so a tenant-B
                # token can never be presented under a database=tenant_a label.
                if not self._tenant_matches(payload.tenant_id, self._tenant_slug):
                    logger.warning(
                        "JDBC tenant mismatch (pid=%d): jwt tenant=%r database=%r — denied",
                        self._pid, payload.tenant_id, self._tenant_slug,
                    )
                    await self._deny_auth(writer, governor)
                    return False
                # Bug-6919: resolve placeholder database names to the JWT's
                # authoritative tenant_id so downstream code (logs, catalogue
                # cache keys, current_database()) always reflects the real
                # tenant and the placeholder never acts as a wildcard.
                if self._tenant_slug in self._PLACEHOLDER_DB_NAMES:
                    logger.info(
                        "JDBC placeholder db %r resolved to JWT tenant %r (pid=%d)",
                        self._tenant_slug, payload.tenant_id, self._pid,
                    )
                    self._tenant_slug = payload.tenant_id
                self._jwt_token = password
                governor.record_auth_success(self._peer_ip)
                return True
            # fall through to password exchange

        # Exchange email + password for JWT via model-service. The exchange is
        # scoped to the database (tenant_slug) param, so the returned JWT is
        # inherently for that tenant; we still verify the claim defensively.
        try:
            self._jwt_token = await login_for_token(self._tenant_slug, email, password)
        except Exception as exc:
            # F-001-11: the upstream exception text can contain the internal
            # model-service URL and status. Log it server-side only and send a
            # fixed, topology-free message to the client.
            logger.info("JDBC login failed (pid=%d): %s", self._pid, exc)
            await self._deny_auth(writer, governor)
            return False

        try:
            payload = verify_jwt_token(self._jwt_token)
        except Exception as exc:
            logger.warning("JDBC issued-JWT validation failed (pid=%d): %s", self._pid, exc)
            await self._deny_auth(writer, governor)
            return False
        if not self._tenant_matches(payload.tenant_id, self._tenant_slug):
            # Fail-closed: a token whose tenant does not match the requested
            # database must never be served, even though model-service issued it.
            logger.warning(
                "JDBC tenant mismatch after login (pid=%d): jwt tenant=%r database=%r — denied",
                self._pid, payload.tenant_id, self._tenant_slug,
            )
            self._jwt_token = ""
            await self._deny_auth(writer, governor)
            return False

        # G2 / grok F-001-01: the direct-JWT branch validates the session
        # upstream (server.py ~741), but the password-exchange branch only
        # checked JWT signature/expiry (verify_jwt_token). model-service issues
        # a token on valid credentials, but a freshly-issued token can already
        # be for a user whose role or token_version changed between issuance and
        # this check; more importantly this closes the parity gap so BOTH JDBC
        # connect paths run the same fail-closed revocation check as XMLA.
        try:
            await validate_session_upstream(self._jwt_token)
        except ValueError as exc:
            logger.info(
                "JDBC issued-JWT session revoked (pid=%d): %s",
                self._pid, exc,
            )
            self._jwt_token = ""
            await self._deny_auth(writer, governor)
            return False

        # Bug-6919: resolve placeholder database names to the JWT's
        # authoritative tenant_id (same as the JWT-direct path above).
        if self._tenant_slug in self._PLACEHOLDER_DB_NAMES:
            logger.info(
                "JDBC placeholder db %r resolved to JWT tenant %r (pid=%d)",
                self._tenant_slug, payload.tenant_id, self._pid,
            )
            self._tenant_slug = payload.tenant_id

        governor.record_auth_success(self._peer_ip)
        return True

    # Bug-6919: placeholder database names that tools send when the user does
    # not specify a tenant.  After authentication, these are resolved to the
    # JWT's authoritative tenant_id so they never act as wildcards.
    _PLACEHOLDER_DB_NAMES = frozenset({"", "default", "postgres"})

    @staticmethod
    def _tenant_matches(jwt_tenant: str, database_param: str) -> bool:
        """Return True iff the JWT tenant equals the database startup param.

        F-001-10 cross-check. Fail-closed: a blank JWT tenant claim never
        matches anything, so a token without a tenant_id cannot satisfy a
        tenant-scoped connection.

        Bug-6919: placeholder database names (``"default"``, ``"postgres"``,
        ``""``) are accepted ONLY when the JWT is valid, and the caller is
        responsible for resolving ``self._tenant_slug`` to the JWT tenant
        immediately after this returns ``True``.  The previous implementation
        accepted them as unconditional wildcards, weakening defence-in-depth.
        """
        if not jwt_tenant:
            return False
        if database_param == jwt_tenant:
            return True
        # Bug-6919: accept placeholder db names only when the JWT is valid
        # (the caller resolves self._tenant_slug to jwt_tenant immediately).
        if database_param in PGWireServer._PLACEHOLDER_DB_NAMES:
            return True
        return False

    async def _deny_auth(self, writer: asyncio.StreamWriter, governor) -> None:
        """Record the failure and send a fixed, topology-free auth error.

        F-001-11: the message is identical for every failure cause (bad
        password, unknown tenant, tenant mismatch, malformed message) so an
        unauthenticated client learns nothing about internal structure.
        F-001-07: each failure is counted against the per-IP throttle window.
        """
        governor.record_auth_failure(self._peer_ip)
        writer.write(
            proto.error_response(
                "Authentication failed: invalid credentials or tenant.",
                severity="FATAL",
                code="28000",
            )
        )
        await writer.drain()

    async def _revalidate_session(self) -> None:
        """G2 / grok F-001-01: re-check session revocation on a long-lived
        connection before dispatching a user query to the router.

        The connect-time handshake validates the session once (Bug-7322), but a
        pooled JDBC connection can stay open long after the user is deactivated,
        demoted, or has their token_version bumped. This mirrors the XMLA SOAP
        handlers, which call ``validate_session_upstream`` on every request.

        ``validate_session_upstream`` keeps its own TTL cache
        (``_SESSION_CHECK_TTL_SECONDS``, default 30s), so calling it before each
        query costs at most one model-service round-trip per cadence window; a
        fresh, still-valid session is served from cache. It fails CLOSED: an
        unreachable model-service, a 401, or any non-200 raises ``ValueError``.

        Raises:
            SessionRevokedError — the session is no longer valid. Callers must
                emit a SQLSTATE 28000 ErrorResponse and let ``handle_client``
                close the socket.
        """
        if not self._jwt_token:
            # No token: the caller's own ``if not self._jwt_token`` guard
            # already emits the 28000 "Authentication required." error.
            return
        try:
            await validate_session_upstream(self._jwt_token)
        except ValueError as exc:
            logger.info(
                "JDBC session revoked mid-connection (pid=%d): %s",
                self._pid, exc,
            )
            raise SessionRevokedError(str(exc)) from exc

    # ------------------------------------------------------------------
    # Query loop
    # ------------------------------------------------------------------

    async def _query_loop(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        # Bug-6934: per-portal execution state.  Each portal created by Bind
        # owns its SQL, result columns, result rows, result format codes, and
        # bound flag independently.  The unnamed portal ("") is the default
        # for the common single-portal flow.
        _portals: dict[str, dict] = {}
        # The "current" portal name — updated by Bind, used by Describe/Execute
        # when the message names the unnamed portal.
        _current_portal: str = ""

        # Statement-level metadata for ParameterDescription — keyed to the
        # most recent unnamed-statement Parse (the only shape that needs
        # statement-level Describe).
        _pending_num_params: int = 0
        # Parameter-type OIDs declared by the client in the active Parse
        # message (F-001-06). Drives whether Bind inlines a value raw or
        # quotes it as text.
        _pending_param_oids: list[int] = []

        # Per pgwire spec: once an error is emitted in the extended protocol,
        # all messages are skipped until the next Sync.  _error_skip tracks
        # that "aborted transaction block" state so we don't double-report.
        _error_skip: bool = False

        def _get_portal(name: str) -> dict:
            """Return the portal state dict, creating an empty one if absent."""
            if name not in _portals:
                _portals[name] = {
                    "sql": "",
                    "rows": None,
                    "cols": None,
                    "formats": [],
                    "bound": False,
                    "row_offset": 0,
                }
            return _portals[name]

        while True:
            type_char, payload = await proto.read_message(reader)

            if type_char == "X":  # Terminate
                return

            elif type_char == "Q":  # Simple Query
                sql = payload.rstrip(b"\x00").decode("utf-8", errors="replace")
                await self._log_sql(sql, "Q")
                # Bug-8107 F1: a Simple Query destroys the unnamed prepared
                # statement and the unnamed portal (PG protocol). Without this,
                # Parse("",A) -> Query(B) -> Bind("","") -> Execute("") would
                # bind portal "" to the STALE statement "" (=A) and run A. Drop
                # the unnamed statement/portal so a later unnamed Bind fails
                # loud (26000) instead of resurrecting stale SQL.
                self._statements.pop("", None)
                self._statement_param_oids.pop("", None)
                _portals.pop("", None)
                await self._handle_query(sql, writer)

            # ----------------------------------------------------------------
            # Extended query protocol (used by DBeaver, psycopg2, pgJDBC)
            # ----------------------------------------------------------------

            elif type_char == "P":  # Parse — extract SQL, cache by statement name
                if _error_skip:
                    continue
                stmt_name, raw_sql, param_oids = proto.parse_parse_message(payload)
                await self._log_sql(raw_sql, "P")
                # Bug-7591 / codex-R1-F7: reject multi-statement in Parse too
                # (PG spec: Parse must contain at most one statement).
                try:
                    _p_stmts = sqlglot.parse(raw_sql.strip().rstrip(";"), read="postgres")
                    if _p_stmts and len([s for s in _p_stmts if s is not None]) > 1:
                        writer.write(proto.error_response(
                            "Multi-statement queries are not supported by the "
                            "Tessallite gateway. Submit each statement individually.",
                            severity="ERROR",
                            code="0A000",
                        ))
                        _error_skip = True
                        await writer.drain()
                        continue
                except Exception:
                    pass
                self._statements[stmt_name] = raw_sql
                # F-001-06: remember the client-declared parameter type OIDs so
                # Bind can quote text parameters as text rather than sniffing.
                self._statement_param_oids[stmt_name] = param_oids
                # Bug-8107 F1: ONLY an unnamed Parse touches the unnamed portal.
                # It seeds the metadata placeholder (unbound) that preserves the
                # unnamed describe-before-bind flow (F-001-01/Bug-6056) while
                # leaving ``bound`` False so Execute cannot run it before Bind.
                # A NAMED Parse must NOT populate portal "" — doing so let a
                # later Execute("") silently run the named statement's SQL with
                # no Bind. Portal existence is no longer a proxy for "bound".
                if stmt_name == "":
                    unnamed = _get_portal("")
                    unnamed["sql"] = raw_sql
                    unnamed["rows"] = None
                    unnamed["cols"] = None
                    unnamed["formats"] = []
                    unnamed["bound"] = False
                    _current_portal = ""
                _pending_param_oids = param_oids
                _pending_num_params = _count_param_placeholders(raw_sql)
                writer.write(proto.parse_complete())
                await writer.drain()

            elif type_char == "B":  # Bind — extract params and result format codes
                if _error_skip:
                    continue
                # Bug-5187: resolve the param OIDs BEFORE parsing the Bind
                # payload so binary parameter decoding uses the declared types
                # from the Parse message rather than a byte-length heuristic.
                _portal_peek, _stmt_peek = proto.peek_bind_names(payload)
                # Bug-8107: an unknown/un-Parse'd statement name must be
                # rejected outright — NEVER silently fall back to the
                # unnamed portal's SQL, which would bind (and later execute)
                # a completely different statement than the one the client
                # named. SQLSTATE 26000 = invalid_sql_statement_name.
                if _stmt_peek not in self._statements:
                    writer.write(proto.error_response(
                        f"prepared statement {_stmt_peek!r} does not exist",
                        severity="ERROR",
                        code="26000",
                    ))
                    _error_skip = True
                    await writer.drain()
                    continue
                raw_sql = self._statements[_stmt_peek]
                # F-001-06: prefer the parameter OIDs the named statement
                # declared; fall back to the most recent Parse for the
                # unnamed statement flow.
                param_oids = self._statement_param_oids.get(_stmt_peek, _pending_param_oids)
                # Wave C #6: parameter decode + literal termination happen here.
                # A parameter the gateway cannot terminate into a safe typed
                # literal (unsupported OID, or a binary encoding it cannot decode
                # exactly) fails with a STABLE protocol error — never a guessed
                # value that could silently change a number.
                try:
                    portal_name, _stmt, params, result_formats = proto.parse_bind_parameters(
                        payload, param_oids=param_oids,
                    )
                    _bound_sql = (
                        _substitute_params(raw_sql, params, param_oids)
                        if params else raw_sql
                    )
                except proto.ParamDecodeError as exc:
                    writer.write(proto.error_response(
                        str(exc),
                        severity="ERROR",
                        code=getattr(exc, "sqlstate", "0A000"),
                    ))
                    _error_skip = True
                    await writer.drain()
                    continue
                # Bug-6934: create/update the named portal's own state.
                portal = _get_portal(portal_name)
                portal["sql"] = _bound_sql
                if params:
                    logger.debug("Bind substitution (pid=%d portal=%r): %s",
                                 self._pid, portal_name, portal["sql"][:300])
                # A fresh Bind invalidates any rows that a prior statement-level
                # Describe may have produced; the portal now carries new params.
                portal["bound"] = True
                portal["rows"] = None
                portal["cols"] = None
                portal["row_offset"] = 0
                portal["formats"] = result_formats
                _current_portal = portal_name
                writer.write(proto.bind_complete())
                await writer.drain()

            elif type_char == "D":  # Describe portal/statement
                if _error_skip:
                    continue
                describe_type = chr(payload[0]) if payload else "P"
                describe_name = payload[1:].rstrip(b"\x00").decode("utf-8", errors="replace") if len(payload) > 1 else ""
                if describe_type == "S":
                    # Bug-8107: same rule as Bind — an unknown/un-Parse'd
                    # statement name is rejected, never silently described
                    # against the unnamed statement's SQL.
                    if describe_name not in self._statements:
                        writer.write(proto.error_response(
                            f"prepared statement {describe_name!r} does not exist",
                            severity="ERROR",
                            code="26000",
                        ))
                        _error_skip = True
                        await writer.drain()
                        continue
                    # Statement-level Describe: resolve param count from named
                    # or unnamed statement.
                    stmt_sql = self._statements[describe_name]
                    num_params = _count_param_placeholders(stmt_sql) if stmt_sql else _pending_num_params
                    param_oids = [proto.OID_TEXT] * num_params
                    writer.write(proto.parameter_description(param_oids))
                    # Statement-Describe returns RowDescription from metadata;
                    # never executes the query.
                    if not stmt_sql:
                        writer.write(proto.no_data())
                        await writer.drain()
                        continue
                    derived = self._describe_columns_metadata_only(stmt_sql)
                    if derived is not None:
                        writer.write(proto.row_description(derived, []))
                    else:
                        writer.write(proto.no_data())
                    await writer.drain()
                    continue

                # Portal-level Describe (describe_type == "P").
                # Bug-8107: a portal that was never created by Bind (or, for
                # the unnamed portal, by Parse — see the F-001-01/Bug-6056
                # describe-before-bind flow just below) is an invalid cursor.
                # NEVER auto-vivify it into a fake empty result — reject with
                # SQLSTATE 34000 (invalid_cursor_name).
                if describe_name not in _portals:
                    writer.write(proto.error_response(
                        f"portal {describe_name!r} does not exist",
                        severity="ERROR",
                        code="34000",
                    ))
                    _error_skip = True
                    await writer.drain()
                    continue
                portal = _get_portal(describe_name)
                if not portal["sql"]:
                    writer.write(proto.no_data())
                    await writer.drain()
                    continue

                # F-001-01 / Bug-6056 — execute the user query at most once.
                if not portal["bound"]:
                    derived = self._describe_columns_metadata_only(portal["sql"])
                    if derived is not None:
                        portal["cols"] = derived
                        writer.write(proto.row_description(derived, portal["formats"]))
                    else:
                        portal["cols"] = None
                        writer.write(proto.no_data())
                    portal["rows"] = None
                    await writer.drain()
                    continue

                # Bug-6592 F4: Describe must be metadata-ONLY. For a bound
                # housekeeping command (SET LOCAL, COMMIT, ROLLBACK, DISCARD,
                # RESET, BEGIN...) the execution helper mutates connection state
                # — capturing a var, clearing LOCAL scope, closing the explicit
                # transaction. Running it during Describe would apply that side
                # effect BEFORE the client's Execute (e.g. describing a bound
                # COMMIT would clear LOCAL too early; describing a bound SET
                # LOCAL would take effect without ever executing). These
                # commands produce no result columns: answer NoData and leave
                # the portal unexecuted so the later Execute performs it.
                if _is_side_effecting_housekeeping(portal["sql"]):
                    portal["cols"] = None
                    portal["rows"] = None
                    writer.write(proto.no_data())
                    await writer.drain()
                    continue

                try:
                    portal["cols"], portal["rows"], err = await self._execute_for_extended(portal["sql"])
                except SessionRevokedError:
                    # G2 / grok F-001-01: session revoked mid-connection. Fail
                    # closed — report 28000, then re-raise so handle_client
                    # closes the socket.
                    writer.write(proto.error_response(
                        "Session has been revoked; reconnect to continue.",
                        severity="FATAL", code="28000",
                    ))
                    await writer.drain()
                    raise
                if err is not None:
                    writer.write(proto.error_response(err[0], code=err[1]))
                    _error_skip = True
                    portal["rows"] = None
                    portal["cols"] = None
                elif portal["cols"] is not None:
                    writer.write(proto.row_description(portal["cols"], portal["formats"]))
                else:
                    writer.write(proto.no_data())
                await writer.drain()

            elif type_char == "E":  # Execute → DataRow* + CommandComplete
                if _error_skip:
                    continue
                # Bug-6934: resolve the portal name from the Execute payload.
                # Bug-6935: also parse the max-rows field (int32 after the
                # null-terminated portal name; 0 means unlimited).
                _e_parts = payload.split(b"\x00", 1) if payload else [b"", b""]
                exec_portal_name = _e_parts[0].decode("utf-8", errors="replace")
                _e_tail = _e_parts[1] if len(_e_parts) > 1 else b""
                max_rows = struct.unpack("!i", _e_tail[:4])[0] if len(_e_tail) >= 4 else 0
                # Bug-8107: reject Execute against a portal that was never
                # created by Bind (or Parse, for the unnamed-portal flow) —
                # never silently treat it as an empty "SELECT 0" success.
                # SQLSTATE 34000 (invalid_cursor_name).
                if exec_portal_name not in _portals:
                    writer.write(proto.error_response(
                        f"portal {exec_portal_name!r} does not exist",
                        severity="ERROR",
                        code="34000",
                    ))
                    _error_skip = True
                    await writer.drain()
                    continue
                portal = _get_portal(exec_portal_name)
                # Bug-8107 F1: Execute requires a portal that Bind actually
                # bound. The unnamed portal can EXIST after an unnamed Parse
                # (the describe-before-bind placeholder) yet never have been
                # bound; executing it would run the statement with no
                # parameters and no Bind. Reject 34000 (invalid_cursor_name)
                # rather than silently execute the wrong/parameterless SQL.
                if not portal["bound"]:
                    writer.write(proto.error_response(
                        f"portal {exec_portal_name!r} is not bound",
                        severity="ERROR",
                        code="34000",
                    ))
                    _error_skip = True
                    await writer.drain()
                    continue
                described = portal["cols"] is not None
                # Re-execute when Describe was skipped or rows were
                # deferred (parameterised queries — real params arrive
                # via Bind after Describe).
                if portal["rows"] is None and portal["sql"]:
                    try:
                        portal["cols"], portal["rows"], err = await self._execute_for_extended(portal["sql"])
                    except SessionRevokedError:
                        # G2 / grok F-001-01: fail closed on revoked session.
                        writer.write(proto.error_response(
                            "Session has been revoked; reconnect to continue.",
                            severity="FATAL", code="28000",
                        ))
                        await writer.drain()
                        raise
                    if err is not None:
                        writer.write(proto.error_response(err[0], code=err[1]))
                        _error_skip = True
                        portal["rows"] = None
                        portal["cols"] = None
                        await writer.drain()
                        continue

                if portal["cols"] is None:
                    writer.write(proto.command_complete("SELECT 0"))
                else:
                    col_oids = [oid for _, oid in portal["cols"]]
                    if not described:
                        writer.write(proto.row_description(portal["cols"], portal["formats"]))
                    all_rows = portal["rows"] or []
                    offset = portal.get("row_offset", 0)
                    remaining = all_rows[offset:]
                    if max_rows > 0 and len(remaining) > max_rows:
                        # Bug-6935: emit only the requested number of rows
                        # and signal PortalSuspended so the client can fetch
                        # additional pages.
                        page = remaining[:max_rows]
                        for row in page:
                            writer.write(proto.data_row(row, portal["formats"], col_oids))
                        portal["row_offset"] = offset + max_rows
                        writer.write(proto.portal_suspended())
                    else:
                        for row in remaining:
                            writer.write(proto.data_row(row, portal["formats"], col_oids))
                        total = len(all_rows)
                        writer.write(proto.command_complete(f"SELECT {total}"))
                        portal["rows"] = None
                        portal["cols"] = None
                        portal["row_offset"] = 0
                await writer.drain()

            elif type_char == "S":  # Sync — flush, clear error state, signal ready
                _error_skip = False
                # Bug-6934: clear all portals on Sync (PG spec: unnamed portal
                # is destroyed at end of transaction; named portals survive
                # until Close, but our gateway is transactionless so clearing
                # all is the safe default).
                _portals.clear()
                # Bug-6592 F4: Sync ends an IMPLICIT transaction. Transaction-
                # scoped SET LOCAL values must not survive it — clear them.
                # Inside an EXPLICIT BEGIN, though, the transaction spans Syncs
                # and only COMMIT/ROLLBACK ends it, so LOCAL is retained.
                if not self._in_explicit_txn:
                    self._session_vars_local.clear()
                writer.write(proto.ready_for_query())
                await writer.drain()

            elif type_char == "H":  # Flush — drain buffered output immediately
                await writer.drain()

            elif type_char == "C":  # Close statement/portal
                if payload and len(payload) > 1:
                    close_type = chr(payload[0])
                    close_name = payload[1:].rstrip(b"\x00").decode("utf-8", errors="replace")
                    if close_type == "S" and close_name in self._statements:
                        del self._statements[close_name]
                        self._statement_param_oids.pop(close_name, None)
                    elif close_type == "P":
                        # Bug-6934: close the named portal.
                        _portals.pop(close_name, None)
                writer.write(proto.close_complete())
                await writer.drain()

            else:
                logger.debug("Unsupported message type '%s' (pid=%d)", type_char, self._pid)
                writer.write(proto.error_response(f"Unsupported message type: {type_char}", code="0A000"))
                writer.write(proto.ready_for_query())
                await writer.drain()

    # ------------------------------------------------------------------
    # Query dispatch
    # ------------------------------------------------------------------

    async def _run_cancellable(self, coro):
        """Run *coro* as a cancellable task registered in the inflight map.

        Bug-5188: wraps the router call in an asyncio Task and registers it
        under ``(pid, cancel_secret)`` so a CancelRequest on a separate
        socket can look it up and cancel it. On cancellation, raises
        ``asyncio.CancelledError`` which the caller translates to an
        ErrorResponse with SQLSTATE 57014 (query_canceled).
        """
        cancel_key = (self._pid, self._cancel_secret)
        # Wrap the coroutine in an inner Task so it can be cancelled
        # independently via the inflight registry.
        inner = asyncio.ensure_future(coro)
        with _conn_counter_lock:
            _inflight_tasks[cancel_key] = inner
        try:
            return await inner
        finally:
            with _conn_counter_lock:
                _inflight_tasks.pop(cancel_key, None)

    def _rebuild_catalogue_for_client_kind(self) -> None:
        """Rebuild the in-memory catalogue after a mid-session client detection.

        Bug-6920: when ``_client_kind_from_sql`` identifies a Looker client on
        the first user query (after the catalogue was built without Looker
        context), rebuild the ``CatalogueDB`` with the deployment's
        ``LOOKER_GATEWAY_ENABLED`` setting applied. When the setting is
        ``False``, Looker-specific relations are removed from the catalogue.

        Limitation: ``CatalogueDB`` does not have a "Looker-only" mode that
        hides non-Looker relations. The data-access guard (the
        ``_LOOKER_CLIENT_KINDS`` check at query time) prevents cross-surface
        data access regardless.
        """
        if self._catalogue is not None:
            self._catalogue.close()
        looker_enabled = (
            settings.LOOKER_GATEWAY_ENABLED
            if self._client_kind in _LOOKER_CLIENT_KINDS
            else True
        )
        self._catalogue = CatalogueDB(
            model_names=self._model_names,
            table_columns=self._table_columns,
            table_descriptions=self._table_descriptions,
            table_trust_meta=self._table_trust_meta,
            table_model_id=self._table_model_id,
            table_foreign_keys=self._table_foreign_keys,
            table_row_estimates=self._table_row_estimates,
            looker_enabled=looker_enabled,
            looker_relations=self._looker_relations,
            tenant_slug=self._tenant_slug,
            table_project_slug=self._table_project_slug,
        )
        self._catalogue_built_at = time.monotonic()
        logger.info(
            "Catalogue rebuilt for client_kind=%r (pid=%d)",
            self._client_kind, self._pid,
        )

    async def _refresh_catalogue_if_stale(self) -> None:
        """Bug-7043: re-fetch model metadata and rebuild the catalogue when
        the CLS TTL has expired.

        After a persona's column-level security restrictions change, an
        already-open JDBC connection would continue serving the old catalogue
        (columns the user should no longer see, or missing newly-allowed
        columns). This method checks a configurable TTL and, when expired,
        re-fetches model metadata from the model-service — which re-resolves
        persona allow-lists and restricted_column_ids — then rebuilds the
        in-memory SQLite catalogue.

        SECURITY: default TTL is 0 (re-validate on every catalogue request)
        so a tightened CLS immediately hides now-restricted columns from the
        field list. The query-router enforcement is always live regardless,
        but the catalogue metadata should not advertise restricted columns.
        """
        ttl = int(system_snapshot_get("gateway.catalogue_cls_ttl"))
        now = time.monotonic()
        if ttl > 0 and (now - self._catalogue_built_at) < ttl:
            return  # still within TTL — keep the current catalogue

        # Re-fetch model metadata (includes CLS-filtered column lists)
        try:
            (
                model_names,
                table_columns,
                table_model_id,
                table_descriptions,
                table_trust_meta,
                table_persona_id,
                table_include_hidden,
                table_query_name,
                table_foreign_keys,
                table_row_estimates,
                looker_relations,
                table_project_slug,
            ) = await fetch_model_metadata(
                self._model_id,
                self._tenant_slug,
                self._jwt_token,
                project_slug=getattr(self, "_project_hint", None),
            )
        except Exception as exc:
            logger.warning(
                "Bug-7043: catalogue CLS refresh failed (pid=%d): %s — "
                "keeping existing catalogue (fail-closed: stale catalogue "
                "retains the PRIOR restriction set, not an unrestricted set)",
                self._pid, exc,
            )
            # On failure, bump the timestamp so we don't retry every query
            # in a tight loop. The existing catalogue is still CLS-filtered
            # (just potentially stale), so this is fail-closed.
            self._catalogue_built_at = now
            return

        # Fail-closed guard: if the prior catalogue had persona-variant
        # relations but the new metadata does not, the persona API likely
        # failed silently (fetch_model_metadata returns an empty persona
        # list on error). Accepting such a snapshot would replace CLS-
        # filtered persona catalogues with the unrestricted base relation.
        # Reject and keep the prior (more restrictive) catalogue.
        old_persona_count = sum(
            1 for pid in self._table_persona_id.values() if pid is not None
        )
        new_persona_count = sum(
            1 for pid in table_persona_id.values() if pid is not None
        )
        if old_persona_count > 0 and new_persona_count == 0:
            logger.warning(
                "Bug-7043: catalogue CLS refresh returned no persona "
                "variants (had %d) — possible partial snapshot from "
                "model-service. Keeping prior catalogue (fail-closed, "
                "pid=%d).",
                old_persona_count, self._pid,
            )
            self._catalogue_built_at = now
            return

        # Check if anything actually changed before paying the rebuild cost.
        # Compare the full metadata snapshot including persona IDs and
        # hidden-column flags, not just column lists, so a persona
        # reassignment with the same column names is detected.
        if (
            model_names == self._model_names
            and table_columns == self._table_columns
            and table_persona_id == self._table_persona_id
            and table_include_hidden == self._table_include_hidden
            and table_model_id == self._table_model_id
        ):
            # Metadata unchanged — update timestamp, skip rebuild
            self._catalogue_built_at = now
            return

        # Metadata changed — build the replacement catalogue BEFORE
        # swapping instance state, so a constructor failure leaves the
        # prior catalogue intact (atomic swap).
        looker_enabled = (
            settings.LOOKER_GATEWAY_ENABLED
            if self._client_kind in _LOOKER_CLIENT_KINDS
            else True
        ) if self._client_kind else True

        try:
            new_catalogue = CatalogueDB(
                model_names=model_names,
                table_columns=table_columns,
                table_descriptions=table_descriptions,
                table_trust_meta=table_trust_meta,
                table_model_id=table_model_id,
                table_foreign_keys=table_foreign_keys,
                table_row_estimates=table_row_estimates,
                looker_enabled=looker_enabled,
                looker_relations=looker_relations,
                tenant_slug=self._tenant_slug,
                table_project_slug=table_project_slug,
            )
        except Exception as exc:
            logger.warning(
                "Bug-7043: catalogue rebuild failed (pid=%d): %s — "
                "keeping prior catalogue",
                self._pid, exc,
            )
            self._catalogue_built_at = now
            return

        # Atomic swap: update instance state only after successful build
        old_catalogue = self._catalogue
        self._catalogue = new_catalogue
        self._model_names = model_names
        self._table_columns = table_columns
        self._table_model_id = table_model_id
        self._table_descriptions = table_descriptions
        self._table_trust_meta = table_trust_meta
        self._table_persona_id = table_persona_id
        self._table_include_hidden = table_include_hidden
        self._table_query_name = table_query_name
        self._table_foreign_keys = table_foreign_keys
        self._table_row_estimates = table_row_estimates
        self._looker_relations = looker_relations
        self._table_project_slug = table_project_slug
        self._catalogue_built_at = time.monotonic()

        if old_catalogue is not None:
            old_catalogue.close()

        logger.info(
            "Bug-7043: catalogue rebuilt after CLS refresh (pid=%d, "
            "tables=%d)",
            self._pid, len(self._model_names),
        )

    def _portal_buffer_cap_error(self, row_count: int) -> tuple[str, str] | None:
        """Bug-8108: reject an oversized portal row buffer, fail closed.

        ``_execute_for_extended`` materialises the full result before
        Describe/Execute can page it out — PortalSuspended only bounds how
        many rows go on the wire per Execute call, not how many the gateway
        buffers in memory. Returns an ``(detail, sqlstate)`` error tuple
        (SQLSTATE 54000, program_limit_exceeded) when *row_count* exceeds the
        configured cap, else ``None``.
        """
        cap = _portal_row_buffer_cap()
        if cap <= 0 or row_count <= cap:
            return None
        logger.warning(
            "Bug-8108: portal row buffer cap exceeded (pid=%d): %d rows > "
            "cap %d",
            self._pid, row_count, cap,
        )
        return (
            f"Result set ({row_count:,} rows) exceeds the configured JDBC "
            f"portal row buffer cap ({cap:,} rows). Narrow the query (add "
            f"filters or a LIMIT clause) to reduce the result size.",
            "54000",
        )

    def _finalize_extended_result(
        self, col_desc: list | None, rows: list | None
    ) -> tuple[list | None, list | None, tuple[str, str] | None]:
        """Bug-8108 F2: single choke point every extended-protocol row producer
        returns through, so the portal row-buffer cap is enforced uniformly —
        catalogue results AND both query-router branches — before any DataRow
        reaches the wire. Returns the ``(col_desc, rows, None)`` success tuple,
        or ``(None, None, error)`` when the cap is exceeded.

        The cap was previously duplicated inline in only the two router
        branches, so a catalogue-backed portal returning more than the cap
        slipped through (``_catalogue.execute`` returned before any cap check).
        """
        err = self._portal_buffer_cap_error(len(rows or []))
        if err is not None:
            return None, None, err
        return col_desc, rows, None

    async def _execute_for_extended(
        self, sql: str
    ) -> tuple[list | None, list | None, tuple[str, str] | None]:
        """
        Run a query and return (col_desc, rows, error).

        On success: error is None.  Empty/DDL-style results return
        (None, None, None).  On failure: error is ``(detail, sqlstate)`` so
        the caller can emit an accurate ErrorResponse over the extended-query
        protocol (DBeaver, pgJDBC, psycopg2).
        """
        if not sql.strip():
            return None, None, None

        # SET / SHOW / transaction control are driver housekeeping.
        # Bug-6592 F4: normalise a single trailing ``;`` so ``COMMIT;`` etc.
        # match the exact-string classifiers (otherwise LOCAL scope leaks).
        stripped_upper = _strip_one_terminator(sql).upper()
        if stripped_upper.startswith("SET "):
            self._capture_session_var(sql.strip())
            return None, None, None
        if stripped_upper.startswith("SHOW "):
            return self._handle_show_extended(stripped_upper)
        if stripped_upper in ("BEGIN", "COMMIT", "ROLLBACK") or stripped_upper.startswith("SAVEPOINT "):
            # Bug-6592 F4: SET LOCAL scope ends at transaction end. BEGIN opens
            # an explicit transaction (so LOCAL survives across intervening
            # Syncs); COMMIT/ROLLBACK close it and clear LOCAL. BEGIN and
            # SAVEPOINT clear nothing (a savepoint nests inside the current
            # transaction; restoring LOCAL state on ROLLBACK TO SAVEPOINT
            # remains the parked architectural item noted on ``_SET_APP_RE``).
            if stripped_upper == "BEGIN":
                self._in_explicit_txn = True
            elif stripped_upper in ("COMMIT", "ROLLBACK"):
                self._session_vars_local.clear()
                self._in_explicit_txn = False
            return None, None, None
        if stripped_upper.startswith("DEALLOCATE"):
            return None, None, None
        # Bug-6929 / codex-R1-F6 / Bug-6592: DISCARD/RESET via extended
        # protocol must also clear session vars (both dicts), matching the
        # simple-query handler. DISCARD ALL is a full session reset — it also
        # aborts any open explicit transaction.
        if stripped_upper.startswith("DISCARD"):
            self._session_vars.clear()
            self._session_vars_local.clear()
            self._in_explicit_txn = False
            return None, None, None
        if stripped_upper.startswith("RESET ") or stripped_upper == "RESET":
            target = stripped_upper[6:].strip() if len(stripped_upper) > 5 else "ALL"
            if target.upper() == "ALL":
                self._session_vars.clear()
                self._session_vars_local.clear()
            else:
                key = target.lower()
                self._session_vars.pop(key, None)
                self._session_vars_local.pop(key, None)
            return None, None, None

        if self._catalogue is not None:
            # Bug-7043: refresh the catalogue if CLS TTL has expired so
            # tightened column restrictions are reflected immediately —
            # but ONLY for statements the catalogue will actually serve.
            # Classifying first (SOL-LAT-001) keeps Bug-7043's immediate CLS
            # revalidation for real catalogue queries while removing the full
            # tenant-metadata reload from the ordinary model-query hot path,
            # which made every JDBC query ~8s regardless of cache state.
            if self._catalogue.references_catalogue(sql):
                await self._refresh_catalogue_if_stale()
            try:
                # F-001-16: offload synchronous SQLite to a worker thread so a
                # metadata flood on one connection does not stall the loop.
                result = await asyncio.to_thread(self._catalogue.execute, sql)
            except CatalogueQueryError as exc:
                return None, None, (f"Catalogue query failed: {exc}", "42601")
            if result is not None:
                columns, rows = result
                # Bug-8108 F2: catalogue portals are capped too (this was the
                # bypass — the cap was only in the router branches).
                return self._finalize_extended_result(columns, rows)

        constant = self._try_constant_select(sql)
        if constant is not None:
            col_desc, rows = constant
            return self._finalize_extended_result(col_desc, rows)
        # F-001-05: a FROM-less SELECT we could not answer as literals
        # (``SELECT now()``, ``SELECT 1+1``) is evaluated by the catalogue
        # SQLite engine; if even that fails we reject with a clear error
        # rather than forwarding to the router or echoing expression text.
        if _is_constant_select_sqlglot(sql.strip().rstrip(";")):
            evaluated = self._eval_constant_select(sql)
            if evaluated is not None:
                col_desc, rows = evaluated
                return self._finalize_extended_result(col_desc, rows)
            return None, None, (
                "Unsupported constant expression; the gateway can evaluate "
                "literal and simple scalar SELECTs only.",
                "0A000",
            )

        if not self._jwt_token:
            return None, None, ("Authentication required.", "28000")

        # G2 / grok F-001-01: re-check session revocation before dispatching to
        # the router on this long-lived connection. Raises SessionRevokedError
        # (fail-closed); the extended-protocol caller writes 28000 and closes.
        await self._revalidate_session()

        model_id, include_hidden, persona_id = self._resolve_model_id_and_variant(sql)
        if not model_id:
            logger.warning("Extended query: cannot resolve model_id for sql (pid=%d): %s", self._pid, sql[:200])
            return None, None, (
                (
                    "Cannot determine model for this query. "
                    "Specify model_id as a connection property, or query a known model table."
                ),
                "42703",
            )

        inferred_client_kind = _client_kind_from_sql(sql, self._looker_relations)
        # Bug-6920: when _client_kind_from_sql first identifies a Looker
        # client mid-session (after the catalogue was already loaded without
        # Looker filtering), rebuild the catalogue so non-Looker relations
        # are no longer exposed in subsequent metadata queries.
        if inferred_client_kind and not self._client_kind:
            self._client_kind = inferred_client_kind
            if self._client_kind in _LOOKER_CLIENT_KINDS:
                self._rebuild_catalogue_for_client_kind()
        else:
            self._client_kind = self._client_kind or inferred_client_kind
        if self._client_kind in _LOOKER_CLIENT_KINDS and not settings.LOOKER_GATEWAY_ENABLED:
            return None, None, (_LOOKER_DISABLED, "0A000")
        if self._client_kind in _LOOKER_CLIENT_KINDS and not self._tls_active:
            return None, None, (
                "Looker connections require TLS; configure SSL mode=require.",
                "08004",
            )

        unsupported = self._unsupported_generated_relation_complex_sql(sql)
        if unsupported:
            return None, None, (unsupported, "0A000")

        original_sql = sql
        security_error = self._kpi_security_error(original_sql, persona_id)
        if security_error:
            return None, None, (security_error, "42501")

        # Bug-6921: log when normalization changes the SQL so the audit trail
        # preserves what the BI tool sent vs. what the router received.
        # F-001-14: redact literals to avoid persisting PII in logs.
        sql = _normalize_bi_sql(sql)
        if sql != original_sql:
            logger.debug(
                "BI_NORMALIZE pid=%d: original=%s | normalized=%s",
                self._pid,
                _redact_sql_for_log(original_sql),
                _redact_sql_for_log(sql),
            )
        route_class = _classify_ungrouped_query(sql, self._table_columns)
        if route_class == "raw":
            sql = self._rewrite_exposed_relations(sql)
            try:
                result = await self._run_cancellable(execute_query(
                    model_id=model_id,
                    sql=sql,
                    tenant_slug=self._tenant_slug,
                    jwt_token=self._jwt_token,
                    include_hidden=include_hidden,
                    persona_id=persona_id,
                    session_vars=self._effective_session_vars(),
                    client_kind=self._client_kind,
                    force_route="raw",
                ))
                columns_meta = result.get("columns", [])
                rows_data = result.get("rows", [])
                # F-001-18 / Bug-6055: the router's $KPIs handler returns every
                # column of every KPI row regardless of the query's projection /
                # WHERE / ORDER BY / LIMIT. Shape it here exactly as the non-raw
                # branch (server.py `_is_kpi_table_query` path) and the simple
                # path do, so an unsupported predicate fails closed (Bug-5185)
                # instead of silently dumping the full unfiltered rowset. Without
                # this, the raw route — the path standard drivers take — skipped
                # the shaper entirely.
                if self._is_kpi_table_query(sql):
                    columns_meta, rows_data, kpi_err = self._shape_kpi_result(
                        sql, columns_meta, rows_data
                    )
                    if kpi_err is not None:
                        return None, None, (kpi_err, "42601")
                columns_meta = self._type_columns_from_catalogue(sql, columns_meta)
                columns_meta, rows_data = _apply_projection_aliases(
                    original_sql, columns_meta, rows_data
                )
                col_desc = _parse_columns(columns_meta)
                col_names = [c[0] for c in col_desc]
                # Bug-8108 F2: check the RAW row count BEFORE materialising the
                # normalized list — ``_normalize_jdbc_row`` is 1:1 and cannot
                # reduce the count, so there is no reason to build a second
                # full-size list only to reject it. ``rows_data`` is already
                # post-KPI-shaping / post-projection (the only transforms that
                # can change the count), so its length is the final row count.
                buffer_cap_error = self._portal_buffer_cap_error(len(rows_data))
                if buffer_cap_error is not None:
                    return None, None, buffer_cap_error
                numeric_cols = self._numeric_result_columns(sql)
                rows = [
                    _normalize_jdbc_row(row, col_names, numeric_cols)
                    for row in rows_data
                ]
                return col_desc, rows, None
            except asyncio.CancelledError:
                logger.info("Extended query cancelled by client (pid=%d)", self._pid)
                return None, None, ("Query cancelled by client request.", "57014")
            except QueryRouterError as exc:
                logger.error("Extended raw query error (pid=%d): %s", self._pid, exc)
                return None, None, (exc.detail, _router_error_sqlstate(exc))
            except (QueryByteCeilingExceeded, GatewayQueryRateLimitExceeded) as exc:
                logger.warning("Bug-7745: gateway resource limit (pid=%d): %s", self._pid, exc)
                return None, None, (str(exc), "54001")
        sql = self._inject_group_by(sql)
        sql = self._rewrite_exposed_relations(sql)
        try:
            result = await self._run_cancellable(execute_query(
                model_id=model_id,
                sql=sql,
                tenant_slug=self._tenant_slug,
                jwt_token=self._jwt_token,
                include_hidden=include_hidden,
                persona_id=persona_id,
                session_vars=self._effective_session_vars(),
                client_kind=self._client_kind,
            ))
        except asyncio.CancelledError:
            logger.info("Extended query cancelled by client (pid=%d)", self._pid)
            return None, None, ("Query cancelled by client request.", "57014")
        except QueryRouterError as exc:
            m = _UNREACHABLE_MEASURE_RE.search(exc.detail)
            if m:
                measure_name = (m.group(1) or m.group(2) or "").strip()
                if measure_name:
                    logger.error(
                        "Unreachable measure %r rejected without retry stripping (pid=%d)",
                        measure_name, self._pid,
                    )
                    return None, None, (exc.detail, _router_error_sqlstate(exc))
            logger.error("Extended query error (pid=%d): %s", self._pid, exc)
            return None, None, (exc.detail, _router_error_sqlstate(exc))
        except (QueryByteCeilingExceeded, GatewayQueryRateLimitExceeded) as exc:
            logger.warning("Bug-7745: gateway resource limit (pid=%d): %s", self._pid, exc)
            return None, None, (str(exc), "54001")
        except Exception as exc:
            logger.error("Extended query error (pid=%d): %s", self._pid, exc)
            return None, None, (str(exc), "42601")

        columns_meta = result.get("columns", [])
        rows_data = result.get("rows", [])
        # Bug-6917: audit log when a pure-measure select (route_class "source")
        # was not served by an aggregate or pocket table.  This feeds the
        # optimizer's miss-log for ROI analysis.
        if route_class == "source":
            actual_route = result.get("route_type", "source")
            if actual_route == "source":
                logger.debug(
                    "AGGREGATE_MISS pid=%d: pure-measure query fell through to "
                    "source (no aggregate/pocket matched): %s",
                    self._pid, _redact_sql_for_log(sql),
                )
        # F-001-09: shape $KPIs results by the query's projection/filter/limit.
        if self._is_kpi_table_query(sql):
            columns_meta, rows_data, kpi_err = self._shape_kpi_result(sql, columns_meta, rows_data)
            if kpi_err is not None:
                return None, None, (kpi_err, "42601")
        columns_meta = self._type_columns_from_catalogue(sql, columns_meta)
        columns_meta, rows_data = _apply_projection_aliases(original_sql, columns_meta, rows_data)
        col_desc = _parse_columns(columns_meta)
        col_names = [c[0] for c in col_desc]
        # Bug-8108 F2: cap on the RAW row count before building the normalized
        # list (``_normalize_jdbc_row`` is 1:1; ``rows_data`` is already
        # post-shaping/projection, so its length is final). Fail closed with
        # SQLSTATE 54000 rather than buffer an unbounded result set.
        buffer_cap_error = self._portal_buffer_cap_error(len(rows_data))
        if buffer_cap_error is not None:
            return None, None, buffer_cap_error
        numeric_cols = self._numeric_result_columns(sql)
        rows = [
            _normalize_jdbc_row(row, col_names, numeric_cols) for row in rows_data
        ]
        return col_desc, rows, None

    async def _log_sql(self, sql: str, source: str = "Q") -> None:
        # F-001-14: the raw statement can carry WHERE-clause literals that are
        # personal data under row-security regimes. Emit the full text only at
        # DEBUG; at INFO log a length-bounded preview with literals stripped so
        # production logs never persist user data verbatim.
        logger.debug("SQL [%s] pid=%d: %s", source, self._pid, sql)
        logger.info("SQL [%s] pid=%d: %s", source, self._pid, _redact_sql_for_log(sql))

    async def _handle_query(self, sql: str, writer: asyncio.StreamWriter) -> None:
        if not sql.strip():
            writer.write(proto.empty_query_response())
            writer.write(proto.ready_for_query())
            await writer.drain()
            return

        # Bug-7591: detect multi-statement input and reject explicitly rather
        # than silently processing only the first statement.
        try:
            parsed_stmts = sqlglot.parse(sql.strip().rstrip(";"), read="postgres")
            if parsed_stmts and len([s for s in parsed_stmts if s is not None]) > 1:
                writer.write(proto.error_response(
                    "Multi-statement queries are not supported by the Tessallite "
                    "gateway. Submit each statement individually.",
                    severity="ERROR",
                    code="0A000",
                ))
                writer.write(proto.ready_for_query())
                await writer.drain()
                return
        except Exception:
            pass  # parse failure is OK; let the normal path handle it

        # Handle driver housekeeping before catalogue to avoid misrouting
        # (e.g. SET session_user triggers _CATALOGUE_RE via \bsession_user\b).
        # Bug-6592 F4: normalise a trailing ``;`` so ``COMMIT;``/``DISCARD;``
        # route to the housekeeping handler instead of the catalogue path.
        stripped_upper = _strip_one_terminator(sql).upper()
        if (
            stripped_upper.startswith("SET ")
            or stripped_upper.startswith("SHOW ")
            or stripped_upper.startswith("DEALLOCATE")
            or stripped_upper.startswith("DISCARD")
            or stripped_upper.startswith("RESET ")
            or stripped_upper == "RESET"
            or stripped_upper in ("BEGIN", "COMMIT", "ROLLBACK")
            or stripped_upper.startswith("SAVEPOINT ")
        ):
            await self._handle_user_query(sql, writer)
            return

        if self._catalogue is not None:
            # Bug-7043: refresh the catalogue if CLS TTL has expired so
            # tightened column restrictions are reflected immediately —
            # but ONLY for statements the catalogue will actually serve.
            # Classifying first (SOL-LAT-001) keeps Bug-7043's immediate CLS
            # revalidation for real catalogue queries while removing the full
            # tenant-metadata reload from the ordinary model-query hot path.
            if self._catalogue.references_catalogue(sql):
                await self._refresh_catalogue_if_stale()
            try:
                # F-001-16: the catalogue is a synchronous in-memory SQLite
                # engine. Offload it to a worker thread so a DBeaver metadata
                # flood on one connection cannot block the single asyncio event
                # loop (and thus every other connection). The SQLite connection
                # is opened with check_same_thread=False for this.
                result = await asyncio.to_thread(self._catalogue.execute, sql)
            except CatalogueQueryError as exc:
                writer.write(proto.error_response(
                    f"Catalogue query failed: {exc}", code="42601"))
                writer.write(proto.ready_for_query())
                await writer.drain()
                return
            if result is not None:
                columns, rows = result
                writer.write(proto.row_description(columns))
                for row in rows:
                    writer.write(proto.data_row(row))
                writer.write(proto.command_complete(f"SELECT {len(rows)}"))
                writer.write(proto.ready_for_query())
                await writer.drain()
                return

        await self._handle_user_query(sql, writer)

    # ------------------------------------------------------------------
    # SHOW command handling (extended query protocol)
    # ------------------------------------------------------------------

    # pgJDBC and Looker Studio issue SHOW during connection setup.
    # Returning NoData causes the driver to drop the connection.
    _SHOW_VARS: dict[str, tuple[str, str]] = {
        "TRANSACTION ISOLATION LEVEL": ("transaction_isolation", "read committed"),
        "TRANSACTION_ISOLATION": ("transaction_isolation", "read committed"),
        "SERVER_VERSION": ("server_version", "15.0"),
        "SERVER_ENCODING": ("server_encoding", "UTF8"),
        "CLIENT_ENCODING": ("client_encoding", "UTF8"),
        "STANDARD_CONFORMING_STRINGS": ("standard_conforming_strings", "on"),
        "INTEGER_DATETIMES": ("integer_datetimes", "on"),
        "DATESTYLE": ("DateStyle", "ISO, MDY"),
        "INTERVALSTYLE": ("IntervalStyle", "postgres"),
        "IS_SUPERUSER": ("is_superuser", "off"),
        "SESSION_AUTHORIZATION": ("session_authorization", "tessallite"),
        "TIMEZONE": ("TimeZone", "UTC"),
        "EXTRA_FLOAT_DIGITS": ("extra_float_digits", "3"),
        "MAX_IDENTIFIER_LENGTH": ("max_identifier_length", "63"),
    }

    def _handle_show_extended(
        self, stripped_upper: str,
    ) -> tuple[list | None, list | None, tuple[str, str] | None]:
        """Return a synthetic result row for SHOW <variable>."""
        var_name = stripped_upper.removeprefix("SHOW ").strip().rstrip(";").strip()
        entry = self._SHOW_VARS.get(var_name)
        if entry is None:
            # Unknown SHOW variable — return a generic empty-string value
            col_label = var_name.lower().replace(" ", "_")
            return [(col_label, proto.OID_TEXT)], [[""]], None
        col_label, value = entry
        return [(col_label, proto.OID_TEXT)], [[value]], None

    # Bug-6058: an optional SESSION / LOCAL qualifier may sit between ``SET``
    # and the ``app.*`` variable (``SET SESSION app.region = 'EMEA'`` /
    # ``SET LOCAL app.region = 'EMEA'``). The gateway acknowledges all three
    # with the same ``SET`` tag — so all three must be captured, otherwise a
    # parameterised filter set with the qualified form is silently lost.
    #
    # Bug-6592: the qualifier is now a CAPTURING group (group 1) so
    # ``_capture_session_var`` can route ``LOCAL`` into its own
    # transaction-scoped dict instead of sharing ``_session_vars``' connection
    # lifetime. ``group(2)`` (name) and ``group(3)`` (value) are unchanged in
    # meaning, only renumbered.
    _SET_APP_RE = re.compile(
        r"^SET\s+(?:(SESSION|LOCAL)\s+)?app\.(\w+)\s*(?:=|TO)\s*(.+)$",
        re.IGNORECASE,
    )

    def _capture_session_var(self, sql: str) -> None:
        m = self._SET_APP_RE.match(sql)
        if m:
            qualifier = (m.group(1) or "").upper()
            key = f"app.{m.group(2).lower()}"
            # Bug-1094: psql (and most drivers) terminate the statement with
            # a semicolon, so the value group can arrive as ``'RETAIL';``.
            # Strip trailing whitespace and any statement terminator BEFORE
            # unwrapping the quotes, otherwise the ``;`` is folded into the
            # captured value and no parameter ever matches it.
            raw = m.group(3).strip().rstrip(";").strip()
            value = self._unquote_set_value(raw)
            # Bug-6592: SET LOCAL is transaction-scoped — capture it
            # separately so COMMIT/ROLLBACK/DISCARD/RESET can clear it
            # without disturbing SET/SET SESSION's connection-lifetime value.
            if qualifier == "LOCAL":
                self._session_vars_local[key] = value
            else:
                self._session_vars[key] = value

    def _effective_session_vars(self) -> dict[str, str] | None:
        """Merge connection-lifetime and transaction-local ``SET`` captures.

        Bug-6592: ``SET LOCAL`` values live in ``_session_vars_local`` and
        must still reach the query-router as the currently-in-effect value —
        they win over a same-named ``SET``/``SET SESSION`` entry because LOCAL
        is the narrower, more specific scope. Returns ``None`` (not an empty
        dict) when nothing is set, matching every call site's prior
        ``self._session_vars or None`` shape.
        """
        if not self._session_vars_local:
            return self._session_vars or None
        if not self._session_vars:
            return dict(self._session_vars_local)
        return {**self._session_vars, **self._session_vars_local}

    @staticmethod
    def _unquote_set_value(raw: str) -> str:
        """Unwrap a quoted ``SET`` value the way the SET syntax intends.

        Bug-6416: the previous ``raw.strip("'\\"")`` stripped *every* leading
        and trailing quote character of either kind and never un-escaped an
        embedded quote, so a value like ``'it''s'`` became ``it''s`` and
        ``''x''`` lost two characters at each end. Strip exactly one matching
        outer quote pair and collapse SQL-doubled quotes (``''`` -> ``'``) back
        to a single character. An unquoted value is returned unchanged.
        """
        if len(raw) >= 2 and raw[0] in ("'", '"') and raw[-1] == raw[0]:
            quote = raw[0]
            return raw[1:-1].replace(quote + quote, quote)
        return raw

    def _try_constant_select(self, sql: str) -> tuple[list, list] | None:
        """Handle a FROM-less SELECT of literal columns locally (``SELECT 1``).

        F-001-05: only an explicit list of integer / string / NULL literals is
        answered here — each projection becomes its own typed column and any
        ``AS`` alias is honoured (``SELECT 1 AS one`` → column ``one``;
        ``SELECT 1, 2`` → two columns). Expressions the gateway cannot evaluate
        as literals (``1+1``, ``now()``) are delegated to the catalogue SQLite
        engine via ``_eval_constant_select`` rather than echoed as raw text.

        Returns (col_desc, rows) when answered locally, or None to forward /
        delegate.
        """
        stripped = sql.strip().rstrip(";").strip()
        if not re.match(r"(?i)^SELECT\s", stripped):
            return None
        # Primary: sqlglot AST check (catches SELECT 'FROM' AS col correctly)
        is_constant = _is_constant_select_sqlglot(stripped)
        if not is_constant:
            # Fallback: regex — catches cases where sqlglot parse also fails
            if re.search(r"\bFROM\b", stripped, re.IGNORECASE):
                return None

        try:
            parsed = sqlglot.parse_one(stripped, read="postgres")
        except Exception:
            parsed = None
        if isinstance(parsed, exp.Select) and parsed.expressions:
            cols: list[tuple[str, int]] = []
            row: list[str | None] = []
            for proj in parsed.expressions:
                lit = self._literal_projection(proj)
                if lit is None:
                    return None  # not a pure-literal projection → delegate
                name, oid, value = lit
                cols.append((name, oid))
                row.append(value)
            return cols, [row]
        return None

    @staticmethod
    def _literal_projection(proj) -> tuple[str, int, str | None] | None:
        """Return (col_name, oid, text_value) for a literal projection, else None.

        A bare literal column is named ``?column?`` (PostgreSQL behaviour); an
        explicit ``AS`` alias is honoured.
        """
        if isinstance(proj, exp.Alias):
            name = proj.alias
            target = proj.this
        else:
            name = "?column?"
            target = proj
        # Negative numbers parse as Neg(Literal).
        if isinstance(target, exp.Neg) and isinstance(target.this, exp.Literal) and target.this.is_number:
            return name, 23, "-" + target.this.this
        if isinstance(target, exp.Literal):
            if target.is_string:
                return name, 25, target.this
            return name, 23, target.this
        if isinstance(target, exp.Null):
            return name, 25, None
        if isinstance(target, exp.Boolean):
            return name, 16, "true" if target.this else "false"
        return None

    def _eval_constant_select(self, sql: str) -> tuple[list, list] | None:
        """Evaluate a FROM-less SELECT through the catalogue SQLite engine.

        F-001-05: expressions like ``SELECT 1+1`` or ``SELECT now()`` are real
        computations, not data. The read-only catalogue SQLite connection (with
        ``now``/``version``/``current_database`` registered) can evaluate them,
        so we forward there instead of echoing the expression text. Returns
        None when no catalogue is available or evaluation fails (the caller
        then raises a clear unsupported error rather than fabricating a value).
        """
        if self._catalogue is None:
            return None
        return self._catalogue.evaluate_constant_select(sql)

    def _resolve_model_id_and_variant(
        self, sql: str
    ) -> tuple[Optional[str], bool, Optional[str]]:
        """Return ``(model_id, include_hidden, persona_id)`` for a user query.

        Primary path: sqlglot AST walks all Table nodes — handles CTEs,
        subqueries, and LATERAL joins correctly.
        Fallback: regex extraction — handles malformed SQL or dialect
        mismatches that sqlglot cannot parse.

        Phase 8 persona-as-catalog: each table is a ``<slug>`` base or a
        ``<slug>_<persona.slug>`` sibling. Generated LookML relations are
        exposed as ``<slug>__<table>`` technical adapters. Their metadata
        was captured by ``fetch_model_metadata`` at connection time. The
        second element is ``True`` when a persona or adapter requires hidden
        columns so the router skips the visibility cascade. The third is the
        persona_id (``None`` for the business base) forwarded to the router
        for allow-list enforcement.
        """
        # Primary: sqlglot AST — correctly handles CTEs, subqueries, LATERAL
        table_names = self._extract_tables_via_sqlglot(sql)
        if not table_names:
            # Fallback: regex — catches cases sqlglot cannot parse
            table_names = self._extract_tables_via_regex(sql)

        for table_name in table_names:
            if table_name.lower() in ("public",):
                continue
            # F-001-04: relation keys carry mixed case (e.g. ``modelx$KPIs``)
            # while unquoted identifiers were already PG-folded to lowercase by
            # the extractor. Try the exact key first, then a case-insensitive
            # match so ``modelx$KPIs`` resolves whether typed quoted or not.
            resolved_key = self._match_relation_key(table_name)
            if resolved_key is None:
                # Named Query references: ``FROM @name`` extracts as ``name``
                # (the @ sits on a Parameter node in the AST), so retry with
                # the @ prefix against the @-keyed named-query relations the
                # catalogue registers for deployed models.
                resolved_key = self._match_relation_key(f"@{table_name}")
            if resolved_key is None:
                continue
            return (
                self._table_model_id[resolved_key],
                bool(self._table_include_hidden.get(resolved_key, False)),
                self._table_persona_id.get(resolved_key),
            )
        if self._model_id:
            return self._model_id, False, None
        return None, False, None

    def _match_relation_key(self, table_name: str) -> Optional[str]:
        """Resolve *table_name* to a known relation key (exact, then case-fold)."""
        if table_name in self._table_model_id:
            return table_name
        lowered = table_name.lower()
        if lowered in self._table_model_id:
            return lowered
        for key in self._table_model_id:
            if key.lower() == lowered:
                return key
        return None

    def _column_type_map(self, sql: str) -> dict[str, str]:
        """Map result-column names → catalogue data_type for the relation in *sql*.

        Result columns from the query-router are semantic names that match the
        catalogue column ``name`` captured at connection time. We union the
        column dicts of every relation referenced by the SQL so a single
        case-insensitive lookup can type any projected column. Returns an empty
        map when no relation resolves (constant/catalogue queries).
        """
        type_map: dict[str, str] = {}
        table_names = self._extract_tables_via_sqlglot(sql)
        if not table_names:
            table_names = self._extract_tables_via_regex(sql)
        for table_name in table_names:
            for cols in (
                self._table_columns.get(table_name),
                self._table_columns.get(table_name.lower()),
                # Named Query relations are @-keyed while the extractor
                # returns the bare name (see _resolve_model_id_and_variant).
                self._table_columns.get(f"@{table_name}"),
                self._table_columns.get(f"@{table_name.lower()}"),
            ):
                for col in cols or []:
                    name = col.get("name")
                    if name:
                        type_map[str(name).lower()] = str(
                            col.get("data_type") or "text"
                        )
        return type_map

    def _numeric_result_columns(self, sql: str) -> set[str]:
        """Lowercased result-column names whose CATALOGUE type is numeric.

        Bug-6054 (F-001-17): drives per-column value normalisation. This uses
        the true catalogue type (via ``_column_type_map``) rather than the wire
        OID, so a computed aggregate downgraded to a TEXT wire OID by Bug-5186
        is still recognised as numeric for VALUE reshaping (preserving the
        Bug-5383 aggregate-formatting fix) while genuine TEXT columns are not.
        """
        return {
            name for name, type_str in self._column_type_map(sql).items()
            if _is_numeric_catalogue_type(type_str)
        }

    def _type_columns_from_catalogue(
        self, sql: str, columns_meta: list
    ) -> list[dict]:
        """Promote name-only router columns to ``{name, type}`` dicts.

        F-001-02: the query-router emits column *names* only. We attach the
        real catalogue ``data_type`` for each result column so the JDBC
        RowDescription advertises numeric / int / date OIDs instead of TEXT —
        clients (DBeaver grids, pandas, Tableau) then sort and aggregate
        correctly. Columns absent from the catalogue (computed aliases) stay
        text. The router already typing a column (dict form) is preserved.

        Bug-5186: a computed/expression output column (e.g.
        ``SUM(revenue) AS revenue``) must NOT inherit the catalogue type of a
        same-named model column. Only bare column references are typed from
        the catalogue; computed projections stay text.
        """
        type_map = self._column_type_map(sql)
        computed_aliases = self._computed_projection_aliases(sql)
        typed: list[dict] = []
        for col in columns_meta:
            if isinstance(col, dict):
                typed.append(col)
                continue
            name = str(col)
            # Bug-5186: if this result column comes from a computed expression
            # (aggregate, function, arithmetic) aliased to a name that happens
            # to match a catalogue column, do NOT inherit the catalogue type.
            if name.lower() in computed_aliases:
                typed.append({"name": name, "type": "text"})
            else:
                typed.append({"name": name, "type": type_map.get(name.lower(), "text")})
        return typed

    @staticmethod
    def _computed_projection_aliases(sql: str) -> set[str]:
        """Return lowercased names of projections that are computed expressions.

        Bug-5186: a projection is "computed" when its output name comes from an
        Alias wrapping a non-Column expression (aggregate, function, arithmetic,
        CASE, etc.). A bare ``Column`` reference or a ``Column AS alias`` is NOT
        computed — those correctly map to the catalogue type. Star projections
        are also non-computed.
        """
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
        except Exception:
            return set()
        if not isinstance(parsed, exp.Select):
            return set()
        computed: set[str] = set()
        for proj in parsed.expressions:
            if isinstance(proj, exp.Star):
                continue
            if isinstance(proj, exp.Column):
                # Bare column reference — not computed.
                continue
            if isinstance(proj, exp.Alias):
                inner = proj.this
                if isinstance(inner, exp.Column):
                    # ``column AS alias`` — the alias is just a rename of a
                    # direct column, so its catalogue type is still correct.
                    continue
                # The alias wraps a computed expression (SUM, CASE, etc.).
                computed.add(proj.alias.lower())
            # An unaliased non-Column, non-Star expression (e.g. ``1+1``)
            # would get a generated name from the router; it won't match any
            # catalogue column in practice, but we skip it safely.
        return computed

    def _is_kpi_table_query(self, sql: str) -> bool:
        """True when *sql* targets a ``<model>$KPIs`` virtual table."""
        for name in self._extract_tables_via_sqlglot(sql) or self._extract_tables_via_regex(sql):
            if name.lower().endswith("$kpis"):
                return True
        return False

    def _kpi_security_error(self, sql: str, persona_id: str | None) -> str | None:
        """Fail closed when cached KPI reads cannot enforce request security.

        Enforcement boundary (Bug-6930):
        - ``session_vars``: JDBC ``SET app.<name>=...`` parameterised filters
          cannot be honoured against pre-aggregated ``kpi_latest`` rows, so any
          active session variable rejects a ``$KPIs`` read here (fail closed).
        - ``persona_id``: this is the RESOLVED relation persona. F-008-05: the
          ``$KPIs`` virtual table is now registered for the BASE surface AND per
          persona variant (``<slug>_<persona>$KPIs``), so a query against a
          persona-variant scorecard carries that persona's id here (the base
          ``<slug>$KPIs`` still resolves ``None``). Persona measure-visibility
          and column-level security are enforced by the query-router (the
          ``/execute`` route resolves the effective persona — the requested
          relation persona for an admin impersonation, or the caller's
          embed-locked/assigned persona from the JWT — and
          ``_handle_kpi_table_query`` gates on measure lineage + CLS). This
          argument is retained for the defence-in-depth case; it is no longer
          "always None".
        - ROW-LEVEL security (principal row predicates) is NOT detectable at the
          gateway: it is neither a relation persona nor a session variable. The
          Bug-6930 root-cause fix lives in the query-router
          (``_handle_kpi_table_query``): it compiles the JWT principal's row
          security and WITHHOLDS every KPI row when any active rule applies (no
          authorised persona bypass), because ``kpi_latest`` is pre-aggregated
          global data. The gateway forwards the JWT to ``/execute``, which
          resolves that principal — so a row-restricted BASE user now gets an
          empty scorecard, not unrestricted totals. This session-variable branch
          remains the gateway's own defence for JDBC ``SET app.*`` filters, which
          the router's withhold does not cover.
        """
        if not self._is_kpi_table_query(sql):
            return None
        # Bug-6592: a transaction-local SET LOCAL app.* filter is just as
        # much an active session-variable context as a connection-lifetime
        # SET — check both dicts, not only the connection-lifetime one.
        if persona_id is None and not self._session_vars and not self._session_vars_local:
            return None
        return (
            "$KPIs cannot be queried through JDBC for secured persona or "
            "session-variable contexts because cached KPI rows cannot enforce "
            "row-level security at read time."
        )

    def _shape_kpi_result(
        self, sql: str, columns_meta: list, rows_data: list
    ) -> tuple[list, list, str | None]:
        """Apply the user's projection / WHERE / ORDER BY / LIMIT to $KPIs rows.

        F-001-09: the router's ``$KPIs`` handler returns every column of every
        KPI row regardless of the query's SELECT list, WHERE, ORDER BY, or
        LIMIT. The gateway already holds the parsed SQL and the full rowset, so
        it shapes the result here (the row count is tiny).

        Bug-5185: an unsupported/unparseable WHERE predicate must NOT silently
        return the full unfiltered rowset. Returns ``(cols, rows, error)`` where
        *error* is a human-readable message when the predicate cannot be
        evaluated — callers must surface this as an ErrorResponse.
        """
        all_names = [c if not isinstance(c, dict) else c.get("name") for c in columns_meta]
        # Normalise rows to dicts keyed by column name for evaluation.
        dict_rows: list[dict] = []
        for row in rows_data:
            if isinstance(row, dict):
                dict_rows.append(dict(row))
            else:
                dict_rows.append({all_names[i]: v for i, v in enumerate(row) if i < len(all_names)})
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
        except Exception:
            # Bug-5185 finding [1]: fail closed — never return the full
            # unfiltered rowset when we cannot parse the query.
            return columns_meta, [], (
                "Could not parse $KPIs query for filtering; "
                "simplify the WHERE clause or remove it."
            )
        if not isinstance(parsed, exp.Select):
            return columns_meta, rows_data, None

        known = {str(n).lower(): n for n in all_names if n is not None}

        # WHERE — only simple AND-combined ``column <op> literal`` predicates.
        # Bug-5185: an unsupported predicate returns an error instead of
        # silently dumping the full unfiltered rowset.
        where = parsed.args.get("where")
        if where is not None:
            filtered = self._filter_kpi_rows(dict_rows, where.this, known)
            if filtered is None:
                return columns_meta, [], (
                    "Unsupported WHERE predicate on $KPIs virtual table. "
                    "Only simple column comparisons (=, !=, <, >, <=, >=) "
                    "combined with AND/OR are supported."
                )
            dict_rows = filtered

        # ORDER BY — single/multi column over known columns.
        order = parsed.args.get("order")
        if order is not None:
            ordered = self._order_kpi_rows(dict_rows, order, known)
            if ordered is not None:
                dict_rows = ordered

        # Projection — explicit column list (``*`` keeps all columns).
        out_columns = columns_meta
        proj_names: list | None = None
        if parsed.expressions and not any(isinstance(p, exp.Star) for p in parsed.expressions):
            proj: list = []
            ok = True
            for p in parsed.expressions:
                col = p if isinstance(p, exp.Column) else (
                    p.this if isinstance(p, exp.Alias) and isinstance(p.this, exp.Column) else None
                )
                if col is None or col.name.lower() not in known:
                    ok = False
                    break
                proj.append(known[col.name.lower()])
            if ok and proj:
                proj_names = proj
                by_name = {
                    (c.get("name") if isinstance(c, dict) else c): c for c in columns_meta
                }
                out_columns = [by_name[n] for n in proj if n in by_name]

        # LIMIT.
        limit = parsed.args.get("limit")
        if limit is not None:
            try:
                n = int(limit.expression.this) if limit.expression else None
                if n is not None and n >= 0:
                    dict_rows = dict_rows[:n]
            except (ValueError, AttributeError, TypeError):
                pass

        keep = proj_names if proj_names is not None else all_names
        shaped_rows = [[r.get(name) for name in keep] for r in dict_rows]
        return out_columns, shaped_rows, None

    @staticmethod
    def _kpi_literal_value(node) -> object:
        if isinstance(node, exp.Literal):
            if node.is_string:
                return node.this
            try:
                return float(node.this)
            except ValueError:
                return node.this
        if isinstance(node, exp.Null):
            return None
        if isinstance(node, exp.Boolean):
            return node.this
        return _UNSUPPORTED

    def _filter_kpi_rows(self, rows: list[dict], cond, known: dict) -> list[dict] | None:
        """Evaluate a simple boolean predicate tree; None if unsupported."""
        evaluator = self._compile_kpi_predicate(cond, known)
        if evaluator is None:
            return None
        return [r for r in rows if evaluator(r)]

    def _compile_kpi_predicate(self, cond, known: dict):
        if isinstance(cond, exp.And):
            left = self._compile_kpi_predicate(cond.left, known)
            right = self._compile_kpi_predicate(cond.right, known)
            if left is None or right is None:
                return None
            return lambda r: left(r) and right(r)
        if isinstance(cond, exp.Or):
            left = self._compile_kpi_predicate(cond.left, known)
            right = self._compile_kpi_predicate(cond.right, known)
            if left is None or right is None:
                return None
            return lambda r: left(r) or right(r)
        if isinstance(cond, exp.Paren):
            return self._compile_kpi_predicate(cond.this, known)

        comparators = {
            exp.EQ: lambda a, b: a == b,
            exp.NEQ: lambda a, b: a != b,
            exp.GT: lambda a, b: a is not None and b is not None and a > b,
            exp.GTE: lambda a, b: a is not None and b is not None and a >= b,
            exp.LT: lambda a, b: a is not None and b is not None and a < b,
            exp.LTE: lambda a, b: a is not None and b is not None and a <= b,
        }
        op = comparators.get(type(cond))
        if op is None:
            return None
        left, right = cond.left, cond.right
        if not isinstance(left, exp.Column) or left.name.lower() not in known:
            return None
        literal = self._kpi_literal_value(right)
        if literal is _UNSUPPORTED:
            return None
        col_key = known[left.name.lower()]

        def _eval(r: dict) -> bool:
            cell = r.get(col_key)
            a, b = cell, literal
            if isinstance(b, float) and cell is not None and not isinstance(cell, (int, float)):
                try:
                    a = float(cell)
                except (ValueError, TypeError):
                    return False
            try:
                return bool(op(a, b))
            except TypeError:
                return False

        return _eval

    def _order_kpi_rows(self, rows: list[dict], order, known: dict) -> list[dict] | None:
        keys: list[tuple[str, bool]] = []
        for ordered in order.expressions:
            target = ordered.this
            if not isinstance(target, exp.Column) or target.name.lower() not in known:
                return None
            keys.append((known[target.name.lower()], bool(ordered.args.get("desc"))))
        result = list(rows)
        for col_key, desc in reversed(keys):
            result.sort(
                key=lambda r: (r.get(col_key) is None, r.get(col_key)),
                reverse=desc,
            )
        return result

    def _describe_columns_metadata_only(
        self, sql: str
    ) -> list[tuple[str, int]] | None:
        """Resolve RowDescription columns without executing (F-001-01).

        Used for a statement-level Describe that arrives before Bind: we must
        never run a NULL-substituted probe query. We can only answer when the
        result shape is unambiguous from the SQL alone — an explicit projection
        (no ``*``, no expressions) over a single catalogue relation where every
        projected column has a known type. Anything else returns ``None`` so the
        caller advertises NoData and the single Execute run becomes the
        authoritative RowDescription. This guarantees the executed shape always
        matches the advertised shape.
        """
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
        except Exception:
            return None
        if parsed is None or not isinstance(parsed, exp.Select):
            return None
        # Single base relation only; joins/subqueries/CTEs make the projection
        # ambiguous to type from catalogue alone.
        tables = [t for t in parsed.find_all(exp.Table)]
        if len(tables) != 1:
            return None
        if parsed.args.get("joins") or parsed.find(exp.Subquery) or parsed.find(exp.With):
            return None
        type_map = self._column_type_map(sql)
        if not type_map:
            return None
        projections = parsed.expressions
        if not projections:
            return None
        resolved: list[tuple[str, int]] = []
        for proj in projections:
            # Reject star and any non-trivial expression.
            if isinstance(proj, exp.Star):
                return None
            col = proj if isinstance(proj, exp.Column) else None
            if col is None and isinstance(proj, exp.Alias) and isinstance(proj.this, exp.Column):
                col = proj.this
            if col is None:
                return None
            name = col.name
            type_str = type_map.get(name.lower())
            if type_str is None:
                return None
            out_name = proj.alias_or_name or name
            resolved.append((out_name, _map_type_oid(type_str)))
        return resolved

    def _unsupported_generated_relation_complex_sql(self, sql: str) -> str | None:
        """Reject complex SQL before generated relations collapse to one model name."""
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
        except Exception:
            return None
        if parsed is None:
            return None
        has_window = parsed.find(exp.Window) is not None
        is_complex = (
            has_window
            or parsed.find(exp.With) is not None
            or parsed.find(exp.Subquery) is not None
        )
        if not is_complex:
            return None
        adapter_relations = {
            table.name.lower()
            for table in parsed.find_all(exp.Table)
            if "__" in table.name and table.name.lower() in self._table_query_name
        }
        if len(adapter_relations) <= 1:
            return None
        if has_window:
            return _LOOKER_WINDOW_UNSUPPORTED
        return _LOOKER_COMPLEX_MULTI_RELATION_UNSUPPORTED

    @staticmethod
    def _extract_tables_via_sqlglot(sql: str) -> list[str]:
        """Extract table names using sqlglot AST.

        F-001-04: PostgreSQL folds *unquoted* identifiers to lowercase and
        treats *quoted* ones case-sensitively. Relation keys are lowercase
        slugs, so unquoted names are lowercased here (``FROM MODELX`` →
        ``modelx``) while quoted names keep their exact case. This mirrors the
        folding already done in ``_rewrite_exposed_relations`` and
        ``_client_kind_from_sql``.
        """
        try:
            parsed = sqlglot.parse(sql, read="postgres")
            if not parsed or parsed[0] is None:
                return []
            names: list[str] = []
            for t in parsed[0].find_all(exp.Table):
                if not t.name:
                    continue
                quoted = bool(t.this.args.get("quoted")) if t.this else False
                names.append(t.name if quoted else t.name.lower())
            return names
        except Exception:
            logger.warning("[JDBC] sqlglot parse failed, falling back to regex: %s", sql[:200])
            return []

    @staticmethod
    def _extract_tables_via_regex(sql: str) -> list[str]:
        """Regex fallback for table extraction."""
        matches = _TABLE_NAME_REGEX.findall(sql)
        return [
            m[0] or m[1] or m[2] or m[3]
            for m in matches
            if m[0] or m[1] or m[2] or m[3]
        ]

    def _inject_group_by(self, sql: str) -> str:
        """Add GROUP BY for dimension columns when a query has measures but no GROUP BY.

        BI tools send ``SELECT dim1, dim2, measure1 FROM model`` without GROUP BY.
        The query-router requires GROUP BY on all dimensions when measures are
        present. This method inspects the model metadata to determine which
        selected columns are dimensions and adds the GROUP BY clause.

        Bug-6932: the flat ``SELECT dim, measure`` shape is now classified
        ``"raw"`` by ``_classify_ungrouped_query`` (Bug-5879) and diverted
        before this method runs. The measure-to-GROUP-BY branch remains
        reachable for the mixed-aggregate edge case:
        ``SELECT dim, measure_col, SUM(x) FROM model`` -- the classifier
        returns ``None`` (mixed explicit-agg + column), so the normal flow
        calls this method, which injects ``GROUP BY dim``. This is the
        only shape where ``has_measure`` is True.
        """
        try:
            parsed = sqlglot.parse(sql, read="postgres")
            if not parsed or not isinstance(parsed[0], exp.Select):
                return sql
            stmt = parsed[0]
            if stmt.find(exp.Group):
                return sql
            tables = list(stmt.find_all(exp.Table))
            if not tables:
                return sql
            # Bug-5879: injection is only valid for the flat dim/measure
            # semantic shape. Complex queries (subqueries, CTEs, window
            # functions, DISTINCT) take the router's passthrough path where
            # an injected GROUP BY reaches the source database physically
            # and produces "column must appear in the GROUP BY clause".
            if any(s is not stmt for s in stmt.find_all(exp.Select)):
                return sql
            if stmt.find(exp.Window):
                return sql
            if stmt.args.get("distinct"):
                return sql
            table_name = tables[0].name
            cols_meta = (
                self._table_columns.get(table_name)
                or self._table_columns.get(table_name.lower())
            )
            if not cols_meta:
                return sql
            measure_names = {
                c["name"].lower() for c in cols_meta if c.get("kind") == "measure"
            }
            if not measure_names:
                return sql
            selected_names = []
            for col_expr in stmt.expressions:
                inner = (
                    col_expr.this
                    if isinstance(col_expr, exp.Alias)
                    else col_expr
                )
                if isinstance(inner, exp.Column):
                    selected_names.append(inner.name.lower())
                elif not isinstance(inner, (exp.Sum, exp.Avg, exp.Count,
                                            exp.Min, exp.Max)):
                    # A non-column, non-aggregate select item (expression,
                    # function call, literal) — grouping only the plain
                    # columns around it is not semantics-preserving.
                    return sql
            has_measure = any(n in measure_names for n in selected_names)
            if not has_measure:
                return sql
            dim_cols = [n for n in selected_names if n not in measure_names]
            if not dim_cols:
                return sql
            group_exprs = [
                exp.Column(this=exp.to_identifier(d, quoted=True))
                for d in dim_cols
            ]
            stmt.set("group", exp.Group(expressions=group_exprs))
            return stmt.sql(dialect="postgres")
        except Exception:
            return sql

    def _rewrite_exposed_relations(self, sql: str) -> str:
        """Replace adapter/persona relation names with the canonical model slug.

        When a client queries ``modelx_analyst`` or a generated LookML view
        backed by ``modelx__payment_transaction``, the query router still
        resolves the deployed canonical model ``modelx``. Handles quoted and
        unquoted schema-qualified forms without altering string literals.
        """
        replacements = {
            table_name.lower(): canonical
            for table_name, canonical in self._table_query_name.items()
            if table_name.lower() != canonical.lower()
        }
        if not replacements:
            return sql
        try:
            parsed = sqlglot.parse(sql, read="postgres")
            changed = False
            for statement in parsed:
                active: dict[str, str] = {}
                for table in statement.find_all(exp.Table):
                    canonical = replacements.get(table.name.lower())
                    if canonical is None:
                        continue
                    active[table.name.lower()] = canonical
                    table.set(
                        "this",
                        exp.to_identifier(
                            canonical,
                            quoted=bool(table.this.args.get("quoted")),
                        ),
                    )
                    changed = True
                for column in statement.find_all(exp.Column):
                    canonical = active.get(column.table.lower()) if column.table else None
                    if canonical is None:
                        continue
                    qualifier = column.args.get("table")
                    column.set(
                        "table",
                        exp.to_identifier(
                            canonical,
                            quoted=bool(qualifier.args.get("quoted")) if qualifier else False,
                        ),
                    )
            if changed:
                return "; ".join(statement.sql(dialect="postgres") for statement in parsed)
            return sql
        except Exception:
            logger.warning("[JDBC] table rewrite parse failed, using guarded regex: %s", sql[:200])

        # Malformed-but-recoverable SQL fallback: only mutate relation tokens
        # immediately following FROM/JOIN; do not replace arbitrary text.
        for table_name, canonical in replacements.items():
            pattern = re.compile(
                r"(?P<prefix>\b(?:from|join)\s+(?:(?:\"[^\"]+\"|\w+)\.)?)"
                rf"(?P<name>\"{re.escape(table_name)}\"|{re.escape(table_name)})"
                r"(?=\s|$|[,;)])",
                re.IGNORECASE,
            )
            sql = pattern.sub(
                lambda match: match.group("prefix")
                + (f'"{canonical}"' if match.group("name").startswith('"') else canonical),
                sql,
            )
        return sql

    async def _handle_user_query(self, sql: str, writer: asyncio.StreamWriter) -> None:
        # Bug-6592 F4: normalise a single trailing ``;`` so ``COMMIT;`` etc.
        # match the exact-string classifiers (otherwise LOCAL scope leaks).
        stripped = _strip_one_terminator(sql).upper()

        # SET — acknowledge silently.
        if stripped.startswith("SET "):
            self._capture_session_var(sql.strip())
            writer.write(proto.command_complete("SET"))
            writer.write(proto.ready_for_query())
            await writer.drain()
            return
        # SHOW — return a synthetic result row so pgJDBC/Looker Studio proceed.
        if stripped.startswith("SHOW "):
            col_desc, rows, _ = self._handle_show_extended(stripped)
            writer.write(proto.row_description(col_desc or []))
            for row in (rows or []):
                writer.write(proto.data_row(row))
            writer.write(proto.command_complete("SHOW"))
            writer.write(proto.ready_for_query())
            await writer.drain()
            return
        if stripped in ("BEGIN", "COMMIT", "ROLLBACK") or stripped.startswith("SAVEPOINT "):
            # Bug-6592 F4: SET LOCAL scope ends at transaction end. BEGIN opens
            # an explicit transaction; COMMIT/ROLLBACK close it and clear LOCAL.
            # BEGIN and SAVEPOINT clear nothing — see the matching comment in
            # ``_execute_for_extended``.
            if stripped == "BEGIN":
                self._in_explicit_txn = True
            elif stripped in ("COMMIT", "ROLLBACK"):
                self._session_vars_local.clear()
                self._in_explicit_txn = False
            writer.write(proto.command_complete(stripped.split()[0]))
            writer.write(proto.ready_for_query())
            await writer.drain()
            return
        # DEALLOCATE — acknowledge silently (gateway has no prepared statements).
        if stripped.startswith("DEALLOCATE"):
            writer.write(proto.command_complete("DEALLOCATE"))
            writer.write(proto.ready_for_query())
            await writer.drain()
            return
        # DISCARD ALL — Npgsql session reset; acknowledge and clear session
        # state.  Bug-6929 / Bug-6792 / Bug-6592: without this, SET app.<name>
        # (SESSION or LOCAL scoped) permanently poisons $KPIs reads for the
        # rest of the connection.
        if stripped.startswith("DISCARD"):
            self._session_vars.clear()
            self._session_vars_local.clear()
            # Bug-6592 F4: DISCARD ALL is a full session reset — it also aborts
            # any open explicit transaction.
            self._in_explicit_txn = False
            writer.write(proto.command_complete("DISCARD ALL"))
            writer.write(proto.ready_for_query())
            await writer.drain()
            return
        # RESET — clear a specific session variable or all of them.
        # Bug-6792 / Bug-6592: without this, SET app.<name> is irrevocable;
        # RESET clears both the SESSION and LOCAL captures for the target.
        if stripped.startswith("RESET ") or stripped == "RESET":
            target = stripped[6:].strip().rstrip(";").strip() if len(stripped) > 5 else "ALL"
            if target.upper() == "ALL":
                self._session_vars.clear()
                self._session_vars_local.clear()
            else:
                key = target.lower()
                self._session_vars.pop(key, None)
                self._session_vars_local.pop(key, None)
            writer.write(proto.command_complete("RESET"))
            writer.write(proto.ready_for_query())
            await writer.drain()
            return

        constant = self._try_constant_select(sql)
        if constant is None and _is_constant_select_sqlglot(sql.strip().rstrip(";")):
            # F-001-05: evaluate non-literal FROM-less SELECTs via SQLite.
            constant = self._eval_constant_select(sql)
            if constant is None:
                writer.write(proto.error_response(
                    "Unsupported constant expression; the gateway can evaluate "
                    "literal and simple scalar SELECTs only.",
                    severity="ERROR",
                    code="0A000",
                ))
                writer.write(proto.ready_for_query())
                await writer.drain()
                return
        if constant is not None:
            col_desc, rows = constant
            writer.write(proto.row_description(col_desc))
            for row in rows:
                writer.write(proto.data_row(row))
            writer.write(proto.command_complete(f"SELECT {len(rows)}"))
            writer.write(proto.ready_for_query())
            await writer.drain()
            return

        if not self._jwt_token:
            writer.write(proto.error_response(
                "Authentication required.", severity="ERROR", code="28000"))
            writer.write(proto.ready_for_query())
            await writer.drain()
            return

        # G2 / grok F-001-01: re-check session revocation before dispatching to
        # the router on this long-lived connection. Fail closed — on a revoked
        # session, report 28000 (FATAL) and re-raise so handle_client closes the
        # socket. Mirrors the XMLA per-request revalidation.
        try:
            await self._revalidate_session()
        except SessionRevokedError:
            writer.write(proto.error_response(
                "Session has been revoked; reconnect to continue.",
                severity="FATAL", code="28000",
            ))
            await writer.drain()
            raise

        model_id, include_hidden, persona_id = self._resolve_model_id_and_variant(sql)
        if not model_id:
            writer.write(proto.error_response(
                "Cannot determine model for this query. "
                "Specify model_id as a connection property, or query a known model table.",
                code="42703",
            ))
            writer.write(proto.ready_for_query())
            await writer.drain()
            return

        inferred_client_kind = _client_kind_from_sql(sql, self._looker_relations)
        # Bug-6920: rebuild catalogue on mid-session Looker detection (same
        # as the extended-query path).
        if inferred_client_kind and not self._client_kind:
            self._client_kind = inferred_client_kind
            if self._client_kind in _LOOKER_CLIENT_KINDS:
                self._rebuild_catalogue_for_client_kind()
        else:
            self._client_kind = self._client_kind or inferred_client_kind
        if self._client_kind in _LOOKER_CLIENT_KINDS and not settings.LOOKER_GATEWAY_ENABLED:
            writer.write(proto.error_response(_LOOKER_DISABLED, code="0A000"))
            writer.write(proto.ready_for_query())
            await writer.drain()
            return
        if self._client_kind in _LOOKER_CLIENT_KINDS and not self._tls_active:
            writer.write(
                proto.error_response(
                    "Looker connections require TLS; configure SSL mode=require.",
                    severity="ERROR",
                    code="08004",
                )
            )
            writer.write(proto.ready_for_query())
            await writer.drain()
            return

        unsupported = self._unsupported_generated_relation_complex_sql(sql)
        if unsupported:
            writer.write(proto.error_response(unsupported, code="0A000"))
            writer.write(proto.ready_for_query())
            await writer.drain()
            return

        original_sql = sql
        security_error = self._kpi_security_error(original_sql, persona_id)
        if security_error:
            writer.write(proto.error_response(security_error, code="42501"))
            writer.write(proto.ready_for_query())
            await writer.drain()
            return

        # Bug-6921: log when normalization changes the SQL.
        # F-001-14: redact literals to avoid persisting PII in logs.
        sql = _normalize_bi_sql(sql)
        if sql != original_sql:
            logger.debug(
                "BI_NORMALIZE pid=%d: original=%s | normalized=%s",
                self._pid,
                _redact_sql_for_log(original_sql),
                _redact_sql_for_log(sql),
            )
        route_class = _classify_ungrouped_query(sql, self._table_columns)
        if route_class == "raw":
            sql = self._rewrite_exposed_relations(sql)
            try:
                result = await self._run_cancellable(execute_query(
                    model_id=model_id,
                    sql=sql,
                    tenant_slug=self._tenant_slug,
                    jwt_token=self._jwt_token,
                    include_hidden=include_hidden,
                    persona_id=persona_id,
                    session_vars=self._effective_session_vars(),
                    client_kind=self._client_kind,
                    force_route="raw",
                ))
            except asyncio.CancelledError:
                logger.info("Query cancelled by client (pid=%d)", self._pid)
                writer.write(proto.error_response(
                    "Query cancelled by client request.",
                    code="57014",
                ))
                writer.write(proto.ready_for_query())
                await writer.drain()
                return
            except QueryRouterError as exc:
                logger.error("Simple raw query error (pid=%d): %s", self._pid, exc)
                writer.write(proto.error_response(exc.detail, code=_router_error_sqlstate(exc)))
                writer.write(proto.ready_for_query())
                await writer.drain()
                return
            except (QueryByteCeilingExceeded, GatewayQueryRateLimitExceeded) as exc:
                logger.warning("Bug-7745: gateway resource limit (pid=%d): %s", self._pid, exc)
                writer.write(proto.error_response(str(exc), code="54001"))
                writer.write(proto.ready_for_query())
                await writer.drain()
                return
            except Exception as exc:
                logger.error("Simple raw query error (pid=%d): %s", self._pid, exc)
                writer.write(proto.error_response(str(exc)))
                writer.write(proto.ready_for_query())
                await writer.drain()
                return
        else:
            sql = self._inject_group_by(sql)
            sql = self._rewrite_exposed_relations(sql)
            result = None
            try:
                result = await self._run_cancellable(execute_query(
                    model_id=model_id,
                    sql=sql,
                    tenant_slug=self._tenant_slug,
                    jwt_token=self._jwt_token,
                    include_hidden=include_hidden,
                    persona_id=persona_id,
                    session_vars=self._effective_session_vars(),
                    client_kind=self._client_kind,
                ))
            except asyncio.CancelledError:
                logger.info("Query cancelled by client (pid=%d)", self._pid)
                writer.write(proto.error_response(
                    "Query cancelled by client request.",
                    code="57014",
                ))
                writer.write(proto.ready_for_query())
                await writer.drain()
                return
            except QueryRouterError as exc:
                m = _UNREACHABLE_MEASURE_RE.search(exc.detail)
                if m:
                    measure_name = (m.group(1) or m.group(2) or "").strip()
                    if measure_name:
                        logger.error(
                            "Unreachable measure %r rejected without retry stripping (pid=%d)",
                            measure_name, self._pid,
                        )
                        writer.write(proto.error_response(exc.detail, code=_router_error_sqlstate(exc)))
                        writer.write(proto.ready_for_query())
                        await writer.drain()
                        return
                logger.error("Query execution error (pid=%d): %s", self._pid, exc)
                writer.write(proto.error_response(exc.detail, code=_router_error_sqlstate(exc)))
                writer.write(proto.ready_for_query())
                await writer.drain()
                return
            except (QueryByteCeilingExceeded, GatewayQueryRateLimitExceeded) as exc:
                logger.warning("Bug-7745: gateway resource limit (pid=%d): %s", self._pid, exc)
                writer.write(proto.error_response(str(exc), code="54001"))
                writer.write(proto.ready_for_query())
                await writer.drain()
                return
            except Exception as exc:
                logger.error("Query execution error (pid=%d): %s", self._pid, exc)
                writer.write(proto.error_response(str(exc)))
                writer.write(proto.ready_for_query())
                await writer.drain()
                return
        if result is None:
            writer.write(proto.error_response("Query failed after measure-strip retries"))
            writer.write(proto.ready_for_query())
            await writer.drain()
            return

        columns_meta = result.get("columns", [])
        rows_data = result.get("rows", [])
        # Bug-6917: audit log when a pure-measure select fell through to source.
        if route_class == "source":
            actual_route = result.get("route_type", "source")
            if actual_route == "source":
                logger.debug(
                    "AGGREGATE_MISS pid=%d: pure-measure query fell through to "
                    "source (no aggregate/pocket matched): %s",
                    self._pid, _redact_sql_for_log(sql),
                )
        # F-001-09: shape $KPIs results by the query's projection/filter/limit.
        if self._is_kpi_table_query(sql):
            columns_meta, rows_data, kpi_err = self._shape_kpi_result(sql, columns_meta, rows_data)
            if kpi_err is not None:
                writer.write(proto.error_response(kpi_err, code="42601"))
                writer.write(proto.ready_for_query())
                await writer.drain()
                return

        columns_meta = self._type_columns_from_catalogue(sql, columns_meta)
        columns_meta, rows_data = _apply_projection_aliases(original_sql, columns_meta, rows_data)
        col_desc = _parse_columns(columns_meta)
        writer.write(proto.row_description(col_desc))
        col_names = [c[0] for c in col_desc]
        numeric_cols = self._numeric_result_columns(sql)
        for row in rows_data:
            writer.write(proto.data_row(
                _normalize_jdbc_row(row, col_names, numeric_cols)
            ))
        writer.write(proto.command_complete(f"SELECT {len(rows_data)}"))
        writer.write(proto.ready_for_query())
        await writer.drain()


# ---------------------------------------------------------------------------
# Numeric value normalisation for JDBC wire
# ---------------------------------------------------------------------------

def _normalize_jdbc_value(v: object, is_numeric: bool = False) -> str:
    """Convert a JSON-deserialised value to a JDBC-wire text string.

    Bug-5383: asyncpg may return ``Decimal('1E+5')`` for aggregate results.
    Pydantic 2 serialises that as the JSON string ``"1.0E+5"`` (or the JSON
    number ``100000.0``).  The gateway receives the deserialised form — either
    a Python ``float`` (from a JSON number) or a ``str`` (from a JSON string
    that looks numeric).  A whole-number aggregate must render as a plain
    integer string (``"100000"``, not ``"1.0E+5"`` or ``"100000.0"``) so JDBC
    clients see the same integer the source returned.

    Bug-6054 (F-001-17): the previous implementation round-tripped EVERY value
    through ``float()`` with no type awareness. That silently corrupted data on
    two axes:
      * ``float`` has a 53-bit mantissa, so any integer above 2^53 (bigint
        surrogate keys, transaction ids) was rounded to the nearest double.
      * any TEXT value that happened to parse as a whole-number float (zip /
        product / account codes with leading zeros, ``"1e5"``-shaped strings)
        was rewritten into a different string.

    The corrected function is lossless and type-aware:
      * native ``int`` renders exactly at any magnitude (no float round-trip);
      * native ``float`` / ``Decimal`` whole numbers still normalise (Bug-5383);
      * a string value is reshaped ONLY when the result column's catalogue type
        is numeric (``is_numeric``) — a TEXT column value is returned verbatim;
      * even for a numeric column, an already-exact integer string is returned
        unchanged and scientific / trailing-``.0`` shapes are normalised through
        ``Decimal`` (exact) rather than ``float`` (lossy above 2^53).
    """
    # ``bool`` is an ``int`` subclass — keep its True/False text, never 1/0.
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return str(v)  # exact at any magnitude — no precision loss
    if isinstance(v, Decimal):
        if v.is_finite() and v == v.to_integral_value():
            return str(int(v))
        return str(v)
    if isinstance(v, float):
        if v == v and v not in (float("inf"), float("-inf")) and v.is_integer():
            return str(int(v))
        return str(v)

    s = str(v)
    # String values: only reshape numeric-typed columns (Bug-6054). A TEXT
    # column value (leading-zero codes, ``"1e5"``-shaped strings, oversized
    # numeric ids) must survive verbatim.
    if not is_numeric:
        return s
    # An exact integer string is already correct — never round-trip it (would
    # corrupt values above 2^53).
    if _INTEGER_STR_RE.match(s):
        return s
    # Scientific / trailing-``.0`` numeric shapes: normalise a whole number to a
    # plain integer string losslessly via ``Decimal``.
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return s
    if d.is_finite() and d == d.to_integral_value():
        return str(int(d))
    return s


# Catalogue data-types whose VALUES may be reshaped by _normalize_jdbc_value.
# Derived from the same vocabulary as the wire-OID map below; excludes bool /
# date / timestamp / text so only genuine numbers are ever normalised.
_NUMERIC_CATALOGUE_TYPES = frozenset({
    "smallint", "int2", "integer", "int", "int4", "bigint", "int8",
    "float", "float4", "real", "float8", "double", "double precision",
    "numeric", "decimal",
})

# A string that is already an exact integer literal (any magnitude).
_INTEGER_STR_RE = re.compile(r"^-?\d+$")


def _is_numeric_catalogue_type(type_str: str | None) -> bool:
    return bool(type_str) and str(type_str).strip().lower() in _NUMERIC_CATALOGUE_TYPES


def _normalize_jdbc_row(
    row: object, col_names: list[str], numeric_cols: set[str],
) -> list:
    """Normalise one result row (dict or sequence) for the JDBC wire.

    Each cell is passed through :func:`_normalize_jdbc_value` with the per-column
    numeric flag derived from ``numeric_cols`` (lowercased catalogue-numeric
    column names). ``None`` cells are preserved as SQL NULL.
    """
    if isinstance(row, dict):
        out: list = []
        for c in col_names:
            v = row.get(c)
            out.append(
                None if v is None
                else _normalize_jdbc_value(v, c.lower() in numeric_cols)
            )
        return out
    out = []
    for i, v in enumerate(row):
        name = col_names[i] if i < len(col_names) else ""
        out.append(
            None if v is None
            else _normalize_jdbc_value(v, name.lower() in numeric_cols)
        )
    return out


# ---------------------------------------------------------------------------
# Parameter substitution
# ---------------------------------------------------------------------------

_PARAM_RE = re.compile(r"\$(\d+)")

# F-001-14: strip string and numeric literals so the INFO-level statement log
# never persists user data (e.g. WHERE-clause values that are PII under row
# security). Single-quoted strings collapse to '?', bare numbers to ?.
_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
_NUMERIC_LITERAL_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_LOG_SQL_MAX_LEN = 500


def _redact_sql_for_log(sql: str) -> str:
    """Return a length-bounded, literal-stripped preview of *sql* for INFO logs."""
    redacted = _STRING_LITERAL_RE.sub("'?'", sql)
    redacted = _NUMERIC_LITERAL_RE.sub("?", redacted)
    redacted = " ".join(redacted.split())
    if len(redacted) > _LOG_SQL_MAX_LEN:
        redacted = redacted[:_LOG_SQL_MAX_LEN] + "…"
    return redacted


# F-001-06: a value may be inlined unquoted only when it is an
# unambiguous SQL numeric literal — strictly ``[-]digits[.digits]``. This
# rejects Python's looser forms (``0123`` leading zeros, ``1_000``
# underscores, ``+5``, surrounding whitespace, ``inf``/``nan``) that
# ``int()``/``float()`` accept but which either change the value or are a
# PostgreSQL syntax error.
_STRICT_NUMERIC_RE = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")


def _substitute_params(
    sql: str,
    params: list[str | None],
    param_oids: list[int] | None = None,
) -> str:
    """Replace $1, $2, ... placeholders with typed, safe SQL literals.

    Wave C #6: the gateway TERMINATES each bound parameter into a safe SQL literal
    at the gateway (there is no downstream connector-native bind API), and never
    guesses. ``None`` becomes ``NULL``. A value declared with a numeric OID is
    inlined raw ONLY after a strict numeric-literal check (else quoted as text so
    a malformed numeric never reaches the source as raw SQL). Every other
    supported type — text, bool, date/time/timestamp, uuid — is quoted as a
    string literal with internal quotes doubled, so an embedded quote or semicolon
    can never break out of the literal (injection-safe). A non-NULL parameter
    whose declared type OID is outside the supported set raises
    :class:`proto.ParamDecodeError` (a stable protocol error), rather than being
    mis-typed. OID 0 (unspecified) is treated as text with numeric-literal
    inference — PostgreSQL infers the type in context.

    Type fidelity (F-001-06): numeric-looking text such as a zip code declared
    VARCHAR stays quoted so PostgreSQL preserves its type.
    """
    oids = param_oids or []

    def _repl(m: re.Match) -> str:
        idx = int(m.group(1)) - 1
        if idx < 0 or idx >= len(params):
            return m.group(0)
        val = params[idx]
        if val is None:
            return "NULL"
        declared_oid = oids[idx] if idx < len(oids) else 0
        if declared_oid != 0 and declared_oid not in proto.SUPPORTED_PARAM_OIDS:
            # Unsupported parameter type: refuse rather than mis-type it. The Bind
            # handler turns this into a stable protocol ErrorResponse (#6).
            raise proto.ParamDecodeError(
                f"parameter ${idx + 1} has unsupported type OID {declared_oid}; "
                "the gateway cannot terminate it into a safe SQL literal."
            )
        if declared_oid in proto.NUMERIC_PARAM_OIDS:
            # Client declared a numeric type. Validate strictly before
            # inlining so a malformed numeric never reaches the source as
            # raw SQL; quote-as-text otherwise (let the source coerce/raise).
            if _STRICT_NUMERIC_RE.match(val):
                return val
            return "'" + val.replace("'", "''") + "'"
        if declared_oid != 0:
            # Any other supported declared type (text, date, bool, ...) → quote
            # as text; PostgreSQL coerces the literal to the target type.
            return "'" + val.replace("'", "''") + "'"
        # No declared OID: fall back to value-shape inference, but only for
        # strict numeric literals so leading zeros / underscores stay text.
        if _STRICT_NUMERIC_RE.match(val):
            return val
        return "'" + val.replace("'", "''") + "'"

    return _PARAM_RE.sub(_repl, sql)


# ---------------------------------------------------------------------------
# Column parsing — handles both list-of-dicts and list-of-strings
# ---------------------------------------------------------------------------

def _parse_columns(columns_meta: list) -> list[tuple[str, int]]:
    """
    Parse column metadata from query-router response.
    Handles both formats:
      - list of dicts: [{"name": "col", "type": "text"}, ...]
      - list of strings: ["col1", "col2", ...]
    """
    result = []
    for i, col in enumerate(columns_meta):
        if isinstance(col, dict):
            result.append((col.get("name", f"col{i}"), _map_type_oid(col.get("type", "text"))))
        else:
            result.append((str(col), proto.OID_TEXT))
    return result


# ---------------------------------------------------------------------------
# Type OID mapping
# ---------------------------------------------------------------------------

# Bug-6647: strip a ``(precision[,scale])`` qualifier from a type spelling
# (shared shape with catalogue._base_data_type so the two never drift).
_PRECISION_PAREN_RE = re.compile(r"\(\s*\d+\s*(?:,\s*\d+\s*)?\)")


def _map_type_oid(type_str: str) -> int:
    mapping = {
        "text": proto.OID_TEXT,
        "varchar": proto.OID_TEXT,
        "char": proto.OID_TEXT,
        "string": proto.OID_TEXT,
        "uuid": proto.OID_TEXT,
        "smallint": proto.OID_INT2,
        "int2": proto.OID_INT2,
        "integer": proto.OID_INT4,
        "int": proto.OID_INT4,
        "int4": proto.OID_INT4,
        "bigint": proto.OID_INT8,
        "int8": proto.OID_INT8,
        "float": proto.OID_FLOAT8,
        "float4": proto.OID_FLOAT4,
        "real": proto.OID_FLOAT4,
        "float8": proto.OID_FLOAT8,
        "double": proto.OID_FLOAT8,
        "double precision": proto.OID_FLOAT8,
        "numeric": proto.OID_NUMERIC,
        "decimal": proto.OID_NUMERIC,
        "boolean": proto.OID_BOOL,
        "bool": proto.OID_BOOL,
        "date": proto.OID_DATE,
        "time": proto.OID_TIME,
        "time without time zone": proto.OID_TIME,
        "timetz": proto.OID_TIMETZ,
        "time with time zone": proto.OID_TIMETZ,
        "timestamp": proto.OID_TIMESTAMP,
        "timestamp without time zone": proto.OID_TIMESTAMP,
        "timestamptz": proto.OID_TIMESTAMPTZ,
        "timestamp with time zone": proto.OID_TIMESTAMPTZ,
        "datetime": proto.OID_TIMESTAMP,
    }
    # Bug-6647 (adversarial finding): normalise the raw spelling the SAME way the
    # catalogue's _base_data_type does, so the wire RowDescription OID cannot
    # drift from information_schema. Strip a ``(precision[,scale])`` qualifier
    # anywhere (incl. ``time(6) with time zone``) WITHOUT dropping the tz phrase,
    # then collapse whitespace — so ``numeric(10,2)``, ``time(6)``,
    # ``timestamp(3) with time zone``, ``varchar(255)`` all resolve, matching
    # catalogue._type_oid.
    normalized = _PRECISION_PAREN_RE.sub("", (type_str or "").lower())
    normalized = " ".join(normalized.split()).strip()
    return mapping.get(normalized, proto.OID_TEXT)


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------

class TransportSecurityError(BaseException):
    """Startup refused because the JDBC transport posture is insecure (Wave C #2).

    Deliberately a ``BaseException`` (not ``Exception``): a refuse-to-start is a
    fatal configuration error like ``SystemExit``, and MUST NOT be swallowed by the
    broad ``except Exception`` around ``start_jdbc_server`` in the lifespan (which
    would otherwise let the gateway come up degraded — XMLA serving, no JDBC —
    instead of refusing). Propagating past that handler aborts startup, so a
    production gateway with password auth + TLS disabled never serves.
    """


def validate_transport_security(cfg) -> None:
    """Wave C #2: the JDBC listener must not START with password/JWT auth + TLS
    disabled unless local dev has EXPLICITLY opted out.

    TLS-required password/JWT auth is the complete JDBC auth contract, and TLS is
    required BY DEFAULT. ``start_jdbc_server`` calls this before binding the
    listener, so a misconfigured production gateway never opens a plaintext port.
    It raises :class:`TransportSecurityError` unless EITHER:

      * TLS is properly enabled AND required — ``GATEWAY_SSL_ENABLED`` with a
        cert+key AND ``GATEWAY_SSL_REQUIRED`` (plaintext startups refused per
        connection); OR
      * the EXPLICIT local-dev opt-out ``GATEWAY_ALLOW_INSECURE_TRANSPORT`` is set.
    """
    allow_insecure = getattr(cfg, "GATEWAY_ALLOW_INSECURE_TRANSPORT", False)
    ssl_required = getattr(cfg, "GATEWAY_SSL_REQUIRED", False)
    ssl_enabled = getattr(cfg, "GATEWAY_SSL_ENABLED", False)
    # Enforce only on a concrete, fully-resolved boolean configuration (a real
    # pydantic Settings). A config whose transport flags are not concrete booleans
    # is not a real deployment configuration and is not validated here.
    if not all(isinstance(v, bool) for v in (allow_insecure, ssl_required, ssl_enabled)):
        return
    if allow_insecure:
        if not (ssl_enabled and ssl_required):
            logger.warning(
                "GATEWAY_ALLOW_INSECURE_TRANSPORT=True — the JDBC listener may "
                "accept PLAINTEXT connections and transmit tenant passwords in "
                "the clear. This is for LOCAL DEVELOPMENT ONLY; never set it on a "
                "deployment reachable by real BI clients."
            )
        return

    problems: list[str] = []
    if not ssl_required:
        problems.append("GATEWAY_SSL_REQUIRED is False (plaintext startups accepted)")
    if not ssl_enabled:
        problems.append("GATEWAY_SSL_ENABLED is False (no TLS listener)")
    elif not (getattr(cfg, "GATEWAY_SSL_CERT_FILE", "")
              and getattr(cfg, "GATEWAY_SSL_KEY_FILE", "")):
        problems.append("GATEWAY_SSL_CERT_FILE / GATEWAY_SSL_KEY_FILE are not set")
    if problems:
        raise TransportSecurityError(
            "Gateway refuses to start: TLS is required by default but the JDBC "
            "transport is not secured (" + "; ".join(problems) + "). Provision TLS "
            "for production (GATEWAY_SSL_ENABLED=true with cert/key + "
            "GATEWAY_SSL_REQUIRED=true), or set "
            "GATEWAY_ALLOW_INSECURE_TRANSPORT=true for local development."
        )


def _build_ssl_context() -> ssl.SSLContext | None:
    """Build an SSLContext from gateway settings; return None when SSL is disabled."""
    if not settings.GATEWAY_SSL_ENABLED:
        return None
    cert = settings.GATEWAY_SSL_CERT_FILE
    key = settings.GATEWAY_SSL_KEY_FILE
    if not cert or not key:
        raise RuntimeError(
            "GATEWAY_SSL_ENABLED=True but GATEWAY_SSL_CERT_FILE / "
            "GATEWAY_SSL_KEY_FILE are not set"
        )
    for path, label in [(cert, "cert"), (key, "key")]:
        if not os.path.isfile(path):
            raise RuntimeError(f"SSL {label} file not found: {path}")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    if settings.GATEWAY_SSL_CA_FILE:
        if not os.path.isfile(settings.GATEWAY_SSL_CA_FILE):
            raise RuntimeError(
                f"SSL CA file not found: {settings.GATEWAY_SSL_CA_FILE}"
            )
        ctx.load_verify_locations(cafile=settings.GATEWAY_SSL_CA_FILE)
        ctx.verify_mode = ssl.CERT_OPTIONAL
    return ctx


async def start_jdbc_server() -> asyncio.Server:
    """
    Start the asyncio TCP server on JDBC_PORT.
    Returns the asyncio.Server object (caller must call serve_forever or
    manage its lifetime in a background task).
    """
    global _ssl_context
    # Wave C #2: refuse to bind the JDBC listener when the transport posture is
    # insecure (password/JWT auth + TLS disabled) unless local dev opted out.
    validate_transport_security(settings)
    port = settings.JDBC_PORT

    _ssl_context = _build_ssl_context()
    if _ssl_context:
        logger.info("JDBC SSL/TLS enabled (cert=%s)", settings.GATEWAY_SSL_CERT_FILE)

    server = await asyncio.start_server(
        lambda r, w: PGWireServer().handle_client(r, w),
        host="0.0.0.0",
        port=port,
    )
    logger.info("JDBC wire protocol server listening on port %d", port)
    return server
