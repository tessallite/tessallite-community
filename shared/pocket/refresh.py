"""Shared refresh/drop helpers for pocket tables."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
from jose import jwt

from sqlalchemy.ext.asyncio import AsyncSession

from shared.aggregate_connection import is_same_database, resolve_source_connection
from shared.connection_scope import (
    CrossProjectConnectionError,
    resolve_endpoint_connection_for_model,
)
from shared.config.settings import get_settings
from shared.config.source_db import (
    resolve_aggregate_target_defaults,
    resolve_target_schema,
)
from shared.connector_qualify import quote_identifier, quote_table_ref
from shared.db.models import DataTarget, Model, PocketDefinition, PocketRefreshRun, ProjectConnection
from shared.pocket_refresh_lock import (
    PocketRefreshInFlightError,
    acquire_pocket_refresh_lock,
)
from shared.pocket.structure import collect_pocket_structure_violations
from shared.schemas.connection_type import normalize_connection_type
from shared.source_executor import (
    ensure_target_schema,
    execute_source_ddl,
    execute_source_sql_scalar,
    open_source_connection,
    resolve_connector_type,
    stream_to_staging_table,
    table_storage_bytes,
)

# Connectors whose pocket cache table can be materialised same-connector.
# - postgresql / redshift: DROP + CTAS (or incremental delta) via a live
#   connection (the original path).
# - bigquery: atomic CREATE OR REPLACE TABLE, mirroring the aggregate BigQuery
#   CTAS path (shared/aggregate_table_ops + scheduler full_refresh).
_POCKET_TARGET_CONNECTORS = frozenset({"postgresql", "redshift", "bigquery"})

# Same-connector cross-database streaming (different connections, same engine
# family) is only implemented for the PostgreSQL family. Streaming from a
# BigQuery source into a PostgreSQL target (or any other connector mismatch)
# is NOT supported and must fail fast with a surfaced error instead of hanging.
_POCKET_CROSS_DB_CONNECTORS = frozenset({"postgresql", "redshift"})

logger = logging.getLogger(__name__)

_settings = get_settings()

# F-005-06: the legacy row-security wrap subquery alias. The wrap pass
# (``wrap_with_row_security``) was retired in F-007-01 in favour of per-scan
# WHERE injection, so this alias is no longer emitted on the current path —
# this remains a defence-in-depth fail-closed guard: if a rewritten pocket
# SELECT ever carries this alias, a row-security predicate was applied to it,
# meaning the materialised table would hold only the principal's permitted
# rows. Pocket materialisation must NEVER cache a row-security-filtered slice
# (it is served to every reader), so its presence is a fail-closed signal.
_ROW_SECURITY_WRAP_MARKER = "__ts_sec"


class PocketRowSecurityLeakError(ValueError):
    """Raised when a pocket's materialise SELECT would cache row-security-filtered
    rows. Fail closed: the cache is shared across readers and must hold the full
    unfiltered model subset.
    """


def _mint_service_token(tenant_id: str) -> str:
    """Mint a short-lived JWT for internal service-to-service calls."""
    payload = {
        "sub": "service:pocket-refresh",
        "tenant_id": tenant_id,
        "role": "system_admin",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    return jwt.encode(payload, _settings.JWT_SECRET_KEY, algorithm=_settings.JWT_ALGORITHM)


async def _get_rewritten_sql(
    model_id: object,
    defining_sql: str,
    bearer_token: str,
) -> str:
    """Call the query-router /explain endpoint to get the translated physical SQL."""
    url = f"{_settings.QUERY_ROUTER_URL}/api/v1/explain"
    headers = {"Authorization": f"Bearer {bearer_token}"}
    body = {
        "model_id": str(model_id),
        "raw_query": defining_sql,
        "protocol": "jdbc",
        "dialect": "postgres",
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=body, headers=headers)
        if resp.status_code >= 400:
            try:
                payload = resp.json()
                detail = payload.get("detail") if isinstance(payload, dict) else None
                if not isinstance(detail, str) or not detail:
                    detail = resp.text
            except Exception:
                detail = resp.text or f"Query router returned HTTP {resp.status_code}"
            raise ValueError(f"Query router explain failed: {detail}")
        data = resp.json()
        rewritten = data.get("rewritten_query")
        if not rewritten:
            raise ValueError("Query router returned empty rewritten_query")
        # F-005-06 (fail closed): the materialise SELECT must hold the FULL
        # model subset, not a row-security-filtered view. The caller always
        # mints a service token (no persona, role=system_admin) so user-role
        # rules do not fire; this guard additionally catches wildcard ("*")
        # rules that match every principal including the service token. The
        # primary signal is the structured `security_rules_applied` field on
        # the explain response (the router injects the predicate directly into
        # the WHERE clause, so there is no reliable string marker in the SQL);
        # the legacy subquery-wrap alias is kept as a secondary belt.
        applied_rules = data.get("security_rules_applied") or []
        if applied_rules or _ROW_SECURITY_WRAP_MARKER in rewritten:
            raise PocketRowSecurityLeakError(
                "Pocket refresh aborted: the model has a row-security rule that "
                "applies to the refresh service identity, so materialising would "
                "cache a row-filtered slice served to every reader. Scope the "
                "rule to specific roles (not '*') so pocket refresh can run "
                "under the unfiltered service identity."
            )
        return rewritten


async def _execute_via_router(
    model_id: object,
    defining_sql: str,
    bearer_token: str,
    *,
    timeout_s: float = 300.0,
) -> tuple[list[dict], list[str]]:
    """Execute pocket SQL through the query-router and return (rows, columns)."""
    url = f"{_settings.QUERY_ROUTER_URL}/api/v1/execute"
    headers = {"Authorization": f"Bearer {bearer_token}"}
    body = {
        "model_id": str(model_id),
        "raw_query": defining_sql,
        "protocol": "jdbc",
        "dialect": "postgres",
    }
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(url, json=body, headers=headers)
        if resp.status_code >= 400:
            try:
                payload = resp.json()
                detail = payload.get("detail") if isinstance(payload, dict) else None
                if not isinstance(detail, str) or not detail:
                    detail = resp.text
            except Exception:
                detail = resp.text or f"Query router returned HTTP {resp.status_code}"
            raise ValueError(f"Query router execute failed: {detail}")
        data = resp.json()
        rows = data.get("rows") or []
        columns = data.get("columns") or (list(rows[0].keys()) if rows else [])
        return rows, columns


def _quoted_table_ref(schema: str | None, table: str, connector: str = "postgresql") -> str:
    schema = (schema or "").strip()
    table = (table or "").strip()
    if "." in table and not schema:
        return quote_table_ref(connector, table)
    if schema:
        return quote_table_ref(connector, f"{schema}.{table}")
    return quote_identifier(connector, table)


def _resolve_target_schema(pocket: PocketDefinition, target: DataTarget) -> str:
    if pocket.target_schema:
        return str(pocket.target_schema)
    cfg = target.config if isinstance(target.config, dict) else {}
    return str(cfg.get("schema") or cfg.get("dataset") or "public")


def _resolve_target_location(pocket: PocketDefinition, target: DataTarget) -> tuple[str, str]:
    table_name = str(pocket.physical_table_name or "").strip()
    schema = _resolve_target_schema(pocket, target)
    if "." in table_name and not pocket.target_schema:
        left, right = table_name.split(".", 1)
        if left and right:
            return left, right
    return schema, table_name


def _quote_ident(name: str, connector: str = "postgresql") -> str:
    ident = (name or "").strip()
    if not ident:
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return quote_identifier(connector, ident)


def _infer_pg_type(value: object) -> str:
    if value is None:
        return "TEXT"
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, int):
        return "BIGINT"
    if isinstance(value, float):
        return "DOUBLE PRECISION"
    if isinstance(value, Decimal):
        return "NUMERIC"
    if isinstance(value, datetime):
        return "TIMESTAMPTZ"
    if isinstance(value, date):
        return "DATE"
    return "TEXT"


def _infer_col_types(rows: list[dict], col_names: list[str]) -> list[tuple[str, str]]:
    if not rows:
        return [(c, "TEXT") for c in col_names]
    sample = rows[:10]
    result = []
    for c in col_names:
        pg_type = "TEXT"
        for row in sample:
            v = row.get(c)
            if v is not None:
                pg_type = _infer_pg_type(v)
                break
        result.append((c, pg_type))
    return result


async def _validate_pocket_via_router(
    model_id: object,
    defining_sql: str,
    bearer_token: str,
    model_slug: str = "",
) -> str | None:
    """Call the query-router /validate endpoint to check pocket SQL.

    Returns None if valid, or a human-readable reason string.

    F-005-03: this is the single chokepoint every non-API writer goes through
    (optimizer auto-create + scheduled refresh). It enforces the FULL pocket
    model-subset grammar via ``shared.pocket.structure`` — not just the
    unresolvable-WHERE / complex-SQL legs — so an aggregate, a projection, or a
    multi-table-join pocket can never be materialised by the optimizer path,
    closing the gap where the model-subset grammar lived only in the
    model-service API.
    """
    url = f"{_settings.QUERY_ROUTER_URL}/api/v1/validate"
    headers = {"Authorization": f"Bearer {bearer_token}"}
    body = {
        "model_id": str(model_id),
        "raw_query": defining_sql,
        "protocol": "jdbc",
        "dialect": "postgres",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=body, headers=headers)
            if resp.status_code >= 400:
                return f"Router validation returned HTTP {resp.status_code}"
            data = resp.json()
            if not data.get("ok"):
                errors = data.get("errors") or []
                return "; ".join(errors) if errors else "Pocket SQL validation failed"
            violations = collect_pocket_structure_violations(data, model_slug)
            if violations:
                # Fail closed: the cached table would not be the model row-subset
                # the matcher assumes, so the only safe outcome is to refuse
                # materialisation (the caller marks the pocket failed /
                # non-routable rather than serving shape-wrong rows).
                return "; ".join(v.message for v in violations)
            return None
    except Exception as exc:
        logger.warning("Pocket validation via router failed: %s", exc)
        return f"Validation check failed: {exc}"


async def _table_exists_via(sc, schema: str, table: str) -> bool:
    row = await sc.fetch_one(
        "SELECT EXISTS ("
        "  SELECT 1 FROM information_schema.tables"
        "  WHERE table_schema = $1 AND table_name = $2"
        ") AS exists",
        schema, table,
    )
    return bool(row and row.get("exists"))


async def _fetch_storage_bytes(
    sc, schema: str, table: str, connector: str,
) -> int | None:
    """Best-effort storage size query. Returns None if unsupported or fails."""
    try:
        if connector == "postgresql":
            row = await sc.fetch_one(
                "SELECT pg_total_relation_size($1::regclass)::bigint AS b",
                f"{schema}.{table}",
            )
            return int(row["b"]) if row and row.get("b") is not None else None
        return None
    except Exception:
        return None


async def refresh_pocket_definition(
    pocket_id: object,
    db: AsyncSession,
    *,
    triggered_by: str = "scheduler",
    refresh_mode: str = "full",
    bearer_token: str | None = None,
    tenant_id: str | None = None,
    existing_run_id: object | None = None,
) -> PocketRefreshRun:
    """Refresh a pocket's materialised table.

    F-005-23: ``existing_run_id`` lets the async manual-refresh path pre-create
    a ``queued`` :class:`PocketRefreshRun` synchronously (so the API can return
    202 with a run id to poll) and have THIS function adopt that same row when
    it later executes in the background — rather than minting a second run. When
    ``None`` (scheduler/optimizer/sync paths) a run row is created here as
    before. The actual materialisation logic is unchanged.
    """
    pocket = await db.get(PocketDefinition, pocket_id)
    if pocket is None:
        raise ValueError(f"PocketDefinition {pocket_id} not found")

    # F-005-06: the materialisation (the SELECT that fills the cache) MUST run
    # under the system service identity, never the caller's token. Under the
    # caller's token the query-router wraps the rewrite with that caller's
    # row-security predicate, so the pocket would cache only the refresher's
    # permitted rows — then served to every reader. The user/caller token is
    # used ONLY for the pre-validate call below (validation parses + checks the
    # subset grammar; it returns no data rows, so it cannot leak rows).
    service_token = _mint_service_token(tenant_id or "system")

    # F-005-03: resolve the model slug so the shared subset grammar can pin the
    # FROM clause to this model (the optimizer/scheduled paths reach this
    # chokepoint without a model_slug otherwise).
    model_slug = ""
    model = await db.get(Model, pocket.model_id)
    if model is not None:
        model_slug = (getattr(model, "slug", "") or "").lower()

    # F-005-23: adopt a pre-created queued run (async manual path) when supplied.
    existing_run: PocketRefreshRun | None = None
    if existing_run_id is not None:
        existing_run = await db.get(PocketRefreshRun, existing_run_id)
        if existing_run is None or existing_run.pocket_definition_id != pocket.id:
            raise ValueError(
                f"PocketRefreshRun {existing_run_id} not found for pocket {pocket_id}"
            )

    if not await acquire_pocket_refresh_lock(db, pocket.id):
        exc = PocketRefreshInFlightError(pocket.id)
        if existing_run is not None:
            existing_run.status = "failed"
            existing_run.error_message = str(exc)[:1000]
            existing_run.completed_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(existing_run)
            return existing_run
        raise exc

    validate_token = bearer_token or service_token
    invalid_reason = await _validate_pocket_via_router(
        pocket.model_id, pocket.defining_sql, validate_token, model_slug
    )
    if invalid_reason is not None:
        now = datetime.now(timezone.utc)
        if existing_run is not None:
            failed_run = existing_run
            failed_run.status = "failed"
            failed_run.error_message = invalid_reason[:1000]
            failed_run.completed_at = now
        else:
            failed_run = PocketRefreshRun(
                pocket_definition_id=pocket.id,
                refresh_mode=refresh_mode,
                status="failed",
                triggered_by=triggered_by,
                error_message=invalid_reason[:1000],
                completed_at=now,
            )
            db.add(failed_run)
        pocket.status = "failed"
        pocket.failure_reason = invalid_reason[:1000]
        await db.commit()
        await db.refresh(failed_run)
        return failed_run
    # F-005-12 / F-005-17 (Bug-2254/2259): the lifecycle has no "invalid" state
    # — the DB CHECK (migration 0022) allows only fresh|stale|invalidating|failed,
    # and drift lands in "failed". The old ("invalid", "failed") tuple carried a
    # dead "invalid" branch; "failed" is the only reachable case here.
    if pocket.status == "failed":
        pocket.status = "stale"
        pocket.failure_reason = None

    target = await db.get(DataTarget, pocket.target_id)
    if target is None:
        raise ValueError(f"DataTarget {pocket.target_id} not found")

    # Bug-5500 fail-closed: a legacy/imported DataTarget can carry a
    # project_connection_id pointing at a ProjectConnection in a DIFFERENT
    # project than the model that owns the pocket. Resolve the target
    # connection through the shared scope guard (the owning model's project is
    # the project the connection must belong to) so pocket materialisation
    # DDL/streaming never targets another project's database. Mirrors the
    # gateway pocket-target execution path.
    target_conn = await resolve_endpoint_connection_for_model(
        db, target, model_id=pocket.model_id
    )

    source_conn = await resolve_source_connection(pocket.model_id, db)
    cross_db = not is_same_database(source_conn, target_conn)

    # Bug-5475: validate the source/target connector combination BEFORE any
    # state transition or remote work, and fail fast with a surfaced error.
    # Previously only postgresql/redshift targets were allowed; a BigQuery
    # target was rejected outright, and a cross-connector combo (e.g. BigQuery
    # source -> PostgreSQL target) was never reached here but, when it was,
    # hung indefinitely in the cross-DB streaming path with no error_message.
    target_connector = normalize_connection_type(target_conn.connection_type)
    source_connector = await resolve_connector_type(source_conn)
    combo_error = unsupported_pocket_combo_reason(
        source_connector, target_connector, cross_db
    )
    if combo_error is not None:
        now = datetime.now(timezone.utc)
        if existing_run is not None:
            failed_run = existing_run
            failed_run.status = "failed"
            failed_run.error_message = combo_error[:1000]
            failed_run.completed_at = now
        else:
            failed_run = PocketRefreshRun(
                pocket_definition_id=pocket.id,
                refresh_mode=refresh_mode,
                status="failed",
                triggered_by=triggered_by,
                error_message=combo_error[:1000],
                completed_at=now,
            )
            db.add(failed_run)
        pocket.status = "failed"
        pocket.failure_reason = combo_error[:1000]
        await db.commit()
        await db.refresh(failed_run)
        return failed_run

    if existing_run is not None:
        run = existing_run
        run.status = "running"
        run.refresh_mode = refresh_mode
    else:
        run = PocketRefreshRun(
            pocket_definition_id=pocket.id,
            refresh_mode=refresh_mode,
            status="running",
            triggered_by=triggered_by,
        )
        db.add(run)
    pocket.status = "invalidating"
    await db.commit()
    await db.refresh(run)
    await db.refresh(pocket)

    target_schema, target_table = _resolve_target_location(pocket, target)
    table_ref = _quoted_table_ref(target_schema, target_table, target_connector)

    try:
        # F-005-06: always the service token here — never the caller's token —
        # so the rewrite the executor materialises carries no row-security
        # filter. _get_rewritten_sql additionally fails closed if a wildcard
        # rule wrapped the SELECT anyway (PocketRowSecurityLeakError).
        token = service_token

        if cross_db:
            row_count, storage_bytes = await _refresh_cross_db(
                pocket, target_conn, target_schema, target_table, table_ref,
                token, refresh_mode, db,
            )
        elif target_connector == "bigquery" and source_connector == "bigquery":
            # Bug-5475: take the same-DB BigQuery CREATE OR REPLACE path only when
            # BOTH source and target are BigQuery. ``cross_db`` (is_same_database)
            # already screens connection identity, but we assert the connector
            # pair explicitly so a future change to is_same_database semantics
            # cannot route a non-BigQuery source's rewrite (wrong dialect) into
            # the BigQuery CTAS. A BigQuery target with a non-BigQuery source on
            # the same connection is an unsupported combo that is already rejected
            # up front by ``unsupported_pocket_combo_reason``.
            # The BigQuery dataset is resolved via the shared target resolver
            # (config.dataset priority), which can differ from the value
            # _resolve_target_location derived. Re-bind target_schema to the
            # dataset the table was ACTUALLY created in so the stamp below (and
            # therefore the pocket matcher's table reference) points at the
            # right dataset.
            target_schema, row_count, storage_bytes = await _refresh_same_db_bigquery(
                pocket, target_conn, target, target_schema, target_table,
                token, db,
            )
        else:
            row_count, storage_bytes = await _refresh_same_db(
                pocket, target_conn, target_schema, target_table, table_ref,
                token, refresh_mode, db,
                target_connector=target_connector,
            )

        now = datetime.now(timezone.utc)
        run.status = "completed"
        run.completed_at = now
        run.rows_written = row_count
        run.bytes_processed = storage_bytes

        pocket.status = "fresh"
        pocket.failure_reason = None
        pocket.last_refresh_at = now
        pocket.target_schema = target_schema
        pocket.physical_table_name = target_table
        pocket.row_count = row_count
        pocket.storage_bytes = storage_bytes

    except Exception as exc:
        now = datetime.now(timezone.utc)
        run.status = "failed"
        run.completed_at = now
        run.error_message = str(exc)[:1000]

        pocket.status = "failed"
        pocket.failure_reason = str(exc)[:1000]

    await db.commit()
    await db.refresh(run)
    return run


def unsupported_pocket_combo_reason(
    source_connector: str,
    target_connector: str,
    cross_db: bool,
) -> str | None:
    """Return a human-readable reason when this source/target connector pair
    cannot be materialised, or ``None`` when it is supported.

    Bug-5475: fail fast (so the caller marks the run ``failed`` with a surfaced
    message) instead of attempting a path that hangs indefinitely.

    Supported:
      - same-connection (``cross_db`` False): postgresql, redshift, bigquery.
      - cross-database (``cross_db`` True, different connections): only the
        PostgreSQL family AND only when source and target share that family
        (PG/Redshift stream-and-stage). Any connector mismatch — notably a
        BigQuery source with a PostgreSQL target — is rejected.
    """
    if target_connector not in _POCKET_TARGET_CONNECTORS:
        return (
            f"Pocket tables require a PostgreSQL, Redshift, or BigQuery target "
            f"for materialisation; the selected target uses "
            f"{target_connector!r}, which is not supported."
        )
    if not cross_db:
        return None
    # Cross-database: only the PG family, and only PG-family source.
    if (
        source_connector not in _POCKET_CROSS_DB_CONNECTORS
        or target_connector not in _POCKET_CROSS_DB_CONNECTORS
    ):
        return (
            f"Cross-connector pocket materialisation is not supported "
            f"(source={source_connector!r}, target={target_connector!r}). "
            f"Materialise the pocket to a target on the same source engine — "
            f"for a BigQuery source, use a BigQuery target."
        )
    return None


async def _refresh_same_db_bigquery(
    pocket: PocketDefinition,
    target_conn: ProjectConnection,
    target: DataTarget,
    target_schema: str,
    target_table: str,
    token: str,
    db: AsyncSession,
) -> tuple[str, int | None, int | None]:
    """Materialise a pocket into a BigQuery target via atomic CREATE OR REPLACE.

    Bug-5475: mirrors the aggregate BigQuery CTAS path. Source and target are
    the same BigQuery connection (gated by ``unsupported_pocket_combo_reason``), so
    the router-rewritten SELECT is already BigQuery dialect and executes
    in-place — no buffering of the full result in model-service, no streaming.
    ``CREATE OR REPLACE TABLE`` is atomic, so there is no destructive DROP gap.

    Returns ``(resolved_schema, row_count, storage_bytes)``. The resolved schema
    is the BigQuery dataset the table was actually created in (via the shared
    target resolver), which the caller stamps onto ``pocket.target_schema`` so
    the matcher's table reference points at the same dataset.
    """
    select_sql = await _get_rewritten_sql(pocket.model_id, pocket.defining_sql, token)

    defaults = await resolve_aggregate_target_defaults(
        tenant_session=db, project_id=target_conn.project_id,
    )
    # NOTE on resolution priority (BigQuery): inside resolve_target_schema the
    # BigQuery branch is config-dataset-first, so when target.config carries a
    # "dataset" it WINS over this ``schema_override``. In other words, on a
    # configured BigQuery target ``pocket.target_schema`` has no effect on which
    # dataset the table lands in — it is NOT a routing knob here. The override
    # only takes effect when the target config has no dataset (then it falls
    # back through schema_override -> config.schema -> default dataset). The
    # resolved dataset is returned to the caller and stamped back onto
    # pocket.target_schema so the matcher points at the dataset actually written.
    tgt_ref = resolve_target_schema(
        "bigquery", target.config or {}, defaults,
        schema_override=pocket.target_schema,
    )
    schema = tgt_ref.schema or target_schema
    dotted = tgt_ref.qualified_table(target_table)
    bq_table_ref = quote_table_ref("bigquery", dotted)

    await ensure_target_schema(target_conn, schema, tenant_session=db)
    await execute_source_ddl(
        target_conn,
        f"CREATE OR REPLACE TABLE {bq_table_ref} AS {select_sql}",
        tenant_session=db,
    )

    count_row = await execute_source_sql_scalar(
        target_conn,
        f"SELECT COUNT(*) AS c FROM {bq_table_ref}",
        tenant_session=db,
    )
    row_count = int(count_row) if count_row is not None else None

    storage_bytes = await table_storage_bytes(
        target_conn, schema, target_table,
        bq_project=tgt_ref.bq_project or None,
        tenant_session=db,
    )
    return schema, row_count, storage_bytes


async def _refresh_same_db(
    pocket: PocketDefinition,
    target_conn: ProjectConnection,
    target_schema: str,
    target_table: str,
    table_ref: str,
    token: str,
    refresh_mode: str,
    db: AsyncSession,
    target_connector: str = "postgresql",
) -> tuple[int | None, int | None]:
    select_sql = await _get_rewritten_sql(pocket.model_id, pocket.defining_sql, token)

    async with open_source_connection(target_conn, tenant_session=db) as sc:
        supports_incremental = bool(pocket.incremental_column)
        use_incremental = supports_incremental and refresh_mode in {"incremental", "scheduled"}
        table_exists = await _table_exists_via(sc, target_schema, target_table)

        if use_incremental and table_exists:
            incremental_col = _quote_ident(str(pocket.incremental_column))
            lookback_hours = int(pocket.incremental_lookback_hours or 24)
            delta_sql = (
                f'SELECT * FROM ({select_sql}) AS pocket_src '
                f"WHERE pocket_src.{incremental_col} >= (NOW() - INTERVAL '{max(lookback_hours, 1)} hours')"
            )
            tmp_table = f'_tmp_{str(pocket.id).replace("-", "")[:16]}'
            tmp_ref = _quote_ident(tmp_table)
            await sc.execute(f"CREATE TEMP TABLE {tmp_ref} AS {delta_sql}")
            await sc.execute(
                f"DELETE FROM {table_ref} AS dst USING {tmp_ref} AS src "
                f"WHERE dst.{incremental_col} = src.{incremental_col}"
            )
            await sc.execute(f"INSERT INTO {table_ref} SELECT * FROM {tmp_ref}")
        else:
            await sc.execute(f"DROP TABLE IF EXISTS {table_ref}")
            await sc.execute(f"CREATE TABLE {table_ref} AS {select_sql}")

        row = await sc.fetch_one(f"SELECT COUNT(*)::bigint AS c FROM {table_ref}")
        row_count = int(row["c"]) if row and row.get("c") is not None else None

        storage_bytes = await _fetch_storage_bytes(sc, target_schema, target_table, target_connector)

    return row_count, storage_bytes


async def _refresh_cross_db(
    pocket: PocketDefinition,
    target_conn: ProjectConnection,
    target_schema: str,
    target_table: str,
    table_ref: str,
    token: str,
    refresh_mode: str,
    db: AsyncSession,
) -> tuple[int | None, int | None]:
    rewritten_sql = await _get_rewritten_sql(pocket.model_id, pocket.defining_sql, token)
    source_conn = await resolve_source_connection(pocket.model_id, db)

    await ensure_target_schema(target_conn, target_schema, tenant_session=db)

    async def _streaming_batches():
        async with open_source_connection(source_conn, tenant_session=db) as sc:
            async for batch in sc.fetch_batched(rewritten_sql, batch_size=20_000):
                yield batch

    # Pocket targets are gated to postgresql/redshift above, so the staging
    # target never needs a BigQuery project qualifier (F-005-17: the old
    # `target_project` plumbing was dead — `bq_project` was always "").
    row_count = await stream_to_staging_table(
        target_conn, target_schema, target_table,
        _streaming_batches(),
        batch_size=20_000,
        infer_types_fn=_infer_col_types,
        tenant_session=db,
    )

    target_connector = normalize_connection_type(target_conn.connection_type)
    async with open_source_connection(target_conn, tenant_session=db) as sc:
        storage_bytes = await _fetch_storage_bytes(sc, target_schema, target_table, target_connector)

    return row_count, storage_bytes


async def drop_pocket_storage(
    pocket: PocketDefinition,
    db: AsyncSession,
) -> None:
    target = await db.get(DataTarget, pocket.target_id)
    if target is None:
        return

    # Bug-5500 fail-closed: never issue DROP TABLE against a target connection
    # in a different project than the pocket's owning model. A cross-project
    # row here would drop a table on another project's database. Resolve through
    # the shared scope guard and refuse (skip the drop) on mismatch rather than
    # execute destructive DDL elsewhere; missing connection/model is treated the
    # same as the existing best-effort no-op cleanup.
    try:
        conn = await resolve_endpoint_connection_for_model(
            db, target, model_id=pocket.model_id
        )
    except CrossProjectConnectionError:
        logger.error(
            "drop_pocket_storage: refusing to drop pocket %s storage — its "
            "target connection belongs to a different project than model %s "
            "(cross-project row rejected fail-closed)",
            pocket.id, pocket.model_id,
        )
        return
    except ValueError:
        return

    connector = normalize_connection_type(conn.connection_type)
    if connector not in _POCKET_TARGET_CONNECTORS:
        return

    target_schema, target_table = _resolve_target_location(pocket, target)

    if connector == "bigquery":
        # Bug-5475: BigQuery targets qualify the dataset/project via the shared
        # target resolver, then quote with backticks (never double quotes).
        defaults = await resolve_aggregate_target_defaults(
            tenant_session=db, project_id=conn.project_id,
        )
        tgt_ref = resolve_target_schema(
            "bigquery", target.config or {}, defaults,
            schema_override=pocket.target_schema,
        )
        dotted = tgt_ref.qualified_table(target_table)
        table_ref = quote_table_ref("bigquery", dotted)
    else:
        table_ref = _quoted_table_ref(target_schema, target_table, connector)

    await execute_source_ddl(conn, f"DROP TABLE IF EXISTS {table_ref}", tenant_session=db)
