"""Shared refresh/drop helpers for pocket tables."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from shared.auth.service_principal import SCOPE_POCKET_REFRESH, create_service_access_token
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import ObjectDeletedError

from shared.aggregate_connection import is_same_database, resolve_source_connection
from shared.artifact_build_binding import apply_build_binding, capture_build_binding
from shared.artifact_incremental_gate import full_rebuild_required
from shared.artifact_target_binding import (
    capture_source_build_binding,
    capture_target_build_binding,
)
from shared.connection_scope import (
    CrossProjectConnectionError,
    resolve_endpoint_connection_for_model,
)
from shared.config.resolver import get_setting
from shared.config.settings import get_settings
from shared.db.session import SystemSessionLocal
from shared.config.source_db import (
    resolve_aggregate_target_defaults,
    resolve_target_schema,
)
from shared.connector_qualify import quote_identifier, quote_table_ref
from shared.db.models import DataTarget, Model, PocketDefinition, PocketRefreshRun, ProjectConnection
from shared.model_refresh_epoch import bump_data_epoch
from shared.pocket_refresh_lock import (
    PocketRefreshInFlightError,
    pocket_refresh_lock,
)
from shared.pocket.incremental import (
    POCKET_RUN_STATUS_COMPLETED,
    build_delta_sql,
    build_identity_delete_sql,
    build_insert_from_delta_sql,
    build_key_count_sql,
    build_key_integrity_probe_sql,
    build_key_scan_sql,
    build_orphan_delete_sql,
    build_unreconciled_key_probe_sql,
    build_window_expression,
    resolve_incremental_window_start,
    resolve_row_identity_candidates,
    usable_identity_columns,
    window_elapsed_seconds,
)
from shared.pocket.refresh_guard import (
    POCKET_STATUS_FRESH,
    POCKET_STATUS_INVALIDATING,
    POCKET_STATUS_STALE,
    read_committed_pocket_status,
    read_pocket_finalization_state,
    resolve_pocket_serving_refusal,
)
from shared.pocket.row_manifest import (
    clear_pocket_row_manifest,
    write_pocket_row_manifest,
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

# ``POCKET_STATUS_FRESH`` (the one status the pocket matcher serves) and
# ``POCKET_STATUS_STALE`` are imported from ``shared.pocket.refresh_guard``, which
# owns the finalisation decision that writes them (Bug-8807). They are used
# throughout this module; no other module imports them from here — every
# consumer takes them from ``refresh_guard``.

# Refresh modes that MAY take the incremental delta path. Every other mode
# ("full", "manual", "create") always re-materialises the whole table.
_INCREMENTAL_REFRESH_MODES = frozenset({"incremental", "scheduled"})

_settings = get_settings()


@dataclass(frozen=True)
class PocketBuildState:
    """Immutable snapshot of the inputs the incremental-vs-full decision needs.

    Bug-8431: captured inside the refresh lock BEFORE ``refresh_pocket_definition``
    overwrites ``pocket.status`` with ``"invalidating"``. Reading ``pocket.status``
    at the point the branch is actually taken would always observe
    ``"invalidating"`` and make the staleness limb of the guard meaningless — it
    would either fire on every run or (if inverted) never fire at all. Plain
    scalars, not the ORM row, so no later mutation, commit, or session expiry can
    change what the decision was based on.
    """

    entry_status: str | None
    built_for_version_id: Any | None
    built_for_epoch: Any | None
    deployed_version_id: Any | None
    deploy_epoch: Any | None


def should_refresh_incrementally(
    *,
    incremental_column: str | None,
    refresh_mode: str,
    table_exists: bool,
    build_state: PocketBuildState,
    window_start: datetime | None,
) -> bool:
    """The CONTROL-PLANE decision point for pocket incremental vs full rebuild.

    Returns True only when a partial DELETE/INSERT over the delta window is
    a SOUND way to bring the cached table up to date:

    * the pocket declares a watermark column, and
    * the caller asked for a mode that permits an incremental leg, and
    * a physical table already exists to patch, and
    * the pocket entered this build in the only status the matcher serves, and
    * the runtime matcher does not currently refuse the artifact, and
    * the delta window can be anchored to this pocket's own last COMPLETED
      refresh run (Bug-8700).

    Everything decidable from control-plane state lives here. Two further
    preconditions are decidable only against the TARGET DATABASE and are
    evaluated by ``_refresh_same_db`` immediately before the leg runs: the
    cached table must expose a usable row-identity key (Bug-8699,
    ``shared.pocket.incremental.usable_identity_columns``) and that key must
    actually be unique and non-NULL in the rows on disk. Both degrade to a full
    rebuild, so "this returned True" means "no control-plane reason to refuse",
    never "the leg will definitely run".

    ``window_start`` of None is Bug-8700's fail-closed case: with no completed
    run to anchor to there is no window that provably covers everything changed
    since the cache was last correct, and the old ``NOW() - lookback`` fallback
    is exactly the wall-clock window that silently dropped rows.

    The last two conditions are Bug-8431. ``services/scheduler/src/jobs/pocket_refresh.py``
    dispatches EVERY swept pocket — including ``stale``, ``failed`` and recovered
    ``invalidating`` ones — as ``refresh_mode="scheduled"``, so without it a pocket
    that a deploy staled (measure semantics changed, shape unchanged) was patched
    over only the last N hours, stamped with the NEW deployed pointer, and flipped
    back to ``fresh``. ``artifact_built_for_current`` then accepted it and the
    matcher served a table mixing old-definition and new-definition rows.

    The refusal predicate is the SAME one the aggregate incremental path uses
    (``shared.artifact_incremental_gate``), so the two families cannot drift.

    The standalone ``entry_status`` check ahead of it is a pocket-specific
    TIGHTENING layered on top of that shared rule, not a competing encoding of
    it. ``full_rebuild_required`` short-circuits when the model is undeployed —
    the aggregate guard's exact semantics, preserved deliberately — which would
    otherwise let a ``stale``/``failed``/``invalidating`` pocket of an undeployed
    model be patched, leaving a physically mixed table on disk labelled
    ``fresh``. That is not a wrong number (the binder 409s every query against an
    undeployed model, and the build stamps a NULL binding the version gate
    refuses the moment the model IS deployed), but ``failed`` in particular can
    mean a previous run left the table half-mutated, and a cache the operator is
    shown as ``fresh`` should not silently be one. One line removes the only
    fail-open shape in the predicate; the aggregate side is untouched because it
    has no equivalent reachable path.
    """
    if not incremental_column:
        return False
    if refresh_mode not in _INCREMENTAL_REFRESH_MODES:
        return False
    if not table_exists:
        return False
    if window_start is None:
        return False
    if build_state.entry_status != POCKET_STATUS_FRESH:
        return False
    # ``artifact_is_stale`` below is REDUNDANT BY CONSTRUCTION — the line above
    # has already returned for every non-fresh pocket, so it is always False
    # here. It is passed anyway, deliberately: if the standalone check is ever
    # moved or narrowed, the shared predicate still evaluates the staleness limb
    # rather than silently losing it. Do NOT "simplify" by deleting the
    # standalone check because this argument looks like it covers the case — it
    # does not, because ``full_rebuild_required`` short-circuits on an undeployed
    # model (round-2 review finding 2; two tests pin this).
    if full_rebuild_required(
        artifact_is_stale=(build_state.entry_status != POCKET_STATUS_FRESH),
        built_for_version_id=build_state.built_for_version_id,
        built_for_epoch=build_state.built_for_epoch,
        deployed_version_id=build_state.deployed_version_id,
        deploy_epoch=build_state.deploy_epoch,
    ):
        return False
    return True


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


class PocketMaxRowsExceededError(ValueError):
    """Raised when a pocket's ACTUAL materialised row count exceeds the
    ``pocket.max_rows`` ceiling.

    Bug-6110: the optimizer candidate analyzer only screens the *estimated*
    average rows of the recurring query. A build can still exceed the ceiling
    (data skew/growth, a wider predicate slice than the sampled misses), so the
    ceiling must also be enforced against the real cached cardinality at
    admission. The oversized table is evicted and the pocket marked failed so it
    never enters the matcher pool.
    """


def _mint_service_token(tenant_id: str) -> str:
    """Mint a short-lived JWT for internal service-to-service calls."""
    return create_service_access_token(
        principal="pocket-refresh",
        tenant_id=tenant_id,
        role="system_admin",
        ttl_minutes=5,
        scopes=[SCOPE_POCKET_REFRESH],
    )


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


# Postgres/Redshift signed 8-byte integer range. A Python int is unbounded, so a
# value outside this range cannot be stored as BIGINT — it overflows on INSERT.
# Such a value must be typed NUMERIC (arbitrary precision) instead (Bug-7006:
# the whole point is that a wider value must never truncate/overflow the column).
_BIGINT_MIN = -(2 ** 63)
_BIGINT_MAX = 2 ** 63 - 1


def _infer_pg_type(value: object) -> str:
    if value is None:
        return "TEXT"
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, int):
        # A Python int beyond signed-64-bit range overflows Postgres BIGINT;
        # widen to NUMERIC so the real value is stored losslessly.
        if _BIGINT_MIN <= value <= _BIGINT_MAX:
            return "BIGINT"
        return "NUMERIC"
    if isinstance(value, float):
        return "DOUBLE PRECISION"
    if isinstance(value, Decimal):
        return "NUMERIC"
    if isinstance(value, datetime):
        return "TIMESTAMPTZ"
    if isinstance(value, date):
        return "DATE"
    return "TEXT"


# Bug-7006: widening lattice for value-inferred pocket staging column types.
#
# The old inference picked the type of the FIRST non-null value in the first
# 10 rows and never revisited it. A column whose later/wider rows exceed that
# first guess (e.g. row 1 = small int -> BIGINT, then a Decimal median or a
# value wider than int64 arrives) then materialised with a physical type that
# truncates or rejects the real data on a cross-DB pocket build.
#
# Instead every observed non-null value contributes; the column type is the
# LEAST-restrictive (widest) type that can hold every value seen so far. The
# order below is a partial lattice: a widen never NARROWS a type that already
# admits a real value. Numbers widen BOOLEAN < BIGINT < NUMERIC < DOUBLE
# PRECISION (NUMERIC before DOUBLE PRECISION keeps exact decimals exact; a mix
# of NUMERIC and float widens to DOUBLE PRECISION). DATE widens to TIMESTAMPTZ
# when a timestamp is also seen. Any incompatible mix (number + text, date +
# number, etc.) widens to TEXT — the universal top — so no real value is ever
# rejected. TEXT is the absorbing top of the lattice.
_WIDEN_RANK: dict[str, int] = {
    "BOOLEAN": 0,
    "BIGINT": 1,
    "NUMERIC": 2,
    "DOUBLE PRECISION": 3,
    "DATE": 1,
    "TIMESTAMPTZ": 2,
    "TEXT": 99,
}
_NUMERIC_FAMILY = frozenset({"BOOLEAN", "BIGINT", "NUMERIC", "DOUBLE PRECISION"})
_TEMPORAL_FAMILY = frozenset({"DATE", "TIMESTAMPTZ"})


def _widen_pg_type(current: str | None, incoming: str) -> str:
    """Return the widest type that admits both ``current`` and ``incoming``.

    Never narrows a type that already holds a real value: the result always
    admits every value either input admitted. Cross-family mixes collapse to
    TEXT (the absorbing top), so no observed value is ever rejected.
    """
    if current is None:
        return incoming
    if current == incoming:
        return current
    if "TEXT" in (current, incoming):
        return "TEXT"
    # Within a single family, take the higher rank (wider) member.
    if current in _NUMERIC_FAMILY and incoming in _NUMERIC_FAMILY:
        return current if _WIDEN_RANK[current] >= _WIDEN_RANK[incoming] else incoming
    if current in _TEMPORAL_FAMILY and incoming in _TEMPORAL_FAMILY:
        return current if _WIDEN_RANK[current] >= _WIDEN_RANK[incoming] else incoming
    # Cross-family (e.g. number + date, number + text): only TEXT holds both.
    return "TEXT"


def _infer_col_types(
    rows: list[dict],
    col_names: list[str],
    declared_types: dict[str, str] | None = None,
    prior: list[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """Infer staging column types with a WIDENING lattice (Bug-7006).

    - Every non-null value in ``rows`` (not just the first 10) contributes; the
      resulting type is the widest that admits all observed values, so a later
      or wider value can only WIDEN the type, never leave it too narrow.
    - ``prior`` carries the types resolved from earlier batches so widening is
      monotonic across the whole stream, not just within one batch.
    - ``declared_types`` supplies the AUTHORITATIVE per-column type (from the
      model/router column definitions). A column that is all-null in the sampled
      rows defers to its declared type instead of collapsing to TEXT, so a
      genuinely numeric/date column with no sampled value still materialises with
      the correct physical type. Declared types are a floor for null-only columns
      only; an observed value still widens above the declaration if the data is
      genuinely wider (fail-open against under-declared metadata).
    """
    declared_types = declared_types or {}
    prior_by_col: dict[str, str] = dict(prior or [])
    result: list[tuple[str, str]] = []
    for c in col_names:
        # Seed with any type already resolved from earlier batches. Widening is
        # monotonic from this seed, so a column with a prior type and no value in
        # this batch simply keeps its prior type (never narrows).
        pg_type: str | None = prior_by_col.get(c)
        for row in rows:
            v = row.get(c)
            if v is None:
                continue
            pg_type = _widen_pg_type(pg_type, _infer_pg_type(v))
            if pg_type == "TEXT":
                break  # TEXT is the absorbing top — no wider type exists.
        if pg_type is None:
            # No value ever seen for this column in this or prior batches:
            # defer to the authoritative declared type, else TEXT.
            pg_type = declared_types.get(c) or "TEXT"
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


async def _table_columns_via(sc, schema: str, table: str) -> list[tuple[str, str]]:
    """``(name, data_type)`` for the cached table's columns, in ORDINAL order.

    Bug-8699: the only branch-independent record of what a pocket table exposes
    (the same reasoning ``shared/pocket/row_manifest.py`` documents). The
    identity key the incremental DELETE matches on must be present here
    verbatim, never assumed from model metadata.

    Ordered, not a set, because the write-back also needs it: an explicit
    by-name column list is what stops a positional ``SELECT *`` INSERT from
    silently shifting every value one column across.

    The DATA TYPE is read in the same round trip because the delta's window
    expression depends on the watermark column's type (a DATE has no time part,
    so the window has to be truncated to a day boundary or it excludes rows on
    the boundary day). The cache is a CTAS of the same SELECT the delta is
    derived from, so the cached column's type IS the source expression's type.
    """
    rows = await sc.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = $1 AND table_name = $2 "
        "ORDER BY ordinal_position",
        schema, table,
    )
    return [
        (str(r["column_name"]), str(r.get("data_type") or ""))
        for r in (rows or [])
        if isinstance(r, dict) and r.get("column_name")
    ]


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


@asynccontextmanager
async def _isolated_tenant_session(tenant_id: str):
    """Yield a fresh, short-lived ``AsyncSession`` on the tenant's own engine.

    Bug-8114 (B-02): the refresh runs on a SHARED session (the scheduler sweep
    reuses one session across every pocket). Best-effort alerting must NEVER be
    handed that session, because ``dispatch_alert`` SWALLOWS its own dispatcher-
    level DB failures and returns normally (``_record_delivery`` catches a failed
    commit without rolling back; route-level failures are caught internally; a
    no-route dispatch returns after DB reads). Any of those on a deadlock/
    connection error would leave the shared session in pending-rollback while
    dispatch returns "fine", and the NEXT pocket's refresh — which reuses that
    same session — fails spuriously. Running record/resolve/dispatch on a
    throwaway session opened here means a dispatcher-swallowed failure poisons
    only this session; ``__aexit__`` rolls it back and closes it, and the
    refresh session is never referenced, so it stays transaction-clean
    regardless of what the dispatcher does.
    """
    from shared.db.session import get_tenant_session_factory

    factory = await get_tenant_session_factory(tenant_id)
    async with factory() as session:
        # Scope get_setting/tenant-cache lookups the same way get_tenant_db does.
        try:
            session.info["tenant_id"] = tenant_id
        except Exception:  # pragma: no cover - defensive
            pass
        yield session


async def _notify_pocket_refresh_failure(
    tenant_id: str | None, pocket: PocketDefinition, error_text: str,
) -> None:
    """Bug-8114: mirror the aggregate refresh failure alerting contract
    (``scheduler/src/jobs/full_refresh.py``, the ``except Exception as exc:``
    block around its ``record_alert``/``dispatch_alert`` calls) for a pocket
    refresh failure — the in-app ``ModelAlert`` (Model Health tab) AND the
    outbound notification-channel dispatch.

    Called from EVERY failure-exit point of ``refresh_pocket_definition``
    (query-validation, connector-combo mismatch, credential/build-binding
    capture, and the terminal materialisation except) so an operator sees
    the same signal regardless of which stage failed.

    B-02 (transactional safety): the caller has ALREADY committed the
    authoritative ``run``/``pocket`` failure state before invoking this, and
    both halves below run on their OWN isolated short-lived tenant session (see
    ``_isolated_tenant_session``), never the shared refresh session. Alerting
    therefore can never fail, roll back, prematurely commit, or otherwise alter
    the refresh outcome, and can never leave the shared sweep session unusable
    for the next pocket — even though ``dispatch_alert`` swallows its own DB
    failures. The two halves are independent best-effort units, mirroring the
    aggregate contract.
    """
    if not tenant_id:
        # No tenant identity to open an isolated session — skip rather than
        # borrow the shared refresh session (which the whole design avoids).
        # Production callers always pass tenant_id; this is a defensive guard.
        logger.warning(
            "Pocket %s: no tenant_id available; skipping best-effort refresh-"
            "failure alert", getattr(pocket, "id", None),
        )
        return

    # Half 1 — in-app ModelAlert (Model Health tab) on an isolated session.
    try:
        from shared.semantic.model_alerts import (
            CATEGORY_REFRESH_FAILURE,
            OBJECT_POCKET,
            SEVERITY_ERROR,
            record_alert,
        )

        async with _isolated_tenant_session(tenant_id) as alert_db:
            await record_alert(
                alert_db,
                model_id=pocket.model_id,
                severity=SEVERITY_ERROR,
                category=CATEGORY_REFRESH_FAILURE,
                title=f"Pocket refresh failed: {pocket.physical_table_name}",
                detail=error_text[:1000],
                related_object_type=OBJECT_POCKET,
                related_object_id=pocket.id,
            )
            await alert_db.commit()
    except Exception as alert_exc:  # pragma: no cover - defensive
        logger.warning(
            "Alert recording failed for pocket %s: %s", pocket.id, alert_exc,
        )

    # Half 2 — outbound notification dispatch on its OWN isolated session.
    try:
        from shared.alerting.dispatcher import dispatch_alert
        from sqlalchemy import select as _select

        async with _isolated_tenant_session(tenant_id) as dispatch_db:
            project_id = (
                await dispatch_db.execute(
                    _select(Model.project_id).where(Model.id == pocket.model_id)
                )
            ).scalar_one_or_none()
            await dispatch_alert(
                dispatch_db,
                event_type="pocket_refresh_failure",
                project_id=project_id,
                # F-022-03: distinct pocket failures are distinct incidents.
                incident_key=f"pocket_refresh_failure:{pocket.id}",
                subject=f"Refresh failed: {pocket.physical_table_name}",
                body_html=(
                    f"<h2>Pocket Refresh Failed</h2>"
                    f"<p><strong>Table:</strong> {pocket.physical_table_name}</p>"
                    f"<p><strong>Error:</strong> {error_text[:500]}</p>"
                ),
                body_text=(
                    f"Pocket refresh failed for {pocket.physical_table_name}: "
                    f"{error_text[:500]}"
                ),
                slack_text=(
                    f"Pocket refresh failed for `{pocket.physical_table_name}`: "
                    f"{error_text[:200]}"
                ),
            )
    except Exception as notify_exc:
        logger.warning(
            "Alert notification failed for pocket %s: %s", pocket.id, notify_exc,
        )


async def _resolve_pocket_refresh_alert(
    tenant_id: str | None, pocket: PocketDefinition,
) -> None:
    """Bug-8114: mirror ``full_refresh.py``'s ``resolve_alert`` call
    (lines ~1184-1195) — a genuinely-successful refresh clears the pocket's
    open refresh-failure ``ModelAlert`` (if any), the same way a successful
    aggregate refresh does for ``OBJECT_AGGREGATE``.

    B-01: called ONLY when the refresh genuinely succeeded and the pocket ends
    ``fresh`` — never on a serving-refusal or superseded completion, which
    leave the pocket non-serving and MUST keep the operator's open failure
    alert until a later rebuild returns it to service.

    B-02(c): the caller has ALREADY committed the fresh pocket state before
    invoking this, and the resolve runs on its OWN isolated short-lived tenant
    session (never the shared refresh session), so resolution can never fail or
    alter a refresh that already physically succeeded, nor poison the shared
    sweep session.
    """
    if not tenant_id:
        logger.warning(
            "Pocket %s: no tenant_id available; skipping refresh-alert resolve",
            getattr(pocket, "id", None),
        )
        return

    try:
        from shared.semantic.model_alerts import (
            CATEGORY_REFRESH_FAILURE,
            OBJECT_POCKET,
            resolve_alert,
        )

        async with _isolated_tenant_session(tenant_id) as alert_db:
            await resolve_alert(
                alert_db,
                model_id=pocket.model_id,
                category=CATEGORY_REFRESH_FAILURE,
                related_object_type=OBJECT_POCKET,
                related_object_id=pocket.id,
            )
            await alert_db.commit()
    except Exception as alert_exc:  # pragma: no cover - defensive
        logger.warning(
            "Alert resolve failed for pocket %s: %s", pocket.id, alert_exc,
        )


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

    # Bug-8114 (B-02): the tenant identity best-effort alerting uses to open its
    # OWN isolated session (never the shared refresh session). Production callers
    # always pass ``tenant_id``; fall back to the refresh session's own tenant
    # info (set by ``get_tenant_db``) when they don't.
    _alert_tenant_id = tenant_id
    if not _alert_tenant_id and isinstance(getattr(db, "info", None), dict):
        _alert_tenant_id = db.info.get("tenant_id")

    # F-005-06: the materialisation (the SELECT that fills the cache) MUST run
    # under the system service identity, never the caller's token. Under the
    # caller's token the query-router wraps the rewrite with that caller's
    # row-security predicate, so the pocket would cache only the refresher's
    # permitted rows — then served to every reader. The user/caller token is
    # used ONLY for the pre-validate call below (validation parses + checks the
    # subset grammar; it returns no data rows, so it cannot leak rows).
    service_token = _mint_service_token(tenant_id or "system")

    # F-005-23: adopt a pre-created queued run (async manual path) when supplied.
    existing_run: PocketRefreshRun | None = None
    if existing_run_id is not None:
        existing_run = await db.get(PocketRefreshRun, existing_run_id)
        if existing_run is None or existing_run.pocket_definition_id != pocket.id:
            raise ValueError(
                f"PocketRefreshRun {existing_run_id} not found for pocket {pocket_id}"
            )

    try:
        async with pocket_refresh_lock(db, pocket.id):
            # Scheduler sessions are reused with expire_on_commit=False. Refresh
            # the root so defining_sql, target location, refresh policy, status
            # and build binding cannot come from a prior identity-map load when
            # this physical build starts.
            #
            # Bug-8431: taken INSIDE the lock, not before it. The status and the
            # recorded build binding read here are the inputs to the
            # incremental-vs-full decision below, so they must describe the
            # pocket as it stands once this build has exclusive ownership of it —
            # not as it stood before a concurrent refresh or a deploy's staling
            # pass committed.
            #
            # Guarded for the same reason as the ``db.refresh(model)`` below
            # (CLAUDE.md shared-primitive discipline: the two call sites in this
            # function have the identical exposure and must be enumerated
            # together). The sweep loads pocket ids, then takes the lock one at
            # a time, so a pocket retired or cascade-deleted in between makes
            # this raise. Turn it into the same explicit error the not-found
            # branch above raises, rather than an opaque ORM exception escaping
            # with no run row recorded.
            try:
                await db.refresh(pocket)
            except ObjectDeletedError as exc:
                raise ValueError(
                    f"PocketDefinition {pocket_id} was deleted while its "
                    f"refresh was starting"
                ) from exc

            # Bug-8431: freeze the pocket's ENTRY status before anything in this
            # function mutates it. ``failed`` is rewritten to ``stale`` below and
            # the whole row is flipped to ``invalidating`` before materialisation
            # starts, so by the time the incremental branch is chosen the live
            # attribute no longer carries the signal the guard needs.
            _entry_status = getattr(pocket, "status", None)

            # F-005-03: resolve the model slug so the shared subset grammar can
            # pin the FROM clause to this model (the optimizer/scheduled paths
            # reach this chokepoint without a model_slug otherwise).
            #
            # Loaded INSIDE the lock (Bug-8412 / Fable R1 finding 5): the same row
            # supplies the build-start deployed pointer captured below, and a read
            # taken while another refresh still held the lock would not describe
            # this build's starting state.
            model_slug = ""
            model = await db.get(Model, pocket.model_id)
            if model is not None:
                # Bug-8479 symmetry (DeepSeek gate note on Bug-8431): ``db.get``
                # can return an identity-map copy loaded earlier in the same
                # sweep session (``expire_on_commit=False``, one session across
                # every pocket). Force committed state, so the slug this build
                # validates its FROM clause against is the model's real current
                # slug and not a pre-rename copy that would fail validation and
                # park the pocket in ``failed``.
                #
                # A Model deleted after the identity map cached it makes
                # ``db.refresh`` raise where the previous ORM-attribute read
                # silently used the stale copy. That escapes this function with
                # no run row recorded, so it is caught and treated as
                # "undeployed": the build then stamps a NULL binding the version
                # gate refuses, which is the same fail-closed outcome the delete
                # would have produced anyway (the pocket is cascade-deleted with
                # its model).
                try:
                    await db.refresh(model)
                except ObjectDeletedError:
                    logger.warning(
                        "Pocket %s: model %s was deleted while this refresh was "
                        "starting; continuing with a NULL build binding, which "
                        "the version gate refuses.",
                        pocket.id, pocket.model_id,
                    )
                    model = None
            if model is not None:
                model_slug = (getattr(model, "slug", "") or "").lower()

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
                # B-02(a): commit the authoritative failure state FIRST, then
                # alert best-effort. Alerting can never roll back or alter an
                # outcome that is already durable.
                await db.commit()
                await db.refresh(failed_run)
                # Bug-8114: query-validation is a genuine refresh-failure exit
                # (the pocket stops refreshing), so it must alert exactly like
                # every other failure exit in this function.
                await _notify_pocket_refresh_failure(_alert_tenant_id, pocket, invalid_reason)
                return failed_run
            # NOTE: the ``failed -> stale`` entry rewrite used to sit HERE. It
            # was moved to just after the Bug-8827 committed-status read below,
            # because it dirties the pocket row and the very next ``db.execute``
            # (``capture_build_binding``, autoflush ON) writes it — so the
            # committed read would observe THIS session's own uncommitted
            # ``stale`` instead of committed truth, log a "written by another
            # party" warning on every retry of a failed pocket, and weaken its
            # ``FOR UPDATE`` to a self-lock. Do not move it back.

            # Bug-8412: freeze the deployed pointer this materialisation will
            # be built FOR into plain scalars. The stamp on success uses THIS
            # value, never a read taken at write time — a write-time read would
            # record the epoch of a deploy/revert that landed mid-build and let
            # rows built from the superseded definition pass the version gate.
            #
            # Captured HERE, not before the lock (Fable R1 finding 5): a build
            # that waited on the pocket lock or was rejected by validation never
            # materialises anything, and a pointer captured before that wait
            # would look superseded on completion and trigger a needless rebuild.
            # This point is inside the lock, after validation, and before the
            # first physical change.
            #
            # Bug-8479 symmetry: taken with the canonical
            # ``capture_build_binding`` (a direct two-column SELECT), NOT
            # ``binding_from_model(model)``. The aggregate writers were moved off
            # the ORM-object read for exactly this reason, and DeepSeek's
            # external gate on Bug-8431 flagged the pocket path as the remaining
            # asymmetry. The ORM read was only correct while nothing had loaded
            # this Model earlier on the shared sweep session; when something had,
            # the captured pointer could pre-date a deploy, the incremental gate
            # would compare a stale pointer against a stale binding and see a
            # match, and the leg would patch a superseded pocket. That was
            # fail-closed downstream (``apply_build_binding``'s supersession
            # probe re-stales it, so the matcher refuses and the next sweep
            # rebuilds in full), but it burned a build and left a physically
            # mixed table on disk. A direct read removes the action-at-a-distance
            # precondition entirely.
            _build_binding = await capture_build_binding(db, pocket.model_id)

            # Bug-8431: the complete, immutable input set for the
            # incremental-vs-full decision, snapshotted here — before
            # ``pocket.status`` is flipped to ``invalidating`` and before any
            # physical change. The live pointer is the SAME captured value that
            # will be stamped on success, which is what keeps the decision and
            # the stamp self-consistent: if this capture is older than committed
            # truth, the guard may allow an incremental slice, but the stamp then
            # records that same older pointer and ``apply_build_binding``'s FRESH
            # supersession probe re-stales the pocket, so the matcher refuses it
            # (a wasted cycle, never a wrong number). See
            # ``shared.artifact_incremental_gate``.
            _build_state = PocketBuildState(
                entry_status=_entry_status,
                built_for_version_id=getattr(pocket, "built_for_version_id", None),
                built_for_epoch=getattr(pocket, "built_for_epoch", None),
                deployed_version_id=_build_binding.version_id,
                deploy_epoch=_build_binding.epoch,
            )

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
                # B-02(a): commit the authoritative failure state FIRST, then
                # alert best-effort.
                await db.commit()
                await db.refresh(failed_run)
                # Bug-8114: a rejected connector combo is a genuine
                # refresh-failure exit — alert exactly like every other one.
                await _notify_pocket_refresh_failure(_alert_tenant_id, pocket, combo_error)
                return failed_run

            # Bug-8822: create (or adopt) the run row BEFORE anything that can
            # fail with a credential/capture error. ``capture_target_build_binding``
            # and ``capture_source_build_binding`` decrypt and resolve connection
            # credentials; an undecryptable or unresolvable endpoint there escapes
            # with no run row to record the failure, leaving a pre-created queued
            # run stuck forever and silent. Creating the run first means the except
            # handler below can always stamp it failed.
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

            # Bug-8807: freeze the RESOLVED routing identity of BOTH databases
            # this build uses, before the first physical change — the same
            # capture the three aggregate writers take (Bug-8481 target,
            # Bug-8602 source), for the same reason and at the same moment.
            #
            # The captures are of the CONNECTION OBJECTS THIS BUILD ACTUALLY
            # DIALS, resolved a few lines above. That is what makes the
            # re-proof at finalisation meaningful: a re-point that committed
            # before this build started but was served to us out of the session
            # identity map (tenant sessions are ``expire_on_commit=False`` and
            # the sweep reuses one session across every pocket) produces a
            # capture that no longer matches committed truth, so the completed
            # build is refused rather than published as fresh.
            #
            # Taken AFTER the connector-combo gate — a build rejected there does
            # no remote work and must not pay for these resolutions either — and
            # BEFORE the ``invalidating`` commit, because that commit is itself
            # a write that can clobber an invalidation (see the finalisation
            # block below for why the status re-read alone cannot cover this
            # window).
            #
            # Bug-8822: wrapped so a credential/capture error stamps the
            # already-created run row as failed instead of escaping with no
            # record.
            try:
                _target_build_binding = await capture_target_build_binding(
                    target, target_conn, tenant_session=db,
                )
                _source_build_binding = await capture_source_build_binding(
                    pocket.model_id, source_conn, tenant_session=db,
                )
            except Exception as _capture_err:
                now = datetime.now(timezone.utc)
                run.status = "failed"
                run.completed_at = now
                run.error_message = str(_capture_err)[:1000]
                pocket.status = "failed"
                pocket.failure_reason = str(_capture_err)[:1000]
                clear_pocket_row_manifest(pocket)
                # B-02(a): commit the authoritative failure state FIRST, then
                # alert best-effort.
                await db.commit()
                await db.refresh(run)
                # Bug-8114: a credential/build-binding capture failure is a
                # genuine refresh-failure exit — alert exactly like every
                # other one.
                await _notify_pocket_refresh_failure(_alert_tenant_id, pocket, str(_capture_err))
                return run

            # Bug-8700 / Bug-8699: the two remaining incremental preconditions
            # that need control-plane state, resolved as plain values so the
            # materialisation driver makes no policy decision of its own.
            #
            # Placed HERE deliberately: inside the lock, after the connector
            # combo gate (a build that fails it does no remote work and must not
            # pay for these queries either), and AFTER this build's own run row
            # exists (Bug-8822). ``resolve_incremental_window_start`` queries
            # only completed runs, so this new running row cannot anchor to
            # itself — the same invariant the previous ordering provided.
            _window_start: datetime | None = None
            _key_candidates: tuple[str, ...] = ()
            if pocket.incremental_column and refresh_mode in _INCREMENTAL_REFRESH_MODES:
                _window_start = await resolve_incremental_window_start(
                    db,
                    pocket_id=pocket.id,
                    lookback_hours=pocket.incremental_lookback_hours,
                )
                _key_candidates = await resolve_row_identity_candidates(
                    db, model_id=pocket.model_id
                )

            # Bug-8827: RE-READ the committed status under a row lock, here, and
            # let it override the pre-lock capture as the authoritative entry
            # status.
            #
            # The line below is a second unconditional clobber of the same kind
            # Bug-8807 fixed at completion, and it is worse, because it erases
            # the ONE input ``should_refresh_incrementally`` consumes. Everything
            # between ``_entry_status``'s capture and this point — the router
            # /validate HTTP round trip, the model/target/connection resolution,
            # the binding captures — is time in which a staler can commit. If it
            # does, the write below deletes the evidence, the incremental gate
            # still sees the pre-lock ``fresh``, and the delta leg patches only
            # the look-back window over rows the previous definition produced.
            # The result is a physically MIXED table published ``fresh`` under
            # the NEW definition's fingerprint: Bug-8431's defect shape, through
            # a different door, and a live wrong number rather than a stale one.
            #
            # The re-read is ``FOR UPDATE`` for the same reason the finalisation
            # one is: a staler whose UPDATE is issued-but-uncommitted must be
            # waited for, not read past. Taken while this transaction holds NO
            # control-plane row (they are resolved above and released; the
            # finalisation locks come much later), so it cannot cycle with a
            # control-plane writer, which always locks its own row first.
            #
            # A staler observed here does NOT abort the build. A FULL rebuild
            # from the current definition legitimately clears staleness — that is
            # what the sweep would do next anyway. Only the incremental SHORTCUT
            # is unsound, and this is exactly what withdraws it.
            _committed_entry_status = await read_committed_pocket_status(
                db, pocket.id, lock_for_finalization=True,
            )
            if _committed_entry_status != _entry_status:
                logger.warning(
                    "Bug-8827: pocket %s was written by another party while this "
                    "refresh was starting (entry=%r committed=%r); rebuilding in "
                    "FULL so no windowed patch is taken over a superseded cache",
                    pocket.id, _entry_status, _committed_entry_status,
                )
            _build_state = replace(
                _build_state, entry_status=_committed_entry_status
            )

            # F-005-12 / F-005-17 (Bug-2254/2259): the lifecycle has no "invalid"
            # state — the DB CHECK (migration 0022) allows only
            # fresh|stale|invalidating|failed, and drift lands in "failed". The
            # old ("invalid", "failed") tuple carried a dead "invalid" branch;
            # "failed" is the only reachable case here.
            #
            # Applied AFTER the committed read above (round-4 review): this is a
            # write to the pocket row, and the read must see committed truth,
            # not this session's own pending rewrite. Both values are non-fresh,
            # so the incremental gate's verdict is identical either way — what
            # the order protects is the honesty of the race warning and the
            # meaning of the row lock.
            if pocket.status == "failed":
                pocket.status = POCKET_STATUS_STALE
                pocket.failure_reason = None

            # Bug-8807: the SAME constant the finalisation guard compares the
            # committed status against, so a rename here cannot silently make
            # every completion look like someone else's write (and therefore
            # refuse every pocket forever) or — worse, if the comparison were
            # inverted — accept every one.
            pocket.status = POCKET_STATUS_INVALIDATING
            await db.commit()
            await db.refresh(run)
            await db.refresh(pocket)

            target_schema, target_table = _resolve_target_location(pocket, target)
            table_ref = _quoted_table_ref(target_schema, target_table, target_connector)

            # Bug-8807: the finalisation verdict, read by the manifest block
            # BELOW the try/except. ``None`` means "no refusal known", which on
            # the failure path is correct by construction — a failed refresh is
            # already non-serving and clears its manifest in the handler.
            _serving_refusal: str | None = None
            # B-01/B-02: the terminal materialisation error, captured in the
            # except below and alerted AFTER the authoritative failure state is
            # committed (never from inside the except, which runs before the
            # commit). ``None`` means the materialisation did not raise.
            _materialisation_error: str | None = None

            try:
                # F-005-06: always the service token here — never the caller's token —
                # so the rewrite the executor materialises carries no row-security
                # filter. _get_rewritten_sql additionally fails closed if a wildcard
                # rule wrapped the SELECT anyway (PocketRowSecurityLeakError).
                token = service_token

                if cross_db:
                    row_count, storage_bytes = await _refresh_cross_db(
                        pocket, target_conn, target_schema, target_table, table_ref,
                        token, db,
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
                        build_state=_build_state,
                        window_start=_window_start,
                        key_candidates=_key_candidates,
                    )

                # Bug-6110: enforce the pocket.max_rows ceiling against the
                # ACTUAL materialised cardinality, not the pre-build estimate.
                # The optimizer candidate analyzer screens only the estimated
                # average rows of the recurring query; a build can still exceed
                # the ceiling (skew, growth, a wider predicate slice than the
                # sampled misses). An oversized pocket must never enter the
                # matcher pool (it substitutes for a full-table scan and is
                # served to every reader), so evict the freshly built table and
                # fail the run — fail closed.
                # Bug-6600: resolve the ceiling with a system_session so an
                # admin-set SYSTEM-level pocket.max_rows is honoured when no
                # per-model override exists. Both this admission gate AND the
                # optimizer candidate analyzer now pass system_session, keeping
                # the two sides consistent (no build/evict churn).
                async with SystemSessionLocal() as sys_db:
                    max_rows = int(
                        await get_setting(
                            "pocket.max_rows",
                            system_session=sys_db,
                            tenant_session=db,
                            model_id=pocket.model_id,
                        )
                    )
                # Bug-6840: fail-CLOSED when row_count is unknown (None).
                # An unknown cardinality means the materialisation driver could
                # not determine how many rows landed — treat that as exceeding
                # the ceiling rather than silently admitting an unbounded cache.
                # max_rows <= 0 means "no ceiling" (disabled).
                if max_rows > 0 and (row_count is None or row_count > max_rows):
                    # Stamp the location so eviction targets the table we just
                    # built, then drop it before marking the pocket failed.
                    pocket.target_schema = target_schema
                    pocket.physical_table_name = target_table
                    try:
                        await drop_pocket_storage(pocket, db)
                    except Exception:  # noqa: BLE001 - best-effort eviction
                        logger.exception(
                            "Bug-6110: failed to evict oversized pocket %s storage; "
                            "still marking the pocket failed",
                            pocket.id,
                        )
                    # Bug-6840: distinct messages for unknown vs exceeded.
                    if row_count is None:
                        reason = (
                            f"Pocket materialised an unknown number of rows "
                            f"(row count unavailable); with pocket.max_rows="
                            f"{max_rows} the admission gate fails closed. The "
                            f"cache was evicted and the pocket marked failed."
                        )
                    else:
                        reason = (
                            f"Pocket materialised {row_count} rows, exceeding "
                            f"the pocket.max_rows ceiling of {max_rows}; the "
                            f"cache was evicted and the pocket marked failed. "
                            f"Narrow the pocket's defining query or raise "
                            f"pocket.max_rows."
                        )
                    raise PocketMaxRowsExceededError(reason)

                # Bug-8807: the finalisation protocol the three aggregate
                # writers already run, applied to the pocket writer — the one
                # artifact finalisation path that never had it.
                #
                # THIS CALL MUST STAY AHEAD OF EVERY ``run``/``pocket``
                # MUTATION BELOW. It takes the control-plane row locks
                # (connections, target, the model's sources) in the one fixed
                # order every control-plane writer agrees with, and each of
                # those writers locks its own row and then UPDATEs the artifacts
                # hanging off it. Dirtying the pocket first and asking for those
                # rows afterwards closes a deadlock cycle with all of them; the
                # aggregate side reproduced exactly that live, twice.
                #
                # What it buys, and why one leg is not enough:
                #
                # * the committed-status re-read (under ``FOR UPDATE``) makes an
                #   invalidation that landed mid-build DURABLE. Without it this
                #   function wrote ``fresh`` unconditionally and silently undid
                #   every one of the seven independent stalers — routing
                #   invalidation, deploy/version staling, schema drift, a
                #   definition edit, a snapshot revert, the TTL/event sweep, and
                #   the query-time missing-table flag. A non-locking read would
                #   not do: the invalidator's UPDATE can be issued and
                #   uncommitted while the read still sees ``invalidating``, and
                #   the write below would then queue behind its row lock and
                #   overwrite ``stale`` with ``fresh``.
                # * the two binding re-proofs cover the window the status leg
                #   structurally cannot: a re-point that lands BEFORE this
                #   refresh committed ``invalidating`` is clobbered by that
                #   commit, and the build can still have dialled a pre-repoint
                #   copy of the connection out of the session identity map.
                _finalization = await read_pocket_finalization_state(
                    db,
                    pocket_id=pocket.id,
                    target_binding=_target_build_binding,
                    source_binding=_source_build_binding,
                    connection_ids=(target_conn.id, source_conn.id),
                    target_id=pocket.target_id,
                    model_id=pocket.model_id,
                )
                _serving_refusal = resolve_pocket_serving_refusal(_finalization)

                now = datetime.now(timezone.utc)
                # Bug-8700: the SAME constant the window anchor filters on, so a
                # rename here cannot silently make every future incremental run
                # anchorless (and therefore a permanent full rebuild).
                #
                # The RUN is completed either way: the physical build really did
                # succeed, and the delta-window anchor must advance with it. It
                # is the POCKET that is refused.
                run.status = POCKET_RUN_STATUS_COMPLETED
                run.completed_at = now
                run.rows_written = row_count
                run.bytes_processed = storage_bytes

                # B-01: the open refresh-failure ModelAlert is resolved ONLY on
                # the true-success path — after every finalisation check has
                # passed, the pocket has committed ``fresh``, and it is genuinely
                # back in service. It is NOT resolved here: a completed physical
                # build is not the same as a pocket that returned to service. A
                # serving refusal (below) or a superseded build binding
                # (``apply_build_binding``) leaves the pocket non-serving, and
                # the operator's failure alert must stay open until a later
                # rebuild succeeds. See the post-commit resolve after this block.
                if _serving_refusal is None:
                    pocket.status = POCKET_STATUS_FRESH
                    pocket.failure_reason = None
                else:
                    # Byte-identical to what ``_invalidate_artifacts`` writes for
                    # a pocket, so the refresh sweep picks it up for rebuild and
                    # the matcher refuses it in the meantime. The manifest and
                    # the liveness pointer are cleared below, in the same
                    # transaction, for the same reason.
                    pocket.status = POCKET_STATUS_STALE
                    pocket.failure_reason = _serving_refusal[:1000]
                    logger.warning(
                        "Bug-8807: pocket %s completed its build but is being "
                        "kept non-serving (committed_status=%r target_ok=%s "
                        "source_ok=%s) — %s",
                        pocket.id, _finalization.committed_status,
                        _finalization.target_binding_matches,
                        _finalization.source_binding_matches,
                        _serving_refusal,
                    )
                pocket.last_refresh_at = now
                pocket.target_schema = target_schema
                pocket.physical_table_name = target_table
                pocket.row_count = row_count
                pocket.storage_bytes = storage_bytes
                # F-013-03 / F-005-01 (Bug-8250): stamp the immutable
                # artifact-to-version binding atomically with the fresh
                # materialisation. This is written UNCONDITIONALLY here (unlike
                # row_manifest.deployed_version_id, which advance_artifact_manifest
                # only writes when the model has BIJECTION edges + passenger
                # materialisation). The matcher requires an exact match against
                # the model's current (deployed_version_id, deploy_epoch), so a
                # fresh pocket built under a previous definition must NOT serve
                # after a deploy/revert. For an undeployed model both stay NULL.
                #
                # Bug-8412: the stamp is the BUILD-START capture taken before the
                # materialisation, never a read taken here.
                _superseded = await apply_build_binding(db, pocket, _build_binding)
                if _superseded:
                    # A deploy/revert landed while this pocket was materialising.
                    # The stamped build-start binding makes the pocket matcher
                    # refuse it (fail closed); mark it stale so the refresh
                    # sweep rebuilds it rather than leaving it permanently
                    # unservable-but-"fresh".
                    pocket.status = POCKET_STATUS_STALE
                    # Bug-8826: clear the manifest and liveness pointer so the
                    # stale-state representation is byte-identical across every
                    # path (the finalisation refusal path and the control-plane
                    # invalidator both clear both). A superseded pocket with a
                    # manifest still present would carry a description of a table
                    # built under a previous definition.
                    pocket.row_manifest = None
                    pocket.active_refresh_run_id = None
                    logger.warning(
                        "Bug-8412: model %s was deployed/reverted while pocket %s "
                        "was materialising; stamped the build-start binding "
                        "(version=%s epoch=%s) and marked the pocket stale",
                        pocket.model_id, pocket.id,
                        _build_binding.version_id, _build_binding.epoch,
                    )
                # F-017-03 (Bug-7989): a fresh pocket materialisation is a data
                # refresh, so bump the model data-freshness epoch atomically in
                # the same transaction that commits the fresh pocket. The KPI
                # cache keys on data_epoch, invalidating stale entries on every
                # replica after the refresh (see shared.model_refresh_epoch).
                await bump_data_epoch(db, pocket.model_id)

            except Exception as exc:
                now = datetime.now(timezone.utc)
                run.status = "failed"
                run.completed_at = now
                run.error_message = str(exc)[:1000]

                pocket.status = "failed"
                pocket.failure_reason = str(exc)[:1000]
                # Bug-8392/Bug-8393 (fail closed): the failure can land AFTER the
                # physical table was already dropped, replaced or partially
                # rebuilt, so the previous build's ``row_manifest`` no longer
                # describes what is on disk. Drop the manifest and its liveness
                # pointer together, otherwise the query-router's RLS gate could
                # prove column coverage from a description of a table that no
                # longer exists.
                clear_pocket_row_manifest(pocket)

                # Bug-8114 / B-02(a): capture the error and alert AFTER the
                # authoritative failure state is committed below — not from
                # inside this except, which runs before ``db.commit()``. Alerting
                # from here would let a poisoned alert session break the very
                # commit that records the failure. The same in-app ModelAlert +
                # outbound dispatch_alert contract as every other failure-exit
                # point is applied post-commit.
                _materialisation_error = str(exc)

            await db.flush()  # ensure run.id is assigned before it is stamped

            # Derived-grain routing (Bug-7359, spec §7.6.3 / §8.6, Phase 3): on a
            # fresh pocket, verify carried edges over the COMPLETE built row
            # population and record them on the versioned ``row_manifest``, only
            # on a clean full-artifact check. This authorises no serving route by
            # itself; it records which attribute edges the build carries.
            #
            # Bug-8393: this call is NOT the row-manifest producer. It returns
            # early for a model with no enabled BIJECTION relationship and never
            # resolves materialised columns, so the authoritative manifest write
            # happens below for every completion the finalisation guard admits.
            #
            # Bug-8807: skipped entirely for a REFUSED completion. The manifest
            # is what admits a pocket under row security, and a refused build
            # either wrote its rows to a database the pocket no longer addresses
            # or was invalidated while it ran — so describing those rows would
            # be proving the columns of a table the platform must not serve.
            # ``clear_pocket_row_manifest`` (no run id) drops the manifest AND
            # the liveness pointer, which is exactly the pair
            # ``_invalidate_artifacts`` clears; the generation-guard invariant
            # that the pointer must ADVANCE applies only to a pocket returning
            # to ``fresh``, and this one is not.
            if run.status == POCKET_RUN_STATUS_COMPLETED and _serving_refusal is not None:
                clear_pocket_row_manifest(pocket)
            elif run.status == POCKET_RUN_STATUS_COMPLETED:
                try:
                    from shared.semantic.attribute_relationship_deploy_verify import (
                        advance_artifact_manifest,
                    )

                    await advance_artifact_manifest(
                        db=db, model_id=pocket.model_id, artifact=pocket,
                        artifact_kind="POCKET", artifact_refresh_run_id=run.id,
                        target_conn=target_conn, target_schema=target_schema,
                        physical_table_name=target_table,
                    )
                except Exception as verify_exc:  # pragma: no cover - defensive
                    logger.warning(
                        "Attribute-relationship verification skipped for pocket %s: %s",
                        pocket_id, verify_exc,
                    )

                # Bug-8393: record what this build ACTUALLY materialised — one
                # descriptor per output column of the built table, read back from
                # the target catalogue — plus the liveness pointer, in THIS
                # transaction. This is what activates RLS-safe pocket serving
                # (Bug-8018) and what makes ``(status, active_refresh_run_id)`` a
                # sound generation stamp for the query-router's TOCTOU guard
                # (Bug-8392). Fail-closed inside: a manifest it cannot prove is
                # cleared, never approximated.
                await write_pocket_row_manifest(
                    pocket=pocket,
                    run_id=run.id,
                    target_conn=target_conn,
                    target_schema=target_schema,
                    target_table=target_table,
                    # Bug-8473: pin WHICH database these columns describe. A
                    # later target/connection re-point must not let the pocket
                    # serve a same-named table on another database.
                    target=target,
                    # Bug-8412: the BUILD-START capture, never a write-time read
                    # of ``model``. ``apply_build_binding`` above re-reads the
                    # model's pointer with a separate two-column SELECT purely to
                    # DETECT supersession — it never mutates the ORM object and
                    # never stamps the newer value — so a write-time read here
                    # would record the version of a deploy/revert that landed
                    # mid-build.
                    deployed_version_id=_build_binding.version_id,
                    tenant_session=db,
                    # Bug-8816: pass the ALREADY-CAPTURED target binding so the
                    # manifest records the value the build PROVED, not whatever
                    # live state re-derives at write time.
                    target_binding_dict=_target_build_binding.to_dict(),
                    # Bug-8780: pass the captured SOURCE binding so the
                    # serve-time guard can detect a source re-point.
                    source_binding_dict=_source_build_binding.to_dict(),
                )

            await db.commit()
            await db.refresh(run)

            # B-01 + B-02(c): now that the authoritative run/pocket outcome is
            # committed, do best-effort alerting. Resolve the open failure alert
            # ONLY when the pocket genuinely returned to service — a completed
            # run whose pocket committed ``fresh`` (i.e. no serving refusal and
            # no superseded binding). A completed-but-refused or superseded
            # pocket ends non-``fresh`` and keeps its alert open. A terminal
            # materialisation failure raises a fresh alert. Alerting is isolated
            # (see the helpers) and can never alter this committed outcome.
            if (
                run.status == POCKET_RUN_STATUS_COMPLETED
                and pocket.status == POCKET_STATUS_FRESH
            ):
                await _resolve_pocket_refresh_alert(_alert_tenant_id, pocket)
            elif _materialisation_error is not None:
                await _notify_pocket_refresh_failure(_alert_tenant_id, pocket, _materialisation_error)
            return run

    except PocketRefreshInFlightError as exc:
        if existing_run is not None:
            existing_run.status = "failed"
            existing_run.error_message = str(exc)[:1000]
            existing_run.completed_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(existing_run)
            return existing_run
        raise


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
    from shared.config.source_db import resolve_connection_bq_project

    tgt_ref = resolve_target_schema(
        "bigquery", target.config or {}, defaults,
        schema_override=pocket.target_schema,
        connection_bq_project=resolve_connection_bq_project(target_conn),
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


async def _run_incremental_leg(
    sc,
    *,
    pocket: PocketDefinition,
    select_sql: str,
    table_ref: str,
    target_schema: str,
    target_table: str,
    target_connector: str,
    window_start: datetime,
    key_candidates: tuple[str, ...],
) -> bool:
    """Patch the cached table over the delta window; return False to force FULL.

    Returns False — leaving the cached table BYTE-FOR-BYTE untouched — for every
    condition under which a windowed patch would be unsound, so the caller can
    fall through to the ordinary full rebuild. See
    ``shared.pocket.incremental`` for why each condition is disqualifying.
    """
    incremental_column = str(pocket.incremental_column)
    destination = await _table_columns_via(sc, target_schema, target_table)
    destination_columns = [name for name, _ in destination]
    destination_types = dict(destination)
    key_columns = usable_identity_columns(
        candidates=key_candidates,
        destination_columns=destination_columns,
        incremental_column=incremental_column,
        incremental_column_data_type=destination_types.get(incremental_column, ""),
    )
    if not key_columns:
        logger.info(
            "Pocket %s is being rebuilt in FULL: no row-identity key is usable "
            "for an incremental patch (model fact primary key %s, cached table "
            "%s.%s exposes %d columns, watermark %r). Matching the DELETE on the "
            "watermark VALUE instead would leave the superseded copy of every "
            "edited row in the cache (Bug-8699).",
            pocket.id, list(key_candidates) or "not declared/ambiguous",
            target_schema, target_table, len(destination_columns),
            incremental_column,
        )
        return False

    quoted_keys = tuple(_quote_ident(k, target_connector) for k in key_columns)
    quoted_inc = _quote_ident(incremental_column, target_connector)
    quoted_destination_columns = tuple(
        _quote_ident(c, target_connector) for c in destination_columns
    )

    # The cached rows must really be keyed by this column set before it can be
    # used to delete them. A model join that fans the fact grain out, or a cache
    # already duplicated by the historical Bug-8699 behaviour, fails here and is
    # repaired by the full rebuild.
    integrity = await sc.fetch_one(
        build_key_integrity_probe_sql(table_ref, quoted_keys)
    )
    if (
        integrity is None
        or int(integrity.get("duplicate_keys") or 0) > 0
        or int(integrity.get("null_keys") or 0) > 0
    ):
        logger.warning(
            "Pocket %s is being rebuilt in FULL: its cached rows are not "
            "uniquely identified by %s (probe=%s), so an identity DELETE would "
            "remove the wrong rows (Bug-8699).",
            pocket.id, list(key_columns), integrity,
        )
        return False

    suffix = str(pocket.id).replace("-", "")[:16]
    delta_ref = _quote_ident(f"_tmp_{suffix}", target_connector)
    keys_ref = _quote_ident(f"_tmpk_{suffix}", target_connector)

    # Names of the temp tables this call ACTUALLY created. The cleanup below
    # drops only these, and never speculatively: an unqualified
    # ``DROP TABLE IF EXISTS "_tmpk_..."`` for a name that was never created as
    # a temp table would resolve through the search path and could hit a
    # PERMANENT table of that name in the target schema. Once the temp exists it
    # shadows any permanent namesake (``pg_temp`` sorts first), so dropping a
    # name we just created is always safe. There is deliberately no
    # pre-emptive DROP before the CREATEs either: the source pool issues
    # ``DISCARD TEMP`` on every hand-out (``shared/source_pool._reset_session``),
    # so the temp namespace is already clean, and a surprise collision should
    # fail the build loudly rather than silently drop something.
    created_temps: list[str] = []

    try:
        # ORDER IS LOAD-BEARING: the KEY SET is snapshotted BEFORE the delta.
        #
        # The two scans are separate statement snapshots, so a row inserted at
        # the source between them appears in one and not the other. Key-set
        # first puts such a row in the DELTA only, which the leg handles
        # correctly and silently (the orphan delete cannot touch a row that is
        # not cached, and the INSERT writes it). Delta first would put it in the
        # KEY SET only — in the key set, not in the delta, not in the cache,
        # which is bit-for-bit an invariant violation (no cached copy, no
        # delta row) — so the reconciliation probe below would fire on every
        # ordinary concurrent write and permanently
        # disable the incremental leg for any source with live insert traffic,
        # while making it cost THREE source scans instead of one. Reviewer round
        # 2 reproduced both orders live. Do not reorder these two statements.
        await sc.execute(
            f"CREATE TEMP TABLE {keys_ref} AS "
            f"{build_key_scan_sql(select_sql, quoted_keys, quoted_inc)}"
        )
        created_temps.append(keys_ref)

        # The SOURCE's CURRENT population must be keyed by the declared identity
        # too — not just the cache and the delta. Reviewer round 3 reproduced the
        # gap live: when a join starts fanning the fact grain out AFTER the cache
        # was built, and the fanned row's own watermark never moves, the extra
        # copy is in the key set, absent from the delta, and its key IS already
        # cached. The orphan anti-join is a semi-join and the reconciliation
        # probe asks EXISTS, so neither can see MULTIPLICITY: the leg patched
        # happily
        # and the pocket under-reported permanently. This is the third and last
        # set the key has to identify, and it is load-bearing, not conservatism.
        key_set_integrity = await sc.fetch_one(
            build_key_integrity_probe_sql(keys_ref, quoted_keys)
        )
        if (
            key_set_integrity is None
            or int(key_set_integrity.get("duplicate_keys") or 0) > 0
            or int(key_set_integrity.get("null_keys") or 0) > 0
        ):
            logger.warning(
                "Pocket %s is being rebuilt in FULL: the source's CURRENT "
                "population is not uniquely identified by %s (probe=%s), so a "
                "row the source now produces more than once could never reach "
                "the cache (Bug-8699).",
                pocket.id, list(key_columns), key_set_integrity,
            )
            return False

        # Computed HERE, immediately before the delta statement, and NOT at the
        # top of the leg. ``NOW()`` inside the expression is evaluated by the
        # source when this statement runs, so any work between the measurement
        # and the statement slides the window's START forward by that much — the
        # unsafe direction, and silent, because an updated row's key is already
        # cached and no probe fires on it. The key scan above is a full source
        # scan, so that gap is not small.
        elapsed = window_elapsed_seconds(window_start)
        if elapsed is None:
            logger.warning(
                "Pocket %s is being rebuilt in FULL: the delta window anchor "
                "(%s) is not in the past, so the clock that stamped the last "
                "completed run and the clock reading it disagree by more than "
                "the whole lookback. A clamped one-second window would leave "
                "every changed row stale and silent.",
                pocket.id, window_start,
            )
            return False
        # The window is evaluated on the SOURCE's clock, in the watermark
        # column's own type — never as a UTC literal, which is silently wrong
        # against a naive ``timestamp`` column and truncates a ``date`` one.
        window_expression = build_window_expression(
            elapsed, destination_types.get(incremental_column, "")
        )
        await sc.execute(
            f"CREATE TEMP TABLE {delta_ref} AS "
            f"{build_delta_sql(select_sql, quoted_inc, window_expression)}"
        )
        created_temps.append(delta_ref)
        delta_integrity = await sc.fetch_one(
            build_key_integrity_probe_sql(delta_ref, quoted_keys)
        )
        if (
            delta_integrity is None
            or int(delta_integrity.get("duplicate_keys") or 0) > 0
            or int(delta_integrity.get("null_keys") or 0) > 0
        ):
            logger.warning(
                "Pocket %s is being rebuilt in FULL: the delta the source "
                "produced is not uniquely identified by %s (probe=%s), so the "
                "declared key does not identify a row of this pocket "
                "(Bug-8699).",
                pocket.id, list(key_columns), delta_integrity,
            )
            return False

        # A source presenting an EMPTY population is almost always a
        # TRUNCATE-and-reload caught mid-flight, not a real mass deletion. The
        # orphan anti-join would evict the entire cache and the INSERT would
        # restore only the delta window. Refuse unless the cache AND the delta
        # agree the population really is empty; the full rebuild reaches the
        # same answer safely, including for a genuine emptying. A non-empty
        # delta beside an empty key set is direct evidence the two snapshots
        # disagree (reviewer round 2).
        #
        # Every ``None`` here is treated as REFUSE, not as zero: an unreadable
        # probe is exactly the state in which permitting an anti-join against an
        # unknown key set is most dangerous. (A ``COUNT(*)`` always returns a
        # row, so this is defence in depth, but the other probes in this
        # function fail closed on None and this one must not be the exception.)
        source_keys_row = await sc.fetch_one(build_key_count_sql(keys_ref))
        if source_keys_row is None:
            logger.warning(
                "Pocket %s is being rebuilt in FULL: the source key-set count "
                "could not be read, so the orphan anti-join's blast radius is "
                "unknown.", pocket.id,
            )
            return False
        if int(source_keys_row.get("source_keys") or 0) == 0:
            cached_row = await sc.fetch_one(
                f"SELECT COUNT(*)::bigint AS c FROM {table_ref}"
            )
            delta_row = await sc.fetch_one(
                f"SELECT COUNT(*)::bigint AS c FROM {delta_ref}"
            )
            if (
                cached_row is None
                or delta_row is None
                or int(cached_row.get("c") or 0) > 0
                or int(delta_row.get("c") or 0) > 0
            ):
                logger.warning(
                    "Pocket %s is being rebuilt in FULL: the source produced an "
                    "EMPTY key set while the cache (%s) or the delta (%s) still "
                    "holds rows. An orphan anti-join against an empty population "
                    "would evict the whole cache; a source caught mid "
                    "TRUNCATE-and-reload looks exactly like this.",
                    pocket.id, cached_row, delta_row,
                )
                return False

        # THE invariant the leg rests on: every key the source currently
        # produces is either cached at that exact watermark or carried by the
        # delta. This replaced a growing enumeration of unsound shapes that
        # three consecutive review rounds each extended by one live-reproduced
        # member. See ``build_unreconciled_key_probe_sql``.
        unreconciled_row = await sc.fetch_one(
            build_unreconciled_key_probe_sql(
                table_ref, delta_ref, keys_ref, quoted_keys, quoted_inc
            )
        )
        if (
            unreconciled_row is None
            or int(unreconciled_row.get("unreconciled_keys") or 0) > 0
        ):
            logger.warning(
                "Pocket %s is being rebuilt in FULL: the source produces rows "
                "the patch cannot account for (probe=%s) — a key that is not "
                "cached at all, or one cached at a DIFFERENT watermark than the "
                "source now reports while sitting outside the delta window. No "
                "windowed patch can reach either, so the cache would under-report "
                "or serve a stale value indefinitely.",
                pocket.id, unreconciled_row,
            )
            return False

        # All-or-nothing: the cache must never be observable with the old copies
        # removed and the new ones not yet inserted.
        async with sc.transaction():
            await sc.execute(
                build_orphan_delete_sql(table_ref, keys_ref, quoted_keys)
            )
            await sc.execute(
                build_identity_delete_sql(table_ref, delta_ref, quoted_keys)
            )
            await sc.execute(
                build_insert_from_delta_sql(
                    table_ref, delta_ref, quoted_destination_columns
                )
            )
        # The refusals above all log; the success path must say so too, or an
        # operator has no way to tell a pocket that is quietly full-rebuilding
        # every run (the normal outcome — see Bug-8719) from one that is really
        # being patched. ``PocketRefreshRun.refresh_mode`` records the REQUESTED
        # mode and cannot answer this.
        logger.info(
            "Pocket %s was patched incrementally on key %s over the window "
            "starting %s (no full rebuild)",
            pocket.id, list(key_columns), window_start,
        )
    finally:
        # Best-effort: a failure to clean up must never mask the real error
        # being propagated.
        for _tmp in created_temps:
            try:
                await sc.execute(f"DROP TABLE IF EXISTS {_tmp}")
            except Exception:  # noqa: BLE001 - cleanup only
                logger.debug(
                    "Pocket %s: could not drop temp table %s", pocket.id, _tmp,
                    exc_info=True,
                )
    return True


async def _refresh_same_db(
    pocket: PocketDefinition,
    target_conn: ProjectConnection,
    target_schema: str,
    target_table: str,
    table_ref: str,
    token: str,
    refresh_mode: str,
    db: AsyncSession,
    *,
    build_state: PocketBuildState,
    window_start: datetime | None = None,
    key_candidates: tuple[str, ...] = (),
    target_connector: str = "postgresql",
) -> tuple[int | None, int | None]:
    select_sql = await _get_rewritten_sql(pocket.model_id, pocket.defining_sql, token)

    # Bug-8416: discovery is one independent statement and must not retain a
    # pool checkout while the refresh plan is decided.
    async with open_source_connection(target_conn, purpose="pocket_refresh", tenant_session=db) as sc:
        table_exists = await _table_exists_via(sc, target_schema, target_table)

    # Bug-8431: ``build_state`` carries the pre-mutation snapshot, NOT
    # ``pocket.status`` — which the caller has already flipped to
    # ``invalidating`` by this point.
    use_incremental = should_refresh_incrementally(
        incremental_column=pocket.incremental_column,
        refresh_mode=refresh_mode,
        table_exists=table_exists,
        build_state=build_state,
        window_start=window_start,
    )
    if not use_incremental and pocket.incremental_column and table_exists:
        logger.info(
            "Pocket %s has an incremental column but is being rebuilt in "
            "FULL (mode=%s entry_status=%s built_for=%s/%s deployed=%s/%s "
            "window_start=%s): a partial rebuild of a matcher-refused pocket "
            "would leave superseded rows in place and then stamp the table "
            "as current (Bug-8431); an unanchored window would silently drop "
            "every row that changed since the last successful run "
            "(Bug-8700)",
            pocket.id, refresh_mode, build_state.entry_status,
            build_state.built_for_version_id, build_state.built_for_epoch,
            build_state.deployed_version_id, build_state.deploy_epoch,
            window_start,
        )

    patched = False
    if use_incremental and window_start is not None:
        # This is the one pocket-refresh exception to operation-scoped checkout:
        # its temp tables are session-local and its three writes are explicitly
        # transactional. Retaining one checkout is therefore required, not an
        # incidental multi-statement convenience.
        async with open_source_connection(
            target_conn, purpose="pocket_refresh", tenant_session=db, transactional=True,
        ) as sc:
            patched = await _run_incremental_leg(
                sc,
                pocket=pocket,
                select_sql=select_sql,
                table_ref=table_ref,
                target_schema=target_schema,
                target_table=target_table,
                target_connector=target_connector,
                window_start=window_start,
                key_candidates=key_candidates,
            )

    if not patched:
        async with open_source_connection(target_conn, purpose="pocket_refresh", tenant_session=db) as sc:
            await sc.execute(f"DROP TABLE IF EXISTS {table_ref}")
            await sc.execute(f"CREATE TABLE {table_ref} AS {select_sql}")

    async with open_source_connection(target_conn, purpose="pocket_refresh", tenant_session=db) as sc:
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
    db: AsyncSession,
) -> tuple[int | None, int | None]:
    """Materialise a pocket across databases by streaming into staging.

    Takes no ``refresh_mode``: this path is ALWAYS a full replace.
    ``stream_to_staging_table`` builds a fresh staging table and atomically swaps
    it in, so there is no delta leg here and no incremental-vs-full decision to
    make (which is also why the Bug-8431 guard applies only to
    ``_refresh_same_db``). It used to accept ``refresh_mode`` and ignore it,
    which read as though the mode were honoured — the same false-premise shape
    that let Bug-8431's pocket leg ship unguarded (round-2 review finding 5).
    """
    rewritten_sql = await _get_rewritten_sql(pocket.model_id, pocket.defining_sql, token)
    source_conn = await resolve_source_connection(pocket.model_id, db)

    await ensure_target_schema(target_conn, target_schema, tenant_session=db)

    async def _streaming_batches():
        async with open_source_connection(source_conn, purpose="pocket_refresh", tenant_session=db) as sc:
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
    async with open_source_connection(target_conn, purpose="pocket_refresh", tenant_session=db) as sc:
        storage_bytes = await _fetch_storage_bytes(sc, target_schema, target_table, target_connector)

    return row_count, storage_bytes


async def resolve_pocket_physical_table(
    pocket: PocketDefinition,
    db: AsyncSession,
) -> tuple[Any, str, str, str] | None:
    """Resolve a validated pocket target to detached cleanup identity.

    Returns ``(connection, connector, schema, qualified_table_name)`` and
    performs the same cross-project and connector checks as the ordinary drop
    path.  Bug-8140's post-commit cleanup outbox snapshots this result before
    the owning model/target/connection metadata can be deleted.
    """
    target = await db.get(DataTarget, pocket.target_id)
    if target is None:
        return None

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
            "resolve_pocket_physical_table: refusing pocket %s storage — its "
            "target connection belongs to a different project than model %s "
            "(cross-project row rejected fail-closed)",
            pocket.id, pocket.model_id,
        )
        return None
    except ValueError:
        return None

    connector = normalize_connection_type(conn.connection_type)
    if connector not in _POCKET_TARGET_CONNECTORS:
        return None

    target_schema, target_table = _resolve_target_location(pocket, target)

    if connector == "bigquery":
        # Bug-5475: BigQuery targets qualify the dataset/project via the shared
        # target resolver, then quote with backticks (never double quotes).
        defaults = await resolve_aggregate_target_defaults(
            tenant_session=db, project_id=conn.project_id,
        )
        from shared.config.source_db import resolve_connection_bq_project

        tgt_ref = resolve_target_schema(
            "bigquery", target.config or {}, defaults,
            schema_override=pocket.target_schema,
            connection_bq_project=resolve_connection_bq_project(conn),
        )
        dotted = tgt_ref.qualified_table(target_table)
        target_schema = tgt_ref.schema
    else:
        dotted = f"{target_schema}.{target_table}" if target_schema else target_table

    return conn, connector, target_schema, dotted


async def drop_pocket_storage(
    pocket: PocketDefinition,
    db: AsyncSession,
) -> None:
    resolved = await resolve_pocket_physical_table(pocket, db)
    if resolved is None:
        return
    conn, connector, _schema, dotted = resolved
    table_ref = quote_table_ref(connector, dotted)

    await execute_source_ddl(conn, f"DROP TABLE IF EXISTS {table_ref}", tenant_session=db)
