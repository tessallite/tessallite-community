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
import threading
from typing import Optional

import sqlglot
from sqlglot import exp

from shared.config.settings import get_settings
from src.auth.base import verify_jwt_token
from src.jdbc.catalogue import CatalogueDB, CatalogueQueryError
from src.jdbc import protocol as proto
from src.jdbc.throttle import get_governor
from src.router_client import QueryRouterError, execute_query, fetch_model_metadata, login_for_token

logger = logging.getLogger(__name__)
settings = get_settings()

_ssl_context: ssl.SSLContext | None = None

# Connection counter — used as fake PID for BackendKeyData
_conn_counter = 0
_conn_counter_lock = threading.Lock()  # M-06 fix: protect concurrent increment

# Bug-5188: registry of in-flight query tasks keyed by (pid, cancel_secret).
# CancelRequest on a fresh socket looks up the target connection's task and
# cancels it. Protected by _conn_counter_lock (reuse; always short-held).
_inflight_tasks: dict[tuple[int, int], asyncio.Task] = {}

_PARAM_PLACEHOLDER_RE = re.compile(r"\$(\d+)")

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
        self._statements: dict[str, str] = {}  # statement_name → raw SQL (with $N placeholders)
        self._statement_param_oids: dict[str, list[int]] = {}  # statement_name → declared param OIDs
        self._session_vars: dict[str, str] = {}  # app.<name> → value (for parameterized filters)
        self._catalogue: CatalogueDB | None = None
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

        # Phase 2: send startup confirmation
        writer.write(proto.startup_sequence(pid=self._pid, secret=self._cancel_secret))
        await writer.drain()

        # Fetch model metadata once at connection time (best-effort).
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
        )
        logger.info(
            "Metadata loaded (pid=%d): tenant=%s model_id=%s tables=%s",
            self._pid, self._tenant_slug, self._model_id, self._model_names,
        )
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
        self._tenant_slug = params.get("database", "default")
        self._model_id = params.get("model_id") or None
        email = params.get("user", "")
        governor = get_governor()

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

        governor.record_auth_success(self._peer_ip)
        return True

    @staticmethod
    def _tenant_matches(jwt_tenant: str, database_param: str) -> bool:
        """Return True iff the JWT tenant equals the database startup param.

        F-001-10 cross-check. Fail-closed: a blank JWT tenant claim never
        matches anything, so a token without a tenant_id cannot satisfy a
        tenant-scoped connection. The ``default`` database placeholder (used by
        catalogue-only browse sessions where the JWT carries the real tenant)
        is the single benign exception — no tenant data is read under it
        because every user query is scoped by the JWT downstream anyway, and
        rejecting it would break tools that connect without naming a database.
        """
        if not jwt_tenant:
            return False
        if database_param == jwt_tenant:
            return True
        if database_param in ("", "default", "postgres"):
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

    # ------------------------------------------------------------------
    # Query loop
    # ------------------------------------------------------------------

    async def _query_loop(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        # Extended-query state: SQL extracted from the most recent Parse message
        _pending_sql: str = ""
        _pending_rows: list | None = None
        _pending_cols: list | None = None
        _result_formats: list[int] = []
        _pending_num_params: int = 0
        # Parameter-type OIDs declared by the client in the active Parse
        # message (F-001-06). Drives whether Bind inlines a value raw or
        # quotes it as text.
        _pending_param_oids: list[int] = []
        # True once a Bind has substituted real parameter values into
        # _pending_sql. Drives the single-execution describe policy (F-001-01):
        # a Describe after Bind may execute once and keep the rows for Execute;
        # a statement-Describe before Bind never executes (metadata-only).
        _pending_bound: bool = False
        # Per pgwire spec: once an error is emitted in the extended protocol,
        # all messages are skipped until the next Sync.  _error_skip tracks
        # that "aborted transaction block" state so we don't double-report.
        _error_skip: bool = False

        while True:
            type_char, payload = await proto.read_message(reader)

            if type_char == "X":  # Terminate
                return

            elif type_char == "Q":  # Simple Query
                sql = payload.rstrip(b"\x00").decode("utf-8", errors="replace")
                await self._log_sql(sql, "Q")
                await self._handle_query(sql, writer)

            # ----------------------------------------------------------------
            # Extended query protocol (used by DBeaver, psycopg2, pgJDBC)
            # ----------------------------------------------------------------

            elif type_char == "P":  # Parse — extract SQL, cache by statement name
                if _error_skip:
                    continue
                stmt_name, raw_sql, param_oids = proto.parse_parse_message(payload)
                await self._log_sql(raw_sql, "P")
                self._statements[stmt_name] = raw_sql
                # F-001-06: remember the client-declared parameter type OIDs so
                # Bind can quote text parameters as text rather than sniffing.
                self._statement_param_oids[stmt_name] = param_oids
                _pending_sql = raw_sql
                _pending_param_oids = param_oids
                _pending_rows = None
                _pending_cols = None
                _result_formats = []
                _pending_bound = False
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
                raw_sql = self._statements.get(_stmt_peek, _pending_sql)
                # F-001-06: prefer the parameter OIDs the named statement
                # declared; fall back to the most recent Parse for the
                # unnamed statement flow.
                param_oids = self._statement_param_oids.get(_stmt_peek, _pending_param_oids)
                _portal, _stmt, params, _result_formats = proto.parse_bind_parameters(
                    payload, param_oids=param_oids,
                )
                if params:
                    _pending_sql = _substitute_params(raw_sql, params, param_oids)
                    logger.debug("Bind substitution (pid=%d): %s", self._pid, _pending_sql[:300])
                else:
                    _pending_sql = raw_sql
                # A fresh Bind invalidates any rows that a prior statement-level
                # Describe may have produced; the portal now carries new params.
                _pending_bound = True
                _pending_rows = None
                _pending_cols = None
                writer.write(proto.bind_complete())
                await writer.drain()

            elif type_char == "D":  # Describe portal/statement
                if _error_skip:
                    continue
                describe_type = chr(payload[0]) if payload else "P"
                if describe_type == "S":
                    param_oids = [proto.OID_TEXT] * _pending_num_params
                    writer.write(proto.parameter_description(param_oids))
                if not _pending_sql:
                    writer.write(proto.no_data())
                    await writer.drain()
                    continue

                # F-001-01 — execute the user query at most once.
                #
                # Case 1: parameters still unbound (statement-level Describe
                # before Bind). Never execute a NULL-substituted probe — it
                # costs a source round trip, returns the wrong shape, and
                # double-counts query telemetry. Derive the RowDescription
                # from catalogue metadata when possible; otherwise advertise
                # text columns and let Execute (which runs once) emit the
                # authoritative description.
                #
                # Case 2: parameters bound (portal Describe, the psycopg2 /
                # pgJDBC flow). Execute exactly once here and KEEP the rows so
                # Execute reuses them instead of re-running the query.
                if _pending_num_params > 0 and not _pending_bound:
                    derived = self._describe_columns_metadata_only(_pending_sql)
                    if derived is not None:
                        _pending_cols = derived
                        writer.write(proto.row_description(derived, _result_formats))
                    else:
                        _pending_cols = None
                        writer.write(proto.no_data())
                    _pending_rows = None
                    await writer.drain()
                    continue

                _pending_cols, _pending_rows, err = await self._execute_for_extended(_pending_sql)
                if err is not None:
                    writer.write(proto.error_response(err[0], code=err[1]))
                    _error_skip = True
                    _pending_rows = None
                    _pending_cols = None
                elif _pending_cols is not None:
                    writer.write(proto.row_description(_pending_cols, _result_formats))
                else:
                    writer.write(proto.no_data())
                await writer.drain()

            elif type_char == "E":  # Execute → DataRow* + CommandComplete
                if _error_skip:
                    continue
                described = _pending_cols is not None
                # Re-execute when Describe was skipped or rows were
                # deferred (parameterised queries — real params arrive
                # via Bind after Describe).
                if _pending_rows is None and _pending_sql:
                    _pending_cols, _pending_rows, err = await self._execute_for_extended(_pending_sql)
                    if err is not None:
                        writer.write(proto.error_response(err[0], code=err[1]))
                        _error_skip = True
                        _pending_rows = None
                        _pending_cols = None
                        await writer.drain()
                        continue

                if _pending_cols is None:
                    writer.write(proto.command_complete("SELECT 0"))
                else:
                    col_oids = [oid for _, oid in _pending_cols]
                    if not described:
                        writer.write(proto.row_description(_pending_cols, _result_formats))
                    for row in (_pending_rows or []):
                        writer.write(proto.data_row(row, _result_formats, col_oids))
                    writer.write(proto.command_complete(f"SELECT {len(_pending_rows or [])}"))
                _pending_rows = None
                _pending_cols = None
                await writer.drain()

            elif type_char == "S":  # Sync — flush, clear error state, signal ready
                _error_skip = False
                _result_formats = []
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

        # SET / SHOW / transaction control are driver housekeeping
        stripped_upper = sql.strip().upper()
        if stripped_upper.startswith("SET "):
            self._capture_session_var(sql.strip())
            return None, None, None
        if stripped_upper.startswith("SHOW "):
            return self._handle_show_extended(stripped_upper)
        if stripped_upper in ("BEGIN", "COMMIT", "ROLLBACK") or stripped_upper.startswith("SAVEPOINT "):
            return None, None, None
        if stripped_upper.startswith("DEALLOCATE"):
            return None, None, None

        if self._catalogue is not None:
            try:
                # F-001-16: offload synchronous SQLite to a worker thread so a
                # metadata flood on one connection does not stall the loop.
                result = await asyncio.to_thread(self._catalogue.execute, sql)
            except CatalogueQueryError as exc:
                return None, None, (f"Catalogue query failed: {exc}", "42601")
            if result is not None:
                columns, rows = result
                return columns, rows, None

        constant = self._try_constant_select(sql)
        if constant is not None:
            col_desc, rows = constant
            return col_desc, rows, None
        # F-001-05: a FROM-less SELECT we could not answer as literals
        # (``SELECT now()``, ``SELECT 1+1``) is evaluated by the catalogue
        # SQLite engine; if even that fails we reject with a clear error
        # rather than forwarding to the router or echoing expression text.
        if _is_constant_select_sqlglot(sql.strip().rstrip(";")):
            evaluated = self._eval_constant_select(sql)
            if evaluated is not None:
                col_desc, rows = evaluated
                return col_desc, rows, None
            return None, None, (
                "Unsupported constant expression; the gateway can evaluate "
                "literal and simple scalar SELECTs only.",
                "0A000",
            )

        if not self._jwt_token:
            return None, None, ("Authentication required.", "28000")

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

        sql = self._rewrite_exposed_relations(sql)
        try:
            result = await self._run_cancellable(execute_query(
                model_id=model_id,
                sql=sql,
                tenant_slug=self._tenant_slug,
                jwt_token=self._jwt_token,
                include_hidden=include_hidden,
                persona_id=persona_id,
                session_vars=self._session_vars or None,
                client_kind=self._client_kind,
            ))
        except asyncio.CancelledError:
            logger.info("Extended query cancelled by client (pid=%d)", self._pid)
            return None, None, ("Query cancelled by client request.", "57014")
        except QueryRouterError as exc:
            logger.error("Extended query error (pid=%d): %s", self._pid, exc)
            return None, None, (exc.detail, exc.sqlstate or "42601")
        except Exception as exc:
            logger.error("Extended query error (pid=%d): %s", self._pid, exc)
            return None, None, (str(exc), "42601")

        columns_meta = result.get("columns", [])
        rows_data = result.get("rows", [])
        # F-001-09: shape $KPIs results by the query's projection/filter/limit.
        if self._is_kpi_table_query(sql):
            columns_meta, rows_data, kpi_err = self._shape_kpi_result(sql, columns_meta, rows_data)
            if kpi_err is not None:
                return None, None, (kpi_err, "42601")
        col_desc = _parse_columns(self._type_columns_from_catalogue(sql, columns_meta))
        col_names = [c[0] for c in col_desc]
        rows = []
        for row in rows_data:
            if isinstance(row, dict):
                rows.append([_normalize_jdbc_value(row.get(c)) if row.get(c) is not None else None for c in col_names])
            else:
                rows.append([_normalize_jdbc_value(v) if v is not None else None for v in row])
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

        # Handle driver housekeeping before catalogue to avoid misrouting
        # (e.g. SET session_user triggers _CATALOGUE_RE via \bsession_user\b)
        stripped_upper = sql.strip().upper()
        if (
            stripped_upper.startswith("SET ")
            or stripped_upper.startswith("SHOW ")
            or stripped_upper.startswith("DEALLOCATE")
            or stripped_upper in ("BEGIN", "COMMIT", "ROLLBACK")
            or stripped_upper.startswith("SAVEPOINT ")
        ):
            await self._handle_user_query(sql, writer)
            return

        if self._catalogue is not None:
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

    _SET_APP_RE = re.compile(
        r"^SET\s+app\.(\w+)\s*(?:=|TO)\s*(.+)$", re.IGNORECASE
    )

    def _capture_session_var(self, sql: str) -> None:
        m = self._SET_APP_RE.match(sql)
        if m:
            key = f"app.{m.group(1).lower()}"
            # Bug-1094: psql (and most drivers) terminate the statement with
            # a semicolon, so the value group can arrive as ``'RETAIL';``.
            # Strip trailing whitespace and any statement terminator BEFORE
            # unwrapping the quotes, otherwise the ``;`` is folded into the
            # captured value and no parameter ever matches it.
            raw = m.group(2).strip().rstrip(";").strip()
            value = raw.strip("'\"")
            self._session_vars[key] = value

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
            ):
                for col in cols or []:
                    name = col.get("name")
                    if name:
                        type_map[str(name).lower()] = str(
                            col.get("data_type") or "text"
                        )
        return type_map

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
        stripped = sql.strip().upper()

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

        sql = self._rewrite_exposed_relations(sql)
        try:
            result = await self._run_cancellable(execute_query(
                model_id=model_id,
                sql=sql,
                tenant_slug=self._tenant_slug,
                jwt_token=self._jwt_token,
                include_hidden=include_hidden,
                persona_id=persona_id,
                session_vars=self._session_vars or None,
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
            logger.error("Query execution error (pid=%d): %s", self._pid, exc)
            writer.write(proto.error_response(exc.detail, code=exc.sqlstate or "42601"))
            writer.write(proto.ready_for_query())
            await writer.drain()
            return
        except Exception as exc:
            logger.error("Query execution error (pid=%d): %s", self._pid, exc)
            writer.write(proto.error_response(str(exc)))
            writer.write(proto.ready_for_query())
            await writer.drain()
            return

        columns_meta = result.get("columns", [])
        rows_data = result.get("rows", [])
        # F-001-09: shape $KPIs results by the query's projection/filter/limit.
        if self._is_kpi_table_query(sql):
            columns_meta, rows_data, kpi_err = self._shape_kpi_result(sql, columns_meta, rows_data)
            if kpi_err is not None:
                writer.write(proto.error_response(kpi_err, code="42601"))
                writer.write(proto.ready_for_query())
                await writer.drain()
                return

        col_desc = _parse_columns(self._type_columns_from_catalogue(sql, columns_meta))
        writer.write(proto.row_description(col_desc))
        col_names = [c[0] for c in col_desc]
        for row in rows_data:
            if isinstance(row, dict):
                writer.write(proto.data_row([_normalize_jdbc_value(row.get(c)) if row.get(c) is not None else None for c in col_names]))
            else:
                writer.write(proto.data_row([_normalize_jdbc_value(v) if v is not None else None for v in row]))
        writer.write(proto.command_complete(f"SELECT {len(rows_data)}"))
        writer.write(proto.ready_for_query())
        await writer.drain()


# ---------------------------------------------------------------------------
# Numeric value normalisation for JDBC wire
# ---------------------------------------------------------------------------

def _normalize_jdbc_value(v: object) -> str:
    """Convert a JSON-deserialised value to a JDBC-wire text string.

    Bug-5383: asyncpg may return ``Decimal('1E+5')`` for aggregate results.
    Pydantic 2 serialises that as the JSON string ``"1.0E+5"`` (or the JSON
    number ``100000.0``).  The gateway receives the deserialised form — either
    a Python ``float`` (from a JSON number) or a ``str`` (from a JSON string
    that looks numeric).  In both cases a whole-number value must render as a
    plain integer string (``"100000"``, not ``"1.0E+5"`` or ``"100000.0"``) so
    that JDBC clients see the same integer the source database returned.
    """
    s = str(v)
    try:
        f = float(s)
        # Only normalise finite values that are exact integers.
        if f == int(f) and not (f != f):  # NaN guard
            return str(int(f))
    except (ValueError, OverflowError):
        pass
    return s


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
    """Replace $1, $2, ... placeholders with parameter values.

    Type fidelity (F-001-06): a value is inlined raw only when the client
    declared a numeric OID for that parameter (or, when no OID was declared,
    when the value is a strict SQL numeric literal). Every other value —
    including numeric-looking text such as a zip code declared VARCHAR — is
    quoted as a string so PostgreSQL preserves its type. ``None`` becomes
    ``NULL``. Quote escaping is unchanged (doubling internal quotes).
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
        if declared_oid in proto.NUMERIC_PARAM_OIDS:
            # Client declared a numeric type. Validate strictly before
            # inlining so a malformed numeric never reaches the source as
            # raw SQL; quote-as-text otherwise (let the source coerce/raise).
            if _STRICT_NUMERIC_RE.match(val):
                return val
            return "'" + val.replace("'", "''") + "'"
        if declared_oid != 0:
            # Any other declared type (text, date, bool, ...) → quote as text.
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
        "numeric": proto.OID_NUMERIC,
        "decimal": proto.OID_NUMERIC,
        "boolean": proto.OID_BOOL,
        "bool": proto.OID_BOOL,
        "date": proto.OID_DATE,
        "timestamp": proto.OID_TIMESTAMP,
        "timestamptz": proto.OID_TIMESTAMPTZ,
        "datetime": proto.OID_TIMESTAMP,
    }
    return mapping.get(type_str.lower(), proto.OID_TEXT)


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------

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
