"""Named Query refresh — full CTAS on the shared artifact substrate.

Mirrors ``shared/pocket/refresh.py::refresh_pocket_definition`` (the
materialised-artifact template) with the incremental leg removed: v1 always
rebuilds the whole result table. Every load-bearing primitive is the SHARED
one, never a fork:

* the refresh lock — ``shared/named_query_refresh_lock`` (dedicated-connection
  advisory lock, Bug-6104/Fable F-1 pattern);
* drift re-validation — the query-router ``/validate`` round trip against the
  CURRENT deployed model;
* the build/version binding — ``capture_build_binding``/``apply_build_binding``
  (Bug-8250);
* the target + source routing bindings — ``capture_target_build_binding`` /
  ``capture_source_build_binding`` (Bug-8807/Bug-8780);
* the finalisation protocol — ``shared/named_query/refresh_guard``, reusing
  ``lock_finalization_rows`` and the shared binding re-proofs (Bug-8807/8827);
* the output-column manifest — the pocket ``RowManifest`` shape read back from
  the target catalogue (``resolve_materialised_columns``), which is what the
  query-router's projection-shape RLS proof consumes.

Incremental refresh is DEFERRED (spec §6.1): v1 always does a FULL CTAS.
"""
from __future__ import annotations

import logging
import secrets
import types
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import ObjectDeletedError

from shared.aggregate_connection import is_same_database, resolve_source_connection
from shared.artifact_build_binding import apply_build_binding, capture_build_binding
from shared.artifact_target_binding import (
    capture_source_build_binding,
    capture_target_build_binding,
)
from shared.auth.service_principal import (
    SCOPE_POCKET_REFRESH,
    create_service_access_token,
)
from shared.config.resolver import get_setting
from shared.config.settings import get_settings
from shared.config.source_db import (
    resolve_aggregate_target_defaults,
    resolve_target_schema,
)
from shared.connection_scope import (
    CrossProjectConnectionError,
    resolve_endpoint_connection_for_model,
)
from shared.connector_qualify import quote_table_ref
from shared.db.models import (
    DataTarget,
    Model,
    NamedQuery,
    NamedQueryArtifact,
    NamedQueryRefreshRun,
    ProjectConnection,
)
from shared.db.session import SystemSessionLocal
from shared.model_refresh_epoch import bump_data_epoch
from shared.named_query_refresh_lock import (
    NamedQueryRefreshInFlightError,
    named_query_refresh_lock,
)
from shared.named_query.refresh_guard import (
    NQ_STATUS_FRESH,
    NQ_STATUS_INVALIDATING,
    NQ_STATUS_STALE,
    read_committed_named_query_status,
    read_named_query_finalization_state,
    resolve_named_query_serving_refusal,
)
from shared.pocket.refresh import unsupported_pocket_combo_reason
from shared.pocket.row_manifest import (
    build_project_is_addressable_by_the_serving_path,
    clear_pocket_row_manifest,
    resolve_materialised_columns,
)
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

logger = logging.getLogger(__name__)

NQ_RUN_STATUS_COMPLETED = "completed"

# Same connector set as pockets: postgresql/redshift (DROP+CTAS or streaming)
# and bigquery (atomic CREATE OR REPLACE when source and target are both BQ).
_NQ_TARGET_CONNECTORS = frozenset({"postgresql", "redshift", "bigquery"})
_NQ_CROSS_DB_CONNECTORS = frozenset({"postgresql", "redshift"})

_settings = get_settings()


class NamedQueryRowSecurityLeakError(ValueError):
    """The model has a row-security rule applying to the refresh identity."""


class NamedQueryMaxRowsExceededError(ValueError):
    """The materialised cardinality exceeded the configured row cap."""


def _mint_service_token(tenant_id: str) -> str:
    """Mint a short-lived JWT for internal service-to-service calls.

    The materialisation SELECT must run under the system service identity,
    never the caller's token — under the caller's token the router would wrap
    the rewrite with that caller's row-security predicate and the Named Query
    would cache only the refresher's permitted rows.
    """
    return create_service_access_token(
        principal="named-query-refresh",
        tenant_id=tenant_id,
        role="system_admin",
        ttl_minutes=5,
        scopes=[SCOPE_POCKET_REFRESH],
    )


async def _validate_definition_via_router(
    model_id: object,
    definition_sql: str,
    bearer_token: str,
    model_slug: str,
) -> Optional[str]:
    """Re-validate the definition against the CURRENT deployed model.

    Returns a structured failure reason (str) on any violation — this is the
    model/source-drift invalidation path (spec §8). Returns None when valid.
    Raises when the router cannot answer (retried by the caller's failure
    handling, which lands the artifact ``failed``).
    """
    url = f"{_settings.QUERY_ROUTER_URL}/api/v1/validate"
    headers = {"Authorization": f"Bearer {bearer_token}"}
    body = {
        "model_id": str(model_id),
        "raw_query": definition_sql,
        "protocol": "jdbc",
        "dialect": "postgres",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=body, headers=headers)
    if resp.status_code >= 500:
        raise RuntimeError(f"Query router validate returned HTTP {resp.status_code}")
    if resp.status_code >= 400:
        try:
            payload = resp.json()
            detail = payload.get("detail") if isinstance(payload, dict) else None
            if not isinstance(detail, str) or not detail:
                detail = resp.text
        except Exception:
            detail = resp.text or f"Query router returned HTTP {resp.status_code}"
        return f"Named Query definition failed validation: {detail}"
    data = resp.json()
    if not data.get("ok"):
        errors = data.get("errors") or ["validation failed"]
        return "Named Query definition failed validation: " + "; ".join(
            str(e) for e in errors
        )
    # Fail closed when the definition does not reference this model's slug —
    # the rewrite must be pinned to the model (same F-005-03 rule as pockets).
    tables = _definition_from_tables(definition_sql)
    if model_slug and tables and not any(
        (t or "").lower() == model_slug for t in tables
    ):
        return (
            f"Named Query definition does not reference the model "
            f"'{model_slug}': {sorted(set(tables))}"
        )
    return None


def _definition_from_tables(definition_sql: str) -> list[str]:
    """Best-effort FROM-table names of the definition (for the slug pin)."""
    try:
        import sqlglot
        from sqlglot import exp

        ast = sqlglot.parse_one(definition_sql)
        return [
            t.name
            for t in ast.find_all(exp.Table)
            if t.name and not t.name.startswith("@")
        ]
    except Exception:  # pragma: no cover - defensive
        return []


async def _get_rewritten_sql(
    model_id: object,
    definition_sql: str,
    bearer_token: str,
) -> str:
    """Compile the definition to the source dialect via the router /explain.

    NQ-2/Bug-9161: the body is the CANONICAL NQ population compile body from
    ``shared/named_query/population_contract`` — force_route="source",
    protocol="jdbc", dialect="postgres", include_hidden=False, no session
    vars, no caption dimensions — so the build compiles EXACTLY the same input
    the serve path's live helper compiles. ``route_type`` MUST be "source"
    (load-bearing): the canonical population is the definition-scoped semantic
    closure, never an aggregate/pocket serve and never the raw LEFT-forced
    detail route.

    Fail closed on any applied row-security rule (the refresh identity is
    unfiltered; a wildcard rule wrapping the SELECT would cache a filtered
    slice served to every reader — the pocket F-005-06 rule).
    """
    url = f"{_settings.QUERY_ROUTER_URL}/api/v1/explain"
    headers = {"Authorization": f"Bearer {bearer_token}"}
    body = {
        "model_id": str(model_id),
        "raw_query": definition_sql,
        "protocol": NQ_CANONICAL_PROTOCOL,
        "dialect": NQ_CANONICAL_DIALECT,
        "include_hidden": NQ_CANONICAL_INCLUDE_HIDDEN,
        "force_route": NQ_CANONICAL_FORCE_ROUTE,
    }
    # NQ-2/Bug-9161: a Named Query is materialised over its definition's canonical
    # SEMANTIC relation closure -- the source route, joined through the model's
    # DECLARED join types over exactly the relations the definition needs -- and
    # served live over that SAME route, so the two populations are identical by
    # construction. Never the raw LEFT-forced star (a BI-detail transport route,
    # not a governed-query authority) and never a route chosen from the outer
    # ``@name`` reference. ``force_route="source"`` bypasses aggregate/pocket so
    # the artifact holds the definition's own source population.
    # See docs/questions/questions_named-query-population.md.
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
        if data.get("route_type") != NQ_CANONICAL_FORCE_ROUTE:
            raise ValueError(
                f"Query router explain returned route_type="
                f"{data.get('route_type')!r}; the Named Query canonical "
                f"population requires the {NQ_CANONICAL_FORCE_ROUTE!r} route "
                f"(force_route is pinned to it, so this is a load-bearing "
                f"invariant)."
            )
        rewritten = data.get("rewritten_query")
        if not rewritten:
            raise ValueError("Query router returned empty rewritten_query")
        applied_rules = data.get("security_rules_applied") or []
        if applied_rules:
            raise NamedQueryRowSecurityLeakError(
                "Named Query refresh aborted: the model has a row-security "
                "rule that applies to the refresh service identity, so "
                "materialising would cache a row-filtered slice served to "
                "every reader. Scope the rule to specific roles so refresh "
                "can run under the unfiltered service identity."
            )
        return rewritten


async def _load_deployed_named_query_definition(
    db: AsyncSession,
    model: Model,
    named_query: NamedQuery,
    *,
    deployed_version_id: Any = None,
) -> tuple[dict, types.SimpleNamespace]:
    """Load the DEPLOYED definition of a Named Query from the deployed snapshot.

    Bug-9161 corrected Phase 1: the refresh build must materialise the
    DEPLOYED definition (governed model content — invariant 7), never the live
    ORM row, which can carry a draft edit made after the last deploy. Uses the
    same shared authority every snapshot-pinned family resolver uses
    (``shared/deploy_resolver_core``, Bug-8384):

    * model not deployed -> clear error;
    * deployed version row / snapshot missing or malformed -> clear error
      (fail closed, never fall back to the live definition);
    * the NQ id absent from the snapshot -> clear error (it is not part of the
      deployed version).

    ``deployed_version_id`` (NQ2C-F3 / Bug-8412): when the caller already
    captured the deployed pointer it is building FOR (the build-binding
    capture), the snapshot is read from THAT pointer — ``db.get(ModelVersion,
    version_id)`` with the owning-model check — instead of the ``model`` row's
    (possibly older) ``deployed_version_id``. The stamp and the content then
    provably come from ONE pointer read. Fail-closed is preserved: a missing
    or mismatched version row raises, and an undeployed pointer raises the
    same clear error as the model-row path.

    Returns ``(deployed_snapshot, definition_row)`` where ``definition_row`` is
    a namespace carrying ``definition_sql``, ``row_cap`` and ``shape`` from the
    DEPLOYED row. Target/policy/artifact state stays live.
    """
    from shared.db.models import ModelVersion
    from shared.deploy_resolver_core import (
        index_snapshot_rows,
        load_deployed_snapshot,
        snapshot_has_shape,
    )

    class _DeployedNamedQueryUnavailable(ValueError):
        pass

    if deployed_version_id is None:
        snapshot = await load_deployed_snapshot(
            db, model, family="named_queries",
            error_cls=_DeployedNamedQueryUnavailable,
        )
    else:
        # NQ2C-F3: the snapshot is read BY the captured build pointer, so the
        # stamped ``built_for_version_id`` and the compiled content provably
        # come from one pointer read (Bug-8412 ordering). Fail-closed: a
        # missing / mismatched / malformed version row raises — never a live
        # fallback.
        version = await db.get(ModelVersion, deployed_version_id)
        if version is None or version.model_id != model.id:
            raise _DeployedNamedQueryUnavailable(
                "The deployed model version could not be found."
            )
        snapshot = version.snapshot_json
        if not isinstance(snapshot, dict) or not snapshot_has_shape(
            snapshot, "named_queries"
        ):
            raise _DeployedNamedQueryUnavailable(
                "The deployed model snapshot is empty or malformed."
            )
    if snapshot is None:
        raise ValueError(
            f"Model {model.id} is not deployed; deploy it before refreshing "
            f"Named Query '{named_query.name}' — its deployed definition is "
            f"the build authority."
        )
    rows = index_snapshot_rows(snapshot, "named_queries")
    row = rows.get(str(named_query.id))
    if row is None:
        raise ValueError(
            f"Named Query '{named_query.name}' ({named_query.id}) is not part "
            f"of the model's deployed snapshot; deploy the model so the "
            f"definition can be materialised."
        )
    return snapshot, types.SimpleNamespace(
        definition_sql=str(row.get("definition_sql") or ""),
        row_cap=row.get("row_cap"),
        shape=str(row.get("shape") or "projection"),
    )


def _resolve_target_schema(artifact: NamedQueryArtifact, target: DataTarget) -> str:
    if artifact.target_schema:
        return str(artifact.target_schema)
    cfg = target.config if isinstance(target.config, dict) else {}
    return str(cfg.get("schema") or cfg.get("dataset") or "public")


def _resolve_target_location(
    artifact: NamedQueryArtifact, target: DataTarget
) -> tuple[str, str]:
    table_name = str(artifact.physical_table_name or "").strip()
    schema = _resolve_target_schema(artifact, target)
    if "." in table_name and not artifact.target_schema:
        left, right = table_name.split(".", 1)
        if left and right:
            return left, right
    return schema, table_name


async def resolve_named_query_physical_table(
    artifact: "NamedQueryArtifact",
    model_id: object,
    db: AsyncSession,
) -> tuple[Any, str, str, str] | None:
    """Resolve a Named Query artifact's materialised table to detached cleanup
    identity (F-013-07). Mirrors ``resolve_pocket_physical_table``.

    Returns ``(connection, connector, schema, qualified_table_name)``, or None
    when there is no outstanding physical identity (no ``physical_table_name``)
    or the target cannot be resolved safely (missing target, or a cross-project
    connection — fail closed, never DROP another project's table). The artifact
    carries no ``model_id`` of its own, so the owning model id is passed in.
    """
    table_name = str(getattr(artifact, "physical_table_name", "") or "").strip()
    if not table_name:
        return None
    target = await db.get(DataTarget, artifact.target_id)
    if target is None:
        return None
    try:
        conn = await resolve_endpoint_connection_for_model(
            db, target, model_id=model_id
        )
    except CrossProjectConnectionError:
        logger.error(
            "resolve_named_query_physical_table: refusing NQ artifact %s "
            "storage — its target connection belongs to a different project "
            "than model %s (cross-project row rejected fail-closed)",
            getattr(artifact, "id", "?"), model_id,
        )
        return None
    except ValueError:
        return None

    connector = normalize_connection_type(conn.connection_type)
    target_schema, target_table = _resolve_target_location(artifact, target)

    if connector == "bigquery":
        defaults = await resolve_aggregate_target_defaults(
            tenant_session=db, project_id=conn.project_id,
        )
        from shared.config.source_db import resolve_connection_bq_project

        tgt_ref = resolve_target_schema(
            "bigquery", target.config or {}, defaults,
            schema_override=artifact.target_schema,
            connection_bq_project=resolve_connection_bq_project(conn),
        )
        dotted = tgt_ref.qualified_table(target_table)
        target_schema = tgt_ref.schema
    else:
        dotted = (
            f"{target_schema}.{target_table}" if target_schema else target_table
        )

    return conn, connector, target_schema, dotted


def _quoted_table_ref(schema: str | None, table: str, connector: str) -> str:
    from shared.connector_qualify import quote_identifier

    schema = (schema or "").strip()
    table = (table or "").strip()
    if "." in table and not schema:
        return quote_table_ref(connector, table)
    if schema:
        return quote_table_ref(connector, f"{schema}.{table}")
    return quote_identifier(connector, table)


async def _fetch_storage_bytes(
    sc, schema: str, table: str, connector: str,
) -> Optional[int]:
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


async def _get_or_create_artifact(
    db: AsyncSession,
    named_query: NamedQuery,
    model: Model,
) -> NamedQueryArtifact:
    """Return the existing artifact or mint one on first refresh.

    The FIRST target of the model (stable order) is the materialisation
    target; a model without any DataTarget cannot materialise and fails with a
    clear reason.
    """
    existing = (
        await db.execute(
            select(NamedQueryArtifact).where(
                NamedQueryArtifact.named_query_id == named_query.id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    target = (
        await db.execute(
            select(DataTarget)
            .where(DataTarget.model_id == named_query.model_id)
            .order_by(DataTarget.created_at, DataTarget.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if target is None:
        raise ValueError(
            "This model has no data target, so the Named Query cannot be "
            "materialised. Configure a target and refresh again."
        )
    seed = model.seed or secrets.token_hex(6)
    suffix = secrets.token_hex(4)
    artifact = NamedQueryArtifact(
        named_query_id=named_query.id,
        target_id=target.id,
        physical_table_name=f"nq_{seed}_{suffix}",
        status=NQ_STATUS_STALE,
    )
    db.add(artifact)
    await db.flush()
    return artifact


async def _refresh_same_db(
    *,
    model_id: object,
    definition_sql: str,
    token: str,
    artifact: NamedQueryArtifact,
    target_conn: ProjectConnection,
    target_schema: str,
    target_table: str,
    table_ref: str,
    db: AsyncSession,
) -> tuple[Optional[int], Optional[int]]:
    """Full CTAS on a same-database target (PostgreSQL family)."""
    select_sql = await _get_rewritten_sql(model_id, definition_sql, token)
    async with open_source_connection(target_conn, purpose="named_query_refresh", tenant_session=db) as sc:
        await sc.execute(f"DROP TABLE IF EXISTS {table_ref}")
        await sc.execute(f"CREATE TABLE {table_ref} AS {select_sql}")
        row = await sc.fetch_one(f"SELECT COUNT(*)::bigint AS c FROM {table_ref}")
        row_count = int(row["c"]) if row and row.get("c") is not None else None
        connector = normalize_connection_type(target_conn.connection_type)
        storage_bytes = await _fetch_storage_bytes(
            sc, target_schema, target_table, connector
        )
    return row_count, storage_bytes


async def _refresh_same_db_bigquery(
    *,
    model_id: object,
    definition_sql: str,
    token: str,
    artifact: NamedQueryArtifact,
    target_conn: ProjectConnection,
    target: DataTarget,
    target_schema: str,
    target_table: str,
    db: AsyncSession,
) -> tuple[str, Optional[int], Optional[int]]:
    """Atomic CREATE OR REPLACE TABLE into a BigQuery target.

    Mirrors the pocket BigQuery path (Bug-5475): source and target are the
    same BigQuery connection, the router-rewritten SELECT is already BigQuery
    dialect and executes in-place.
    """
    select_sql = await _get_rewritten_sql(model_id, definition_sql, token)
    defaults = await resolve_aggregate_target_defaults(
        tenant_session=db, project_id=target_conn.project_id,
    )
    from shared.config.source_db import resolve_connection_bq_project

    tgt_ref = resolve_target_schema(
        "bigquery", target.config or {}, defaults,
        schema_override=artifact.target_schema,
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


async def _refresh_cross_db(
    *,
    model_id: object,
    definition_sql: str,
    token: str,
    artifact: NamedQueryArtifact,
    target_conn: ProjectConnection,
    target_schema: str,
    target_table: str,
    db: AsyncSession,
) -> tuple[Optional[int], Optional[int]]:
    """Materialise across databases by streaming into staging.

    Always a full replace (same as the pocket cross-db path): the rewritten
    source SELECT streams through ``stream_to_staging_table``.
    """
    rewritten_sql = await _get_rewritten_sql(model_id, definition_sql, token)
    source_conn = await resolve_source_connection(model_id, db)
    await ensure_target_schema(target_conn, target_schema, tenant_session=db)

    async def _streaming_batches():
        async with open_source_connection(source_conn, purpose="named_query_refresh", tenant_session=db) as sc:
            async for batch in sc.fetch_batched(rewritten_sql, batch_size=20_000):
                yield batch

    from shared.pocket.refresh import _infer_col_types as _infer_fn

    row_count = await stream_to_staging_table(
        target_conn, target_schema, target_table,
        _streaming_batches(),
        batch_size=20_000,
        infer_types_fn=_infer_fn,
        tenant_session=db,
    )
    connector = normalize_connection_type(target_conn.connection_type)
    async with open_source_connection(target_conn, purpose="named_query_refresh", tenant_session=db) as sc:
        storage_bytes = await _fetch_storage_bytes(
            sc, target_schema, target_table, connector
        )
    return row_count, storage_bytes


# NQ-2/Bug-9161 (corrected Phase 1): the canonical-population CONTRACT now lives
# in ``shared/named_query/population_contract.py`` -- a dependency-light module
# the query-router serve path can import without dragging this heavyweight
# build-side module onto its import graph (Bug-9174/NQ2R1-F7). The two contract
# names are re-exported here for consumers and tests that import through the
# refresh module. Bump NQ_POPULATION_CONTRACT_VERSION there whenever the NQ
# population compile logic changes: every artifact carrying an older (or no)
# fingerprint is refused at serve and falls back to live until rebuilt.
# See docs/questions/questions_named-query-population.md.
from shared.named_query.population_contract import (  # noqa: F401
    NQ_CANONICAL_DIALECT,
    NQ_CANONICAL_FORCE_ROUTE,
    NQ_CANONICAL_INCLUDE_HIDDEN,
    NQ_CANONICAL_PROTOCOL,
    NQ_POPULATION_CONTRACT_VERSION,
    named_query_population_fingerprint,
    named_query_population_manifest_matches,
)
from shared.named_query.star_expansion import (  # noqa: F401
    expand_named_query_star_definition,
)


async def write_named_query_row_manifest(
    *,
    artifact: NamedQueryArtifact,
    run_id: object,
    target_conn: ProjectConnection,
    target_schema: Optional[str],
    target_table: str,
    target: DataTarget,
    deployed_version_id: object = None,
    population_fingerprint: Optional[str] = None,
    tenant_session: Any = None,
    target_binding_dict: Optional[dict] = None,
    source_binding_dict: Optional[dict] = None,
) -> bool:
    """Write the artifact's row manifest + liveness pointer for ``run_id``.

    Mirrors ``write_pocket_row_manifest`` (Bug-8393/Bug-8473/Bug-8780): the
    columns are read back from the target catalogue, the already-captured
    target + source build bindings are recorded verbatim, and the manifest is
    written in the same transaction as ``active_refresh_run_id``. Returns True
    when written, False when cleared fail-closed (never raises — a manifest
    failure must cost only the RLS-serving proof, not the refresh).
    """
    from shared.semantic.artifact_manifest import RowManifest, compute_row_manifest_hash

    system_session = None
    try:
        from shared.db.session import SystemSessionLocal

        async with SystemSessionLocal() as sys_db:
            system_session = sys_db
            try:
                if not build_project_is_addressable_by_the_serving_path(
                    target, target_conn,
                ):
                    raise ValueError(
                        "this BigQuery build wrote to a project the serving "
                        "path cannot name; refusing to write a manifest for a "
                        "table the query will never read"
                    )
                columns = await resolve_materialised_columns(
                    target_conn,
                    schema=target_schema,
                    table=target_table,
                    tenant_session=tenant_session,
                    system_session=system_session,
                )
                if not columns:
                    raise ValueError(
                        f"Target catalogue reports no columns for named query "
                        f"table {target_schema or ''}.{target_table}"
                    )
                manifest = RowManifest(
                    deployed_version_id=(
                        str(deployed_version_id) if deployed_version_id else None
                    ),
                    row_definition_fingerprint=population_fingerprint,
                    columns=columns,
                    attribute_edges=[],
                    build_refresh_run_id=str(run_id),
                    target_binding=target_binding_dict,
                    source_binding=source_binding_dict,
                )
                manifest.manifest_hash = compute_row_manifest_hash(manifest)
                artifact.row_manifest = manifest.to_dict()
                artifact.active_refresh_run_id = run_id
                return True
            except Exception:
                logger.warning(
                    "Named Query row manifest not written for artifact %s "
                    "(run %s); it will not be served under row-level security "
                    "until a later refresh records its materialised columns",
                    getattr(artifact, "id", None), run_id, exc_info=True,
                )
                clear_pocket_row_manifest(artifact, generation_run_id=run_id)
                return False
    except Exception:
        logger.warning(
            "Named Query row manifest not written for artifact %s (run %s); "
            "the system-scoped endpoint could not be resolved",
            getattr(artifact, "id", None), run_id, exc_info=True,
        )
        clear_pocket_row_manifest(artifact, generation_run_id=run_id)
        return False


def _mark_run_failed(
    run: NamedQueryRefreshRun, reason: str, now: datetime,
) -> None:
    run.status = "failed"
    run.completed_at = now
    run.error_message = reason[:1000]


async def _stamp_failure(
    db: AsyncSession,
    artifact: NamedQueryArtifact,
    run: NamedQueryRefreshRun,
    reason: str,
) -> None:
    """Commit the authoritative failure state (status failed + run failed).

    The manifest and liveness pointer are cleared together (Bug-8393 fail
    closed): the previous build's manifest no longer describes what is on disk.
    """
    now = datetime.now(timezone.utc)
    _mark_run_failed(run, reason, now)
    artifact.status = "failed"
    artifact.failure_reason = reason[:1000]
    clear_pocket_row_manifest(artifact)


async def refresh_named_query_artifact(
    named_query_id: object,
    db: AsyncSession,
    *,
    triggered_by: str = "api",
    bearer_token: Optional[str] = None,
    tenant_id: Optional[str] = None,
    existing_run_id: object = None,
) -> NamedQueryRefreshRun:
    """Refresh a Named Query's materialised result table (full CTAS).

    The F-005-23 pattern: ``existing_run_id`` lets the async manual path
    pre-create a ``queued`` run and have this function adopt it.
    """
    named_query = await db.get(NamedQuery, named_query_id)
    if named_query is None:
        raise ValueError(f"NamedQuery {named_query_id} not found")

    if not tenant_id and isinstance(getattr(db, "info", None), dict):
        tenant_id = db.info.get("tenant_id")
    service_token = _mint_service_token(tenant_id or "system")

    existing_run: Optional[NamedQueryRefreshRun] = None
    if existing_run_id is not None:
        existing_run = await db.get(NamedQueryRefreshRun, existing_run_id)
        if existing_run is None or existing_run.named_query_id != named_query.id:
            raise ValueError(
                f"NamedQueryRefreshRun {existing_run_id} not found for "
                f"named query {named_query_id}"
            )

    try:
        async with named_query_refresh_lock(db, named_query.id):
            try:
                await db.refresh(named_query)
            except ObjectDeletedError as exc:
                raise ValueError(
                    f"NamedQuery {named_query_id} was deleted while its "
                    f"refresh was starting"
                ) from exc

            model = await db.get(Model, named_query.model_id)
            model_slug = ""
            if model is not None:
                model_slug = (getattr(model, "slug", "") or "").lower()
            if model is None:
                raise ValueError(
                    f"Model {named_query.model_id} for named query "
                    f"{named_query.id} not found"
                )

            # Freeze the deployed pointer this materialisation is built FOR
            # (Bug-8412: captured before the definition is read, stamped on
            # success, never re-read at write time).
            _build_binding = await capture_build_binding(db, named_query.model_id)

            # The DEPLOYED definition is the build authority (Bug-9161
            # corrected Phase 1): read it from the deployed snapshot — never
            # the live ORM row, which can carry a draft edit made after the
            # last deploy — and EXPAND a row-preserving star definition to its
            # explicit exposed-field projection so the canonical source compile
            # joins the model's declared relations instead of collapsing to the
            # anchor table (see shared/named_query/star_expansion.py).
            # NQ2C-F3: the snapshot is loaded BY the captured build pointer
            # (``_build_binding.version_id``), so the manifest stamp and the
            # compiled content provably come from ONE pointer read — the
            # ``model`` ORM row read above is t1 state and is used only for
            # identity/slug, never as the snapshot pointer.
            _deployed_snapshot, _deployed_nq = (
                await _load_deployed_named_query_definition(
                    db, model, named_query,
                    deployed_version_id=_build_binding.version_id,
                )
            )
            definition_sql = expand_named_query_star_definition(
                _deployed_nq.definition_sql, _deployed_snapshot,
            )

            # Drift re-validation against the CURRENT deployed model, over the
            # deployed (expanded) definition this build compiles.
            validate_token = bearer_token or service_token
            try:
                invalid_reason = await _validate_definition_via_router(
                    named_query.model_id,
                    definition_sql,
                    validate_token,
                    model_slug,
                )
            except RuntimeError as exc:
                invalid_reason = str(exc)
            if invalid_reason is not None:
                now = datetime.now(timezone.utc)
                if existing_run is not None:
                    run = existing_run
                    _mark_run_failed(run, invalid_reason, now)
                else:
                    run = NamedQueryRefreshRun(
                        named_query_id=named_query.id,
                        refresh_mode="full",
                        status="failed",
                        triggered_by=triggered_by,
                        error_message=invalid_reason[:1000],
                        completed_at=now,
                    )
                    db.add(run)
                artifact = (
                    await db.execute(
                        select(NamedQueryArtifact).where(
                            NamedQueryArtifact.named_query_id == named_query.id
                        )
                    )
                ).scalar_one_or_none()
                if artifact is not None:
                    await _stamp_failure(db, artifact, run, invalid_reason)
                await db.commit()
                await db.refresh(run)
                return run

            artifact = await _get_or_create_artifact(db, named_query, model)
            target = await db.get(DataTarget, artifact.target_id)
            if target is None:
                raise ValueError(
                    f"DataTarget {artifact.target_id} for named query "
                    f"{named_query.id} not found"
                )
            try:
                target_conn = await resolve_endpoint_connection_for_model(
                    db, target, model_id=named_query.model_id
                )
            except (CrossProjectConnectionError, ValueError) as exc:
                raise ValueError(
                    f"Named Query target connection invalid: {exc}"
                ) from exc
            source_conn = await resolve_source_connection(named_query.model_id, db)
            cross_db = not is_same_database(source_conn, target_conn)

            target_connector = normalize_connection_type(
                target_conn.connection_type
            )
            source_connector = await resolve_connector_type(source_conn)
            combo_error = unsupported_pocket_combo_reason(
                source_connector, target_connector, cross_db
            )
            if combo_error is not None:
                if existing_run is not None:
                    run = existing_run
                    _mark_run_failed(run, combo_error, datetime.now(timezone.utc))
                else:
                    run = NamedQueryRefreshRun(
                        named_query_id=named_query.id,
                        refresh_mode="full",
                        status="failed",
                        triggered_by=triggered_by,
                        error_message=combo_error[:1000],
                        completed_at=datetime.now(timezone.utc),
                    )
                    db.add(run)
                await _stamp_failure(db, artifact, run, combo_error)
                await db.commit()
                await db.refresh(run)
                return run

            # Run row BEFORE anything that can fail with a credential error
            # (Bug-8822), so the except handler can always stamp it failed.
            if existing_run is not None:
                run = existing_run
                run.status = "running"
                run.refresh_mode = "full"
            else:
                run = NamedQueryRefreshRun(
                    named_query_id=named_query.id,
                    refresh_mode="full",
                    status="running",
                    triggered_by=triggered_by,
                )
                db.add(run)

            try:
                _target_build_binding = await capture_target_build_binding(
                    target, target_conn, tenant_session=db,
                )
                _source_build_binding = await capture_source_build_binding(
                    named_query.model_id, source_conn, tenant_session=db,
                )
            except Exception as _capture_err:
                now = datetime.now(timezone.utc)
                _mark_run_failed(run, str(_capture_err), now)
                artifact.status = "failed"
                artifact.failure_reason = str(_capture_err)[:1000]
                clear_pocket_row_manifest(artifact)
                await db.commit()
                await db.refresh(run)
                return run

            # The invalidating write itself can clobber a staler (Bug-8827).
            # With no incremental leg there is no shortcut to withdraw, so a
            # staler observed here is logged only — the finalisation guard is
            # the gate that keeps a clobbered build out of the serving pool.
            _committed_status = await read_committed_named_query_status(
                db, artifact.id, lock_for_finalization=True,
            )
            if _committed_status not in (NQ_STATUS_STALE, "failed", "fresh", None):
                logger.warning(
                    "Named query artifact %s was written by another party "
                    "while this refresh was starting (committed=%r); "
                    "rebuilding in FULL",
                    artifact.id, _committed_status,
                )
            if artifact.status == "failed":
                artifact.status = NQ_STATUS_STALE
                artifact.failure_reason = None
            artifact.status = NQ_STATUS_INVALIDATING
            await db.commit()
            await db.refresh(run)

            target_schema, target_table = _resolve_target_location(artifact, target)
            table_ref = _quoted_table_ref(
                target_schema, target_table, target_connector
            )

            _serving_refusal: Optional[str] = None
            try:
                token = service_token
                if cross_db:
                    row_count, storage_bytes = await _refresh_cross_db(
                        model_id=named_query.model_id,
                        definition_sql=definition_sql,
                        token=token,
                        artifact=artifact,
                        target_conn=target_conn,
                        target_schema=target_schema,
                        target_table=target_table,
                        db=db,
                    )
                elif (
                    target_connector == "bigquery"
                    and source_connector == "bigquery"
                ):
                    target_schema, row_count, storage_bytes = (
                        await _refresh_same_db_bigquery(
                            model_id=named_query.model_id,
                            definition_sql=definition_sql,
                            token=token,
                            artifact=artifact,
                            target_conn=target_conn,
                            target=target,
                            target_schema=target_schema,
                            target_table=target_table,
                            db=db,
                        )
                    )
                else:
                    row_count, storage_bytes = await _refresh_same_db(
                        model_id=named_query.model_id,
                        definition_sql=definition_sql,
                        token=token,
                        artifact=artifact,
                        target_conn=target_conn,
                        target_schema=target_schema,
                        target_table=target_table,
                        table_ref=table_ref,
                        db=db,
                    )

                # ROW cap — reject, never truncate (spec §4.1/§10). The
                # per-NQ row_cap overrides the system named_query.max_rows.
                # The cap is DEPLOYED governance (read from the snapshot row),
                # not live ORM state.
                effective_row_cap: Optional[int] = None
                if _deployed_nq.row_cap is not None:
                    effective_row_cap = int(_deployed_nq.row_cap)
                else:
                    async with SystemSessionLocal() as sys_db:
                        max_rows = int(
                            await get_setting(
                                "named_query.max_rows",
                                system_session=sys_db,
                                tenant_session=db,
                                model_id=named_query.model_id,
                            )
                        )
                    if max_rows > 0:
                        effective_row_cap = max_rows
                if (
                    effective_row_cap is not None
                    and effective_row_cap > 0
                    and (row_count is None or row_count > effective_row_cap)
                ):
                    artifact.target_schema = target_schema
                    artifact.physical_table_name = target_table
                    try:
                        await drop_named_query_storage(artifact, db)
                    except Exception:
                        logger.exception(
                            "Failed to evict oversized named query %s storage; "
                            "still marking the artifact failed", artifact.id,
                        )
                    if row_count is None:
                        reason = (
                            f"Named Query materialised an unknown number of "
                            f"rows (row count unavailable); with a row cap of "
                            f"{effective_row_cap} the admission gate fails "
                            f"closed. The table was dropped and the artifact "
                            f"marked failed."
                        )
                    else:
                        reason = (
                            f"Named Query materialised {row_count} rows, "
                            f"exceeding the row cap of {effective_row_cap}; "
                            f"the table was dropped and the artifact marked "
                            f"failed. Narrow the definition or raise the cap. "
                            f"(ROW_CAP_EXCEEDED)"
                        )
                    raise NamedQueryMaxRowsExceededError(reason)

                # Finalisation protocol (Bug-8807): MUST run before any
                # run/artifact mutation below, on committed truth under the
                # fixed-order control-plane locks.
                _finalization = await read_named_query_finalization_state(
                    db,
                    artifact_id=artifact.id,
                    target_binding=_target_build_binding,
                    source_binding=_source_build_binding,
                    connection_ids=(target_conn.id, source_conn.id),
                    target_id=artifact.target_id,
                    model_id=named_query.model_id,
                )
                _serving_refusal = resolve_named_query_serving_refusal(
                    _finalization
                )

                now = datetime.now(timezone.utc)
                run.status = NQ_RUN_STATUS_COMPLETED
                run.completed_at = now
                run.rows_written = row_count
                run.bytes_processed = storage_bytes
                if _serving_refusal is None:
                    artifact.status = NQ_STATUS_FRESH
                    artifact.failure_reason = None
                else:
                    artifact.status = NQ_STATUS_STALE
                    artifact.failure_reason = _serving_refusal[:1000]
                    logger.warning(
                        "Named query artifact %s completed its build but is "
                        "being kept non-serving (committed_status=%r "
                        "target_ok=%s source_ok=%s) — %s",
                        artifact.id, _finalization.committed_status,
                        _finalization.target_binding_matches,
                        _finalization.source_binding_matches,
                        _serving_refusal,
                    )
                artifact.last_refresh_at = now
                artifact.target_schema = target_schema
                artifact.physical_table_name = target_table
                artifact.row_count = row_count
                # ``apply_build_binding`` (shared) reads ``artifact.model_id``
                # to re-prove the deployed pointer; the artifact table does not
                # carry that column (it lives on the definition), so attach the
                # owning definition's model id as a transient attribute.
                artifact.model_id = named_query.model_id
                _superseded = await apply_build_binding(
                    db, artifact, _build_binding
                )
                if _superseded:
                    artifact.status = NQ_STATUS_STALE
                    clear_pocket_row_manifest(artifact)
                    logger.warning(
                        "Model %s was deployed/reverted while named query %s "
                        "was materialising; stamped the build-start binding "
                        "and marked the artifact stale",
                        named_query.model_id, named_query.id,
                    )
                await bump_data_epoch(db, named_query.model_id)
            except Exception as exc:
                now = datetime.now(timezone.utc)
                _mark_run_failed(run, str(exc), now)
                artifact.status = "failed"
                artifact.failure_reason = str(exc)[:1000]
                clear_pocket_row_manifest(artifact)

            await db.flush()

            # Manifest: written only for a completed, admitted build (the
            # manifest is what admits projection-shape serving under RLS; a
            # refused build must not describe its rows).
            if run.status == NQ_RUN_STATUS_COMPLETED and _serving_refusal is not None:
                clear_pocket_row_manifest(artifact)
            elif run.status == NQ_RUN_STATUS_COMPLETED:
                await write_named_query_row_manifest(
                    artifact=artifact,
                    run_id=run.id,
                    target_conn=target_conn,
                    target_schema=target_schema,
                    target_table=target_table,
                    target=target,
                    deployed_version_id=_build_binding.version_id,
                    population_fingerprint=named_query_population_fingerprint(
                        model_id=named_query.model_id,
                        named_query_id=named_query.id,
                        deployed_version_id=_build_binding.version_id,
                        deploy_epoch=_build_binding.epoch,
                        definition_sql=definition_sql,
                    ),
                    tenant_session=db,
                    target_binding_dict=_target_build_binding.to_dict(),
                    source_binding_dict=_source_build_binding.to_dict(),
                )

            await db.commit()
            await db.refresh(run)
            return run

    except NamedQueryRefreshInFlightError as exc:
        if existing_run is not None:
            existing_run.status = "failed"
            existing_run.error_message = str(exc)[:1000]
            existing_run.completed_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(existing_run)
            return existing_run
        raise


async def drop_named_query_storage(
    artifact: NamedQueryArtifact,
    db: AsyncSession,
) -> None:
    """Drop the physical result table (eviction path, delete + row-cap).

    Mirrors ``drop_pocket_storage`` with the same fail-closed guards: never
    issue DROP TABLE against a target connection in a different project than
    the owning model; missing connection/model is a best-effort no-op.
    """
    from shared.db.models import NamedQuery

    named_query = await db.get(NamedQuery, artifact.named_query_id)
    if named_query is None:
        return
    target = await db.get(DataTarget, artifact.target_id)
    if target is None:
        return
    try:
        conn = await resolve_endpoint_connection_for_model(
            db, target, model_id=named_query.model_id
        )
    except CrossProjectConnectionError:
        logger.error(
            "drop_named_query_storage: refusing to drop named query %s "
            "storage — its target connection belongs to a different project "
            "than model %s (cross-project row rejected fail-closed)",
            named_query.id, named_query.model_id,
        )
        return
    except ValueError:
        return

    connector = normalize_connection_type(conn.connection_type)
    if connector not in _NQ_TARGET_CONNECTORS:
        return
    target_schema, target_table = _resolve_target_location(artifact, target)
    if connector == "bigquery":
        defaults = await resolve_aggregate_target_defaults(
            tenant_session=db, project_id=conn.project_id,
        )
        from shared.config.source_db import resolve_connection_bq_project

        tgt_ref = resolve_target_schema(
            "bigquery", target.config or {}, defaults,
            schema_override=artifact.target_schema,
            connection_bq_project=resolve_connection_bq_project(conn),
        )
        dotted = tgt_ref.qualified_table(target_table)
        table_ref = quote_table_ref("bigquery", dotted)
    else:
        table_ref = _quoted_table_ref(target_schema, target_table, connector)
    await execute_source_ddl(conn, f"DROP TABLE IF EXISTS {table_ref}", tenant_session=db)
