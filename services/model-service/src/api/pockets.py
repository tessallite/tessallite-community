"""Pocket table CRUD + refresh routes."""
from __future__ import annotations

import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from shared.config.resolver import get_setting
from shared.config.settings import get_settings
# Bug-8453: the single definition of the /execute row-security contract.
from shared.security.execute_contract import (
    RowSecurityDeniedError,
    execute_response_denied_all,
)
from shared.db.models import (
    DataTarget,
    Model,
    PocketDefinition,
    PocketPredicate,
    PocketRefreshPolicy,
    PocketRefreshRun,
    Project,
    ProjectConnection,
    QueryLog,
    RouteLog,
)
from shared.schemas.connection_type import normalize_connection_type
from shared.db.session import get_tenant_db
# Bug-8581: the ONE cache-eviction helper. It mints a short-lived internal
# tenant-admin service token carrying only SCOPE_CACHE_EVICT — forwarding the
# caller's bearer would silently 403 for a project-scoped modeler (Bug-6204),
# which is how an eviction call can appear wired and do nothing. Imported rather
# than re-implemented so a pocket delete cannot drift from what deploy does.
from src.api.versions import _evict_query_router_cache
from shared.aggregate_connection import is_same_database, resolve_source_connection
from shared.pocket.fingerprint import predicate_set_hash
from shared.pocket.refresh import (
    unsupported_pocket_combo_reason,
    drop_pocket_storage,
    refresh_pocket_definition,
)
from shared.pocket.refresh_guard import (
    POCKET_INELIGIBLE_POPULATION_REASON,
    POCKET_POPULATION_ELIGIBILITY_INELIGIBLE,
)
from shared.pocket_refresh_lock import (
    PocketRefreshInFlightError,
    pocket_refresh_lock,
)
from shared.source_executor import resolve_connector_type
from shared.pocket.structure import collect_pocket_structure_violations
from shared.schemas.pydantic_models import (
    PocketDefinitionCreate,
    PocketDefinitionResponse,
    PocketDefinitionUpdate,
    PocketCompoundEdit,
    PocketDryRunRequest,
    PocketDryRunResponse,
    PocketRefreshPolicyResponse,
    PocketRefreshPolicyUpsert,
    PocketRefreshRunResponse,
    PocketValidateRequest,
    PocketValidateResponse,
    PocketViolationItem,
)
from src.api._validator_unavailable import validator_unavailable
from src.api._model_lock import acquire_model_definition_lock
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

logger = logging.getLogger(__name__)
_settings = get_settings()

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/pockets",
    tags=["pockets"],
)


async def _run_pocket_refresh_in_background(
    tenant_id: str,
    pocket_id: UUID,
    run_id: UUID,
    bearer_token: str | None,
) -> None:
    """F-005-23: execute a queued pocket refresh off the request path.

    Opens its own tenant-scoped DB session (the request session is closed by
    the time this runs) and adopts the pre-created ``queued`` run row via
    ``existing_run_id`` so the same run transitions queued → running →
    completed/failed. Never raises — ``refresh_pocket_definition`` records its
    own failure on the run row; this wrapper only guards against an unexpected
    crash leaving the run stuck in ``queued``.
    """
    try:
        async for db in get_tenant_db(tenant_id):
            try:
                await refresh_pocket_definition(
                    pocket_id,
                    db,
                    triggered_by="api",
                    refresh_mode="manual",
                    bearer_token=bearer_token,
                    tenant_id=tenant_id,
                    existing_run_id=run_id,
                )
            except Exception as exc:
                logger.exception(
                    "Async pocket refresh crashed for pocket=%s run=%s: %s",
                    pocket_id, run_id, exc,
                )
                # Best-effort: mark the run failed so it does not hang in queued.
                try:
                    run = await db.get(PocketRefreshRun, run_id)
                    if run is not None and run.status in ("queued", "running"):
                        run.status = "failed"
                        run.error_message = str(exc)[:1000]
                        run.completed_at = datetime.now(timezone.utc)
                        pocket = await db.get(PocketDefinition, pocket_id)
                        if pocket is not None:
                            pocket.status = "failed"
                            pocket.failure_reason = str(exc)[:1000]
                            pocket.row_manifest = None
                            pocket.active_refresh_run_id = None
                            pocket.built_for_version_id = None
                            pocket.built_for_epoch = None
                        await db.commit()
                except Exception:
                    logger.exception("Failed to mark pocket run %s failed", run_id)
    except Exception:
        logger.exception("Async pocket refresh session setup failed for run=%s", run_id)


async def _get_scoped_model(db, project_id: UUID, model_id: UUID) -> Model:
    model = await db.get(Model, model_id)
    if model is None or model.project_id != project_id:
        raise HTTPException(status_code=404, detail="Model not found")
    return model


async def _resolve_effective_pocket_budget(db, model: Model) -> int | None:
    """Bug-7005: the effective pocket size budget for a model.

    Model budget overrides project; NULL on the model means inherit from the
    project; both NULL means unlimited. Mirrors
    ``optimizer/src/advisor/pocket_suggester.resolve_effective_budget`` so the
    manual-create path and the optimizer auto-create path agree on the same
    authoritative budget fields (Model/Project ``pocket_size_budget_bytes``).
    """
    model_budget = getattr(model, "pocket_size_budget_bytes", None)
    if model_budget is not None:
        return int(model_budget)
    project = await db.get(Project, model.project_id)
    project_budget = getattr(project, "pocket_size_budget_bytes", None)
    if project is not None and project_budget is not None:
        return int(project_budget)
    return None


async def _current_pocket_usage_bytes(db, model_id: UUID) -> int:
    """Bug-7005: live pocket storage for a model (retired pockets excluded).

    Retirement is signalled by ``retired_at IS NOT NULL`` (the eviction janitor
    never sets a ``retired`` status — the DB CHECK forbids it), so only live
    pockets count toward the budget. Mirrors
    ``pocket_suggester._current_pocket_usage``.
    """
    result = await db.execute(
        select(func.coalesce(func.sum(PocketDefinition.storage_bytes), 0)).where(
            PocketDefinition.model_id == model_id,
            PocketDefinition.retired_at.is_(None),
        )
    )
    return int(result.scalar_one())


class RouterUnavailableError(RuntimeError):
    """Bug-8162: the query-router could not answer at all.

    Deliberately NOT a ``ValueError``. ``ValueError`` from this helper means
    "the router examined your SQL and rejected it"; this means "the router was
    unreachable, so the SQL is UNKNOWN". Callers must keep the two apart —
    reporting an outage as a rejection tells a modeller their correct SQL is
    wrong. See ``scratchpad_measures._validator_unavailable`` for the sibling
    contract this mirrors.
    """


def _router_unavailable_http(exc: Exception) -> HTTPException:
    """Bug-8162: the 503 a router outage surfaces as (never a 400/422)."""
    return validator_unavailable("pocket", exc)


async def _validate_via_router(
    model_id: UUID,
    sql: str,
    bearer: str,
    timeout_s: float = 30.0,
) -> dict:
    """POST the SQL to the query-router's /validate endpoint.

    Raises ``ValueError`` when the router returns a client-level (4xx)
    rejection — a verdict on the SQL — and ``RouterUnavailableError`` when it
    could not answer at all (network error, or a 5xx from the router/proxy).
    Bug-8162: before that split, a 5xx surfaced to the user as "Pocket SQL
    failed validation" (blaming correct SQL for the router being down) and a
    network error escaped uncaught as a 500.
    """
    url = f"{_settings.QUERY_ROUTER_URL}/api/v1/validate"
    headers = {"Authorization": f"Bearer {bearer}"}
    body = {
        "model_id": str(model_id),
        "raw_query": sql,
        "protocol": "jdbc",
        "dialect": "postgres",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise RouterUnavailableError(f"{type(exc).__name__}: {exc}") from exc

    if resp.status_code >= 500:
        raise RouterUnavailableError(f"router returned HTTP {resp.status_code}")
    if resp.status_code >= 400:
        try:
            payload = resp.json()
            detail = payload.get("detail") if isinstance(payload, dict) else None
            if not isinstance(detail, str) or not detail:
                detail = resp.text
        except Exception:
            detail = resp.text or f"Query router returned HTTP {resp.status_code}"
        raise ValueError(detail)
    return resp.json()


def _predicates_from_validation(validation: dict) -> list[dict]:
    """Build the authoritative PocketPredicate rows from the validated SQL.

    Bug-1093 / Bug-1096 (F-005): the `PocketPredicate` child rows MUST describe
    the cached slice exactly, because the query-router's matcher trusts them to
    decide routability. The only trustworthy description of the cached rows is
    the WHERE-clause the engine actually executed, so predicates are derived
    authoritatively from the router-extracted `filters` (the same sqlglot-based
    extraction the validator already ran). Any client-supplied `predicates`
    payload is ignored — it can under-describe the SQL and make a narrowed
    pocket over-claim coverage. The extraction is never branched on database
    type; it reuses the validation response verbatim.

    Bug-6989: deduplicate by (column_name, operator, value_json) before
    returning. A query with redundant predicates (``WHERE x = 1 AND x = 1``)
    produces duplicate filter entries; inserting those as PocketPredicate rows
    violates the UNIQUE constraint and surfaces a misleading 409 "already
    exists" error instead of succeeding.
    """
    normalised: list[dict] = []
    seen: set[tuple] = set()
    for f in (validation.get("filters") or []):
        column_name = str(f.get("dimension_name") or "").strip()
        operator = str(f.get("operator") or "eq").strip().lower()
        if not column_name:
            continue
        value = f.get("value")
        # Build a hashable dedup key. value may be a list, so convert to tuple.
        dedup_key = (column_name, operator, _hashable_value(value))
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        normalised.append({
            "column_name": column_name,
            "operator": operator,
            "value": value,
        })
    return normalised


def _hashable_value(val: object) -> object:
    """Convert a predicate value to a hashable form for deduplication."""
    if isinstance(val, list):
        return tuple(val)
    if isinstance(val, dict):
        return tuple(sorted(val.items()))
    return val


def _check_pocket_structure(
    validation: dict,
    model_slug: str,
) -> list[PocketViolationItem]:
    """Check pocket-specific structural rules from router /validate response.

    F-005-03: delegates to the single shared grammar
    (``shared.pocket.structure.collect_pocket_structure_violations``) so the
    model-service API, the optimizer auto-create path, and the scheduled-refresh
    path all enforce the identical model-subset contract. These are simple
    field checks — no SQL parsing.
    """
    return [
        PocketViolationItem(
            code=v.code,
            message=v.message,
            suggestion=v.suggestion,
        )
        for v in collect_pocket_structure_violations(validation, model_slug)
    ]


class PocketMetricsResponse(BaseModel):
    total_pockets: int
    fresh_pockets: int
    stale_pockets: int
    invalidating_pockets: int
    failed_pockets: int
    ineligible_pockets: int
    retired_pockets: int
    pocket_hit_rate: float
    pocket_time_saved_ms: int
    pocket_storage_bytes: int
    pocket_evictions_24h: int
    top_pockets: list[dict]
    # F-005-22: live signal when a pocket is fresh (materialised) yet has never
    # matched a query since its last refresh — a sign its predicate shape does
    # not line up with real traffic. ``zero_match_fresh_pockets`` counts those
    # pockets; ``top_skip_reason``/``top_skip_count`` surface the single most
    # common reason the router skipped a pocket route for this model in the
    # recent window, so a modeler can see *why* (e.g. ``no_tenant_filter``)
    # rather than guessing.
    zero_match_fresh_pockets: int = 0
    top_skip_reason: str | None = None
    top_skip_count: int = 0


@router.get("", response_model=list[PocketDefinitionResponse])
async def list_pockets(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> list[PocketDefinitionResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_model(db, project_id, model_id)
        result = await db.execute(
            select(PocketDefinition)
            .where(
                PocketDefinition.model_id == model_id,
                PocketDefinition.retired_at.is_(None),
            )
            .options(
                selectinload(PocketDefinition.predicates),
                selectinload(PocketDefinition.refresh_policy_row),
            )
            .order_by(PocketDefinition.created_at.desc())
        )
        return [PocketDefinitionResponse.model_validate(p) for p in result.scalars().all()]


@router.get("/metrics", response_model=PocketMetricsResponse)
async def pocket_metrics(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> PocketMetricsResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_model(db, project_id, model_id)

        result = await db.execute(
            select(PocketDefinition)
            .where(PocketDefinition.model_id == model_id)
            .order_by(PocketDefinition.hit_count.desc(), PocketDefinition.last_access_at.desc().nullslast())
        )
        pockets = list(result.scalars().all())
        active = [p for p in pockets if p.retired_at is None]
        total = len(active)
        ineligible_ids = {
            id(p)
            for p in active
            if p.status == "ineligible"
            or getattr(p, "population_eligibility", None) == "ineligible"
        }
        ineligible = len(ineligible_ids)
        fresh = sum(
            1 for p in active
            if p.status == "fresh" and id(p) not in ineligible_ids
        )
        stale = sum(1 for p in active if p.status == "stale")
        invalidating = sum(1 for p in active if p.status == "invalidating")
        failed = sum(1 for p in active if p.status == "failed")
        state_total = fresh + stale + invalidating + failed + ineligible
        if state_total != total:
            # A new/unknown lifecycle token must not be silently omitted from
            # the product metrics. Fail loudly until its producer/consumer
            # contract is updated.
            raise RuntimeError(
                "Pocket metrics status counters do not reconcile with total "
                f"({state_total} != {total})"
            )
        retired = sum(1 for p in pockets if p.retired_at is not None)
        # F-005-08 (Bug-2251): `time_saved_ms_total` is now the per-pocket sum of
        # (source baseline − pocket execution) recorded at route time, so this
        # total is genuine time SAVED, not time spent on pockets.
        time_saved = sum(int(p.time_saved_ms_total or 0) for p in active)
        storage = sum(int(p.storage_bytes or 0) for p in active)
        day_ago = datetime.now(timezone.utc) - timedelta(hours=24)
        evictions_24h = sum(
            1
            for p in pockets
            if p.retired_at is not None
            and (p.retired_at if p.retired_at.tzinfo else p.retired_at.replace(tzinfo=timezone.utc)) >= day_ago
        )
        # F-005-08 (Bug-2251): hit RATIO = pocket-routed queries / all queries
        # for this model (the business question the design doc promises),
        # computed from QueryLog route_type proportions — NOT total hits / pocket
        # count (which could exceed 1.0 and answered no real question).
        route_counts = await db.execute(
            select(QueryLog.route_type, func.count())
            .where(QueryLog.model_id == model_id, QueryLog.status == "success")
            .group_by(QueryLog.route_type)
        )
        counts = {rt: int(c) for rt, c in route_counts.all()}
        total_queries = sum(counts.values())
        pocket_queries = counts.get("pocket", 0)
        hit_rate = (pocket_queries / total_queries) if total_queries else 0.0

        # F-005-22: a fresh pocket that has not matched a query since its last
        # refresh is silently providing zero value. "matched since refresh" is
        # true when last_match_at is set and is at or after last_refresh_at.
        def _matched_since_refresh(p: PocketDefinition) -> bool:
            if p.last_match_at is None:
                return False
            if p.last_refresh_at is None:
                return True
            lm = p.last_match_at if p.last_match_at.tzinfo else p.last_match_at.replace(tzinfo=timezone.utc)
            lr = p.last_refresh_at if p.last_refresh_at.tzinfo else p.last_refresh_at.replace(tzinfo=timezone.utc)
            return lm >= lr

        zero_match_fresh = sum(
            1
            for p in active
            if p.status == "fresh"
            and id(p) not in ineligible_ids
            and not _matched_since_refresh(p)
        )

        # F-005-22: surface the single most common reason the router skipped a
        # pocket route for this model over the recent window, read from the
        # already-persisted RouteLog.detail JSON ('pocket_skipped_reason'). This
        # reuses existing telemetry — the matcher is untouched.
        top_skip_reason: str | None = None
        top_skip_count = 0
        if zero_match_fresh:
            skip_window = datetime.now(timezone.utc) - timedelta(days=7)
            skip_expr = RouteLog.detail["pocket_skipped_reason"].astext
            skip_rows = await db.execute(
                select(skip_expr, func.count())
                .join(QueryLog, QueryLog.id == RouteLog.query_log_id)
                .where(
                    QueryLog.model_id == model_id,
                    RouteLog.created_at >= skip_window,
                    skip_expr.is_not(None),
                )
                .group_by(skip_expr)
                .order_by(func.count().desc())
                .limit(1)
            )
            top = skip_rows.first()
            if top is not None and top[0]:
                top_skip_reason = str(top[0])
                top_skip_count = int(top[1])

        top_pockets = [
            {
                "pocket_id": str(p.id),
                "physical_table_name": p.physical_table_name,
                "status": p.status,
                "population_eligibility": getattr(p, "population_eligibility", "unknown"),
                "hit_count": int(p.hit_count or 0),
                "ttl_days": int(p.ttl_days or 0),
                "matched_since_refresh": _matched_since_refresh(p),
            }
            for p in active[:10]
        ]
        return PocketMetricsResponse(
            total_pockets=total,
            fresh_pockets=fresh,
            stale_pockets=stale,
            invalidating_pockets=invalidating,
            failed_pockets=failed,
            ineligible_pockets=ineligible,
            retired_pockets=retired,
            pocket_hit_rate=round(hit_rate, 4),
            pocket_time_saved_ms=time_saved,
            pocket_storage_bytes=storage,
            pocket_evictions_24h=evictions_24h,
            top_pockets=top_pockets,
            zero_match_fresh_pockets=zero_match_fresh,
            top_skip_reason=top_skip_reason,
            top_skip_count=top_skip_count,
        )


@router.get("/{pocket_id}", response_model=PocketDefinitionResponse)
async def get_pocket(
    project_id: UUID,
    model_id: UUID,
    pocket_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> PocketDefinitionResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_model(db, project_id, model_id)
        result = await db.execute(
            select(PocketDefinition)
            .where(PocketDefinition.id == pocket_id)
            .options(
                selectinload(PocketDefinition.predicates),
                selectinload(PocketDefinition.refresh_policy_row),
            )
        )
        pocket = result.scalar_one_or_none()
        if pocket is None or pocket.model_id != model_id:
            raise HTTPException(status_code=404, detail="Pocket not found")
        return PocketDefinitionResponse.model_validate(pocket)


async def _pocket_combo_error(db, model_id: UUID, target: DataTarget) -> str | None:
    """Return an error string if `target`'s connector cannot pair with the
    model's source connector for pocket materialisation, else None.

    Bug-5898: shared by create/update and by validate/dry-run so "validated"
    always means "this target combination can actually be created and
    refreshed" — see unsupported_pocket_combo_reason for the authoritative
    rule (mirrored from shared/pocket/refresh.py).
    """
    target_conn_id = getattr(target, "project_connection_id", None)
    target_conn = await db.get(ProjectConnection, target_conn_id) if target_conn_id else None
    if target_conn is None:
        return None
    target_connector = normalize_connection_type((target_conn.connection_type or "").lower())
    source_conn = await resolve_source_connection(model_id, db)
    source_connector = await resolve_connector_type(source_conn)
    cross_db = not is_same_database(source_conn, target_conn)
    return unsupported_pocket_combo_reason(source_connector, target_connector, cross_db)


@router.post(
    "",
    response_model=PocketDefinitionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_pocket(
    project_id: UUID,
    model_id: UUID,
    body: PocketDefinitionCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> PocketDefinitionResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        model = await _get_scoped_model(db, project_id, model_id)

        try:
            validation = await _validate_via_router(
                model_id, body.defining_sql, current_user.raw_token
            )
        except RouterUnavailableError as exc:
            # Bug-8162: unreachable validator — refuse, but as 503 ("unknown,
            # retry"), never 400 ("your SQL is wrong").
            raise _router_unavailable_http(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        if not validation.get("ok"):
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "Pocket SQL failed validation.",
                    "errors": validation.get("errors", []),
                },
            )

        slug = (getattr(model, "slug", "") or "").lower()
        violations = _check_pocket_structure(validation, slug)
        if violations:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "Pocket SQL is not a valid model subset.",
                    "violations": [v.model_dump() for v in violations],
                },
            )

        # Bug-6995: reject a user-authored LIMIT clause in the defining SQL.
        # The validate endpoint handles LIMIT for its LIMIT-1 probe, but the
        # create path previously accepted LIMIT silently. A pocket with LIMIT
        # materialises an arbitrarily capped subset — semantically wrong for a
        # cache that claims full-slice coverage, so the pocket matcher may
        # serve a query whose result set is incomplete.
        sql_text = (body.defining_sql or "").strip().rstrip(";").strip()
        if re.search(r"\bLIMIT\b", sql_text, re.IGNORECASE):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Pocket SQL must not contain a LIMIT clause. A pocket "
                    "materialises a full subset; LIMIT would produce an "
                    "incomplete cache that the matcher incorrectly treats "
                    "as complete."
                ),
            )

        target = await db.get(DataTarget, body.target_id)
        if target is None or target.model_id != model_id:
            raise HTTPException(status_code=400, detail="Invalid target_id for model")

        # Bug-5200 / Bug-5475: reject an unsupported source/target connector
        # combination at creation rather than letting the pocket persist and
        # fail (or hang) on every refresh. Pocket materialisation supports a
        # postgresql/redshift/bigquery target, but only same-connector
        # combinations: a BigQuery target requires a BigQuery source (atomic
        # CREATE OR REPLACE), and cross-database streaming is PG-family only.
        # The authoritative validation lives in shared/pocket/refresh.py
        # (unsupported_pocket_combo_reason); this is the create-time mirror.
        combo_error = await _pocket_combo_error(db, model_id, target)
        if combo_error is not None:
            raise HTTPException(status_code=400, detail=combo_error)

        # Bug-7005: enforce the effective pocket size budget on manual/UI create.
        # The optimizer auto-create path
        # (services/optimizer/src/lifecycle/pocket_creator.py) refuses a new
        # pocket once the model's resolved budget is exhausted, but manual creation
        # bypassed it entirely — a modeler could add pockets past the configured
        # cap. A manual pocket is not materialised at create time (status="stale",
        # storage_bytes NULL), so we cannot size the new slice here; the faithful
        # mirror of the optimizer's "budget exhausted" guard is to refuse a new
        # pocket when live usage already meets or exceeds the budget. The refresh
        # path (shared/pocket/refresh.py) remains the post-materialisation check.
        budget = await _resolve_effective_pocket_budget(db, model)
        if budget is not None:
            used = await _current_pocket_usage_bytes(db, model_id)
            if used >= budget:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Pocket storage budget exhausted "
                        f"({used} of {budget} bytes used); retire a pocket or "
                        f"raise the budget before creating another."
                    ),
                )

        allowed = await get_setting("pocket.allowed_refresh_policies", tenant_session=db)
        if body.refresh_policy not in set(allowed or []):
            raise HTTPException(
                status_code=400,
                detail=f"refresh_policy must be one of {allowed}",
            )

        # Bug-6993: PocketDefinitionCreate.ttl_days now carries a schema-level
        # gt=0 constraint, so a non-positive value 422s before this handler
        # ever runs — the previous "silently substitute the tenant's
        # pocket.ttl_days config default for a caller-supplied 0" fallback is
        # unreachable and has been removed rather than left as dead code.
        ttl_days = int(body.ttl_days)

        seed = model.seed or secrets.token_hex(6)
        suffix = secrets.token_hex(4)
        physical_table_name = f"pocket_{seed}_{suffix}"

        # Bug-1096 (F-005): derive the predicate rows authoritatively from the
        # validated SQL's extracted filters. Client-supplied `body.predicates`
        # are ignored — trusting them lets a caller persist a predicate set that
        # under-describes (over-claims coverage of) the cached rows, which the
        # matcher would then route incomplete results to with no error.
        normalised_predicates = _predicates_from_validation(validation)

        # F-005-19 (Bug-2260): an empty query_fingerprint makes every such
        # pocket collide on the (model_id, fingerprint, predicate_set_hash)
        # identity index. The router always returns a fingerprint for a valid
        # parse; an empty one means the validation contract was not met, so fail
        # loud rather than persist a degenerate identity.
        fingerprint = (validation.get("query_fingerprint") or "").strip()
        if not fingerprint:
            raise HTTPException(
                status_code=400,
                detail="Pocket SQL produced no query fingerprint; cannot establish a stable pocket identity.",
            )
        pocket = PocketDefinition(
            model_id=model_id,
            target_id=body.target_id,
            physical_table_name=physical_table_name,
            defining_sql=body.defining_sql,
            query_fingerprint=fingerprint,
            predicate_set_hash=predicate_set_hash(normalised_predicates),
            refresh_policy=body.refresh_policy,
            refresh_cron=body.refresh_cron,
            incremental_column=body.incremental_column,
            incremental_lookback_hours=body.incremental_lookback_hours,
            ttl_days=ttl_days,
            status="stale",
        )
        db.add(pocket)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A pocket with the same query shape and predicate set already exists for this model.",
            )

        for pred in normalised_predicates:
            db.add(PocketPredicate(
                pocket_definition_id=pocket.id,
                column_name=pred["column_name"],
                operator=pred["operator"],
                value_json={"value": pred["value"]},
            ))

        # Bug-5199 / Bug-7007: when the create payload requests a scheduled
        # refresh, create the PocketRefreshPolicy child row IN THE SAME
        # transaction so the scheduler's refresh_due_pockets query (which
        # filters on enabled PocketRefreshPolicy rows with a cron) picks it up.
        # Bug-7007: the complete initial policy travels in the create payload so
        # this is the ONLY transaction — the caller no longer needs a second
        # PUT .../refresh/policy request to finish configuring the pocket (which
        # could fail and leave the pocket persisted with a mismatched policy).
        # ``refresh_policy_enabled`` defaults to enabled for a scheduled pocket
        # when the caller omits it (the prior always-enabled behaviour).
        if body.refresh_policy == "schedule" and body.refresh_cron:
            policy_enabled = (
                body.refresh_policy_enabled
                if body.refresh_policy_enabled is not None
                else True
            )
            db.add(PocketRefreshPolicy(
                pocket_definition_id=pocket.id,
                cron_expression=body.refresh_cron,
                is_enabled=policy_enabled,
            ))

        await db.commit()

        result = await db.execute(
            select(PocketDefinition)
            .where(PocketDefinition.id == pocket.id)
            .options(
                selectinload(PocketDefinition.predicates),
                selectinload(PocketDefinition.refresh_policy_row),
            )
        )
        persisted = result.scalar_one()
        return PocketDefinitionResponse.model_validate(persisted)


# Cron fields cannot contain whitespace-free garbage; the structural fallback
# accepts only cron-legal characters (digits, ``* / , -`` and month/day names).
_CRON_FIELD_RE = re.compile(r"[0-9*/,\-A-Za-z]+")


def _validate_pocket_cron(value: str) -> str:
    """Bug-6108: reject an unparseable ``refresh_cron`` so a PATCH cannot write a
    schedule the sweep can never fire.

    Bug-6593: this is now defense-in-depth. The authoritative gate is the
    ``PocketDefinitionUpdate`` schema validator (``_check_refresh_cron``), which
    rejects an unparseable cron at the request-body boundary with a Pydantic 422
    — the same status ``PocketDefinitionCreate`` returns for the same malformed
    input, keeping create and update consistent. By the time this handler runs
    the cron has already passed that check, so this call only ever sees a valid,
    trimmed value; it stays as a guard for any internal caller that constructs
    ``updates`` without going through the schema.

    Uses ``croniter`` for exact validation when it is importable, and falls back
    to a structural 5/6-field check otherwise. ``croniter`` is a transitive dep
    of the shared package (pythonpath-wired, not pip-installed into the
    model-service image), so it may be absent at runtime — the import must never
    hard-fail the request (the scheduler's is_due check is the ultimate safety
    net for a subtly-invalid cron; this guard catches obvious garbage).
    """
    cron = value.strip()
    try:
        from croniter import croniter  # type: ignore

        ok = bool(croniter.is_valid(cron))
    except ImportError:
        parts = cron.split()
        ok = len(parts) in (5, 6) and all(
            _CRON_FIELD_RE.fullmatch(p) for p in parts
        )
    if not ok:
        raise HTTPException(
            status_code=400, detail=f"Invalid cron expression: {value!r}"
        )
    return cron


async def _prepare_pocket_definition_update(
    db,
    *,
    model: Model,
    pocket_id: UUID,
    pocket_status: str | None,
    body: PocketDefinitionUpdate,
    current_user: CurrentUser,
) -> tuple[dict, list[dict] | None]:
    """Validate a definition edit using the canonical PATCH contract.

    Compound history edits and ordinary PATCHes must have identical semantic
    gates before either one mutates a pocket.  Keeping this preparation phase
    side-effect free also lets the compound route validate its child policy in
    the same transaction without a weaker parallel SQL validator.
    """
    updates = body.model_dump(exclude_unset=True)
    rebuilt_predicates: list[dict] | None = None

    if "refresh_policy" in updates and updates["refresh_policy"] is not None:
        allowed = await get_setting(
            "pocket.allowed_refresh_policies", tenant_session=db
        )
        if updates["refresh_policy"] not in set(allowed or []):
            raise HTTPException(
                status_code=400,
                detail=f"refresh_policy must be one of {allowed}",
            )
    if "refresh_cron" in updates and updates["refresh_cron"]:
        updates["refresh_cron"] = _validate_pocket_cron(updates["refresh_cron"])

    if "defining_sql" in updates and updates["defining_sql"]:
        try:
            validation = await _validate_via_router(
                model.id, updates["defining_sql"], current_user.raw_token
            )
        except RouterUnavailableError as exc:
            raise _router_unavailable_http(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if not validation.get("ok"):
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "Pocket SQL failed validation.",
                    "errors": validation.get("errors", []),
                },
            )
        slug = (getattr(model, "slug", "") or "").lower()
        violations = _check_pocket_structure(validation, slug)
        if violations:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "Pocket SQL is not a valid model subset.",
                    "violations": [v.model_dump() for v in violations],
                },
            )
        updates["query_fingerprint"] = validation.get("query_fingerprint") or ""
        rebuilt_predicates = _predicates_from_validation(validation)
        updates["predicate_set_hash"] = predicate_set_hash(rebuilt_predicates)
        # A definition edit changes the physical generation and clears both
        # axes.  The next matcher proof may establish eligibility, but it may
        # never make this old physical table fresh; only a successful rebuild
        # may do that.
        updates.update(
            status="stale",
            failure_reason=None,
            population_eligibility="unknown",
            population_eligibility_reason=None,
            population_proof_fingerprint=None,
            row_manifest=None,
            active_refresh_run_id=None,
            built_for_version_id=None,
            built_for_epoch=None,
        )

    return updates, rebuilt_predicates


async def _apply_pocket_definition_update(
    db,
    *,
    pocket: PocketDefinition,
    pocket_id: UUID,
    updates: dict,
    rebuilt_predicates: list[dict] | None,
) -> None:
    """Apply an already validated definition edit and its authoritative rows."""
    for key, value in updates.items():
        if hasattr(pocket, key):
            setattr(pocket, key, value)
    if rebuilt_predicates is None:
        return
    await db.execute(
        delete(PocketPredicate).where(
            PocketPredicate.pocket_definition_id == pocket_id
        )
    )
    for predicate in rebuilt_predicates:
        db.add(PocketPredicate(
            pocket_definition_id=pocket_id,
            column_name=predicate["column_name"],
            operator=predicate["operator"],
            value_json={"value": predicate["value"]},
        ))


async def _commit_pocket_definition_write(db) -> None:
    """Commit a pocket definition edit with the canonical identity conflict."""
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "A pocket with the same query shape and predicate set already "
                "exists for this model."
            ),
        )


@router.post(
    "/{pocket_id}/compound-edit",
    response_model=PocketDefinitionResponse,
    dependencies=[require_role("modeler")],
)
async def compound_edit_pocket(
    project_id: UUID,
    model_id: UUID,
    pocket_id: UUID,
    body: PocketCompoundEdit,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> PocketDefinitionResponse:
    """Apply definition and refresh policy in one tenant transaction.

    Drawer history replays this endpoint so one compound edit has one commit,
    one cache invalidation boundary, and one revision delta.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        model = await _get_scoped_model(db, project_id, model_id)
        await acquire_model_definition_lock(db, model_id)
        pocket = await db.get(PocketDefinition, pocket_id)
        if pocket is None or pocket.model_id != model_id:
            raise HTTPException(status_code=404, detail="Pocket not found")
        updates, rebuilt_predicates = await _prepare_pocket_definition_update(
            db,
            model=model,
            pocket_id=pocket_id,
            pocket_status=getattr(pocket, "status", None),
            body=body.definition,
            current_user=current_user,
        )
        await _apply_pocket_definition_update(
            db,
            pocket=pocket,
            pocket_id=pocket_id,
            updates=updates,
            rebuilt_predicates=rebuilt_predicates,
        )
        policy_data = body.policy.model_dump()
        policy = (await db.execute(select(PocketRefreshPolicy).where(
            PocketRefreshPolicy.pocket_definition_id == pocket_id,
        ))).scalar_one_or_none()
        if policy is None:
            policy = PocketRefreshPolicy(pocket_definition_id=pocket_id, **policy_data)
            db.add(policy)
        else:
            for key, value in policy_data.items():
                setattr(policy, key, value)
        await _commit_pocket_definition_write(db)
        result = await db.execute(select(PocketDefinition).where(
            PocketDefinition.id == pocket_id,
        ).options(
            selectinload(PocketDefinition.predicates),
            selectinload(PocketDefinition.refresh_policy_row),
        ))
        return PocketDefinitionResponse.model_validate(result.scalar_one())


@router.patch(
    "/{pocket_id}",
    response_model=PocketDefinitionResponse,
    dependencies=[require_role("modeler")],
)
async def update_pocket(
    project_id: UUID,
    model_id: UUID,
    pocket_id: UUID,
    body: PocketDefinitionUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> PocketDefinitionResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        model = await _get_scoped_model(db, project_id, model_id)
        pocket = await db.get(PocketDefinition, pocket_id)
        if pocket is None or pocket.model_id != model_id:
            raise HTTPException(status_code=404, detail="Pocket not found")

        updates, rebuilt_predicates = await _prepare_pocket_definition_update(
            db,
            model=model,
            pocket_id=pocket_id,
            pocket_status=getattr(pocket, "status", None),
            body=body,
            current_user=current_user,
        )
        await _apply_pocket_definition_update(
            db,
            pocket=pocket,
            pocket_id=pocket_id,
            updates=updates,
            rebuilt_predicates=rebuilt_predicates,
        )

        # Bug-6108: the scheduler's refresh_due_pockets query reads the
        # PocketRefreshPolicy child row (enabled + cron), NOT pocket.refresh_cron
        # (a deprecated column). A PATCH that changed the schedule must therefore
        # update the policy row, mirroring create (Bug-5199) and the PUT
        # /refresh/policy endpoint — otherwise the schedule change never takes
        # effect. Sync only when the schedule fields were actually touched.
        #
        # Bug-6811: the child PocketRefreshPolicy row is the single source of
        # truth for schedule state (the scheduler reads it, PUT /refresh/policy
        # writes it). A PATCH that sets refresh_policy="schedule" without also
        # supplying refresh_cron must NOT overwrite the child row's cron with
        # the parent's stale/None value. Use the child row's existing cron when
        # the PATCH did not supply a new one.
        if "refresh_policy" in updates or "refresh_cron" in updates:
            policy_row = (
                await db.execute(
                    select(PocketRefreshPolicy).where(
                        PocketRefreshPolicy.pocket_definition_id == pocket_id
                    )
                )
            ).scalar_one_or_none()
            if pocket.refresh_policy == "schedule":
                # Determine the effective cron: prefer the PATCH-supplied value,
                # fall back to the child row's existing value, then the
                # deprecated parent column (last resort).
                effective_cron = (
                    updates.get("refresh_cron")
                    or (policy_row.cron_expression if policy_row else None)
                    or pocket.refresh_cron
                )
                if effective_cron:
                    if policy_row is None:
                        db.add(PocketRefreshPolicy(
                            pocket_definition_id=pocket_id,
                            cron_expression=effective_cron,
                            is_enabled=True,
                        ))
                    else:
                        policy_row.cron_expression = effective_cron
                        policy_row.is_enabled = True
            elif policy_row is not None:
                # Switched away from a scheduled refresh — stop the sweep from
                # firing on the stale cron instead of leaving it enabled.
                policy_row.is_enabled = False

        # Keep the identity conflict contract shared with compound history
        # edits: the same partial unique index must produce the same 409 and
        # rollback rather than leaking a raw IntegrityError.
        await _commit_pocket_definition_write(db)
        result = await db.execute(
            select(PocketDefinition)
            .where(PocketDefinition.id == pocket_id)
            .options(
                selectinload(PocketDefinition.predicates),
                selectinload(PocketDefinition.refresh_policy_row),
            )
        )
        return PocketDefinitionResponse.model_validate(result.scalar_one())


@router.post(
    "/{pocket_id}/refresh",
    response_model=PocketRefreshRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[require_role("modeler")],
)
async def refresh_pocket(
    project_id: UUID,
    model_id: UUID,
    pocket_id: UUID,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> PocketRefreshRunResponse:
    """F-005-23: queue the refresh and return 202 immediately.

    A manual refresh runs a full CTAS over the pocket's slice, which can take
    far longer than a Cloud Run request timeout for a large pocket. Rather than
    block the request on materialisation, this creates a ``queued``
    :class:`PocketRefreshRun` synchronously, schedules the actual refresh on a
    background task (which adopts the same run row), and returns 202 with the
    run id. Clients poll ``GET .../refresh/runs`` (or the run's status) until it
    reaches ``completed``/``failed``.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_model(db, project_id, model_id)
        pocket = await db.get(PocketDefinition, pocket_id)
        if pocket is None or pocket.model_id != model_id:
            raise HTTPException(status_code=404, detail="Pocket not found")
        if getattr(pocket, "population_eligibility", None) == POCKET_POPULATION_ELIGIBILITY_INELIGIBLE:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=POCKET_INELIGIBLE_POPULATION_REASON,
            )

        run = PocketRefreshRun(
            pocket_definition_id=pocket_id,
            refresh_mode="manual",
            status="queued",
            triggered_by="api",
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        run_id = run.id

        # Starlette awaits async background tasks within the event loop AFTER the
        # response is sent, so the heavy CTAS no longer holds the request open
        # (the Cloud Run timeout risk in F-005-23). The task opens its own DB
        # session and adopts this queued run via existing_run_id.
        background_tasks.add_task(
            _run_pocket_refresh_in_background,
            current_user.tenant_id,
            pocket_id,
            run_id,
            current_user.raw_token or None,
        )
        return PocketRefreshRunResponse.model_validate(run)


@router.get("/{pocket_id}/refresh/runs", response_model=list[PocketRefreshRunResponse])
async def list_pocket_runs(
    project_id: UUID,
    model_id: UUID,
    pocket_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> list[PocketRefreshRunResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_model(db, project_id, model_id)
        pocket = await db.get(PocketDefinition, pocket_id)
        if pocket is None or pocket.model_id != model_id:
            raise HTTPException(status_code=404, detail="Pocket not found")

        result = await db.execute(
            select(PocketRefreshRun)
            .where(PocketRefreshRun.pocket_definition_id == pocket_id)
            .order_by(PocketRefreshRun.started_at.desc())
            .limit(100)
        )
        return [PocketRefreshRunResponse.model_validate(r) for r in result.scalars().all()]


@router.delete(
    "/{pocket_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("admin")],
)
async def delete_pocket(
    project_id: UUID,
    model_id: UUID,
    pocket_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_model(db, project_id, model_id)
        pocket = await db.get(PocketDefinition, pocket_id)
        if pocket is None or pocket.model_id != model_id:
            raise HTTPException(status_code=404, detail="Pocket not found")
        # Capture the scalar before any rollback can expire the ORM identity
        # map. The failure path below must remain safe even when a real
        # AsyncSession would raise while lazily reading ``pocket.id`` after
        # rollback (Bug-8579).
        pocket_id_scalar = pocket.id

        try:
            # Bug-9430: serialize destructive delete with the same dedicated
            # per-pocket lock held across refresh CTAS/streaming. An in-flight
            # refresh is a retryable conflict, never a partially deleted
            # pocket.
            async with pocket_refresh_lock(db, pocket_id_scalar):
                try:
                    await drop_pocket_storage(pocket, db)
                except Exception as exc:
                    # Bug-8579: a storage-drop failure is not success. Keep the
                    # metadata and physical identity intact so the operator or
                    # reclamation sweep can retry; return the established
                    # retryable service error instead of swallowing evidence.
                    await db.rollback()
                    logger.exception(
                        "Pocket %s delete could not reclaim physical storage; "
                        "metadata was retained for retry",
                        pocket_id_scalar,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail=(
                            "Pocket storage could not be reclaimed. The pocket "
                            "was not deleted; retry after the target is available."
                        ),
                    ) from exc

                await db.delete(pocket)
                await db.commit()
        except PocketRefreshInFlightError as exc:
            await db.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        # Bug-8581: mechanism 2 of the result-cache invalidation contract
        # (shared/cache/result_cache.py) — clear the receiving replica NOW so the
        # operator who just deleted a pocket does not keep seeing route_type=pocket
        # with its id on the very next query. Mechanism 1 (the pocket_generation
        # component of the cache key) is what makes the invalidation correct
        # across replicas; this call is the immediate single-replica half, exactly
        # as deploy/undeploy/revert and the row-security mutations do it.
        # Best-effort by contract: the delete has already committed and must not
        # fail on an unreachable query-router.
        await _evict_query_router_cache(model_id, current_user.tenant_id)
        return None


# ---------------------------------------------------------------------------
# Refresh policy (schedule) — 1:1 with PocketDefinition
# ---------------------------------------------------------------------------


@router.get("/{pocket_id}/refresh/policy", response_model=PocketRefreshPolicyResponse)
async def get_pocket_refresh_policy(
    project_id: UUID,
    model_id: UUID,
    pocket_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> PocketRefreshPolicyResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_model(db, project_id, model_id)
        pocket = await db.get(PocketDefinition, pocket_id)
        if pocket is None or pocket.model_id != model_id:
            raise HTTPException(status_code=404, detail="Pocket not found")

        result = await db.execute(
            select(PocketRefreshPolicy).where(
                PocketRefreshPolicy.pocket_definition_id == pocket_id
            )
        )
        policy = result.scalar_one_or_none()
        if policy is None:
            raise HTTPException(status_code=404, detail="Refresh policy not configured")
        return PocketRefreshPolicyResponse.model_validate(policy)


@router.put(
    "/{pocket_id}/refresh/policy",
    response_model=PocketRefreshPolicyResponse,
    dependencies=[require_role("modeler")],
)
async def upsert_pocket_refresh_policy(
    project_id: UUID,
    model_id: UUID,
    pocket_id: UUID,
    body: PocketRefreshPolicyUpsert,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> PocketRefreshPolicyResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_model(db, project_id, model_id)
        pocket = await db.get(PocketDefinition, pocket_id)
        if pocket is None or pocket.model_id != model_id:
            raise HTTPException(status_code=404, detail="Pocket not found")

        result = await db.execute(
            select(PocketRefreshPolicy).where(
                PocketRefreshPolicy.pocket_definition_id == pocket_id
            )
        )
        policy = result.scalar_one_or_none()
        if policy is None:
            policy = PocketRefreshPolicy(
                pocket_definition_id=pocket_id, **body.model_dump()
            )
            db.add(policy)
        else:
            for k, v in body.model_dump().items():
                setattr(policy, k, v)
        await db.commit()
        await db.refresh(policy)
        return PocketRefreshPolicyResponse.model_validate(policy)


# ---------------------------------------------------------------------------
# Validate + dry-run (authoring-time helpers) — routed via the query-router
# ---------------------------------------------------------------------------


async def _route_query(
    model_id: UUID,
    sql: str,
    bearer: str,
    timeout_s: float = 60.0,
) -> dict:
    """POST the SQL to the query-router's /execute endpoint.

    Bug-8453: raises :class:`RowSecurityDeniedError` on the deny-all sentinel.
    A denial rewrites the query to ``... WHERE 0 = 1``, over which the dry-run's
    ``SELECT COUNT(*)`` still returns a row containing **0** — so without this
    the modeller is shown an authoritative "this pocket would hold 0 rows" that
    is not a measurement at all, and could size or reject a pocket on it.
    """
    url = f"{_settings.QUERY_ROUTER_URL}/api/v1/execute"
    headers = {"Authorization": f"Bearer {bearer}"}
    body = {
        "model_id": str(model_id),
        "raw_query": sql,
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
            raise ValueError(detail)
        payload = resp.json()
        # Bug-8453: classify through the shared execute contract, never by
        # inspecting the returned rows.
        if execute_response_denied_all(payload):
            raise RowSecurityDeniedError(
                "Row-level security denied access to every row for this query."
            )
        return payload


@router.post(
    "/validate",
    response_model=PocketValidateResponse,
    dependencies=[require_role("modeler")],
)
async def validate_pocket_sql(
    project_id: UUID,
    model_id: UUID,
    body: PocketValidateRequest,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> PocketValidateResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        model = await _get_scoped_model(db, project_id, model_id)

        sql = (body.defining_sql or "").strip().rstrip(";").strip()
        if not sql:
            return PocketValidateResponse(ok=False, stage="parse", error="SQL is empty")

        # Bug-5898: validate target_id when supplied — same checks as create
        # (target ownership + source/target connector compatibility), so a
        # "validated" pocket cannot be rejected later by save for a reason
        # validate never looked at.
        if body.target_id is not None:
            target = await db.get(DataTarget, body.target_id)
            if target is None or target.model_id != model_id:
                return PocketValidateResponse(
                    ok=False, stage="parse",
                    error="Invalid target_id for this model.",
                )
            combo_error = await _pocket_combo_error(db, model_id, target)
            if combo_error is not None:
                return PocketValidateResponse(ok=False, stage="parse", error=combo_error)

        try:
            validation = await _validate_via_router(
                model_id, sql, current_user.raw_token
            )
        except RouterUnavailableError as exc:
            # Bug-8162: ``ok=false, stage="parse"`` states a verdict on the
            # user's SQL. An unreachable validator has no verdict, so this
            # leaves the 200 envelope entirely and answers 503 — the same
            # "unknown, retry" signal create/update give.
            raise _router_unavailable_http(exc) from exc
        except ValueError as exc:
            return PocketValidateResponse(ok=False, stage="parse", error=str(exc))

        if not validation.get("ok"):
            errors = validation.get("errors") or []
            return PocketValidateResponse(
                ok=False,
                stage="parse",
                error="; ".join(errors) if errors else "SQL validation failed.",
            )

        slug = (getattr(model, "slug", "") or "").lower()
        violations = _check_pocket_structure(validation, slug)
        if violations:
            return PocketValidateResponse(
                ok=False,
                stage="subset",
                error="; ".join(v.message for v in violations),
                violations=violations,
            )

        # F-005-19 (Bug-2260): detect an existing LIMIT by word boundary, not a
        # bare substring — `"LIMIT" not in sql.upper()` misfired on a column
        # named e.g. `credit_limit`, skipping the probe cap and scanning the
        # full slice. `\bLIMIT\b` matches the keyword only.
        probe_sql = sql if re.search(r"\bLIMIT\b", sql, re.IGNORECASE) else f"{sql} LIMIT 1"
        try:
            result = await _route_query(model_id, probe_sql, current_user.raw_token)
            columns = result.get("columns")
        except RowSecurityDeniedError:
            # Bug-8453 [fail closed]: do NOT certify the SQL as validated when
            # the probe never actually read a row. Certifying here would let a
            # pocket be authored and saved on the strength of a read that row
            # security refused.
            return PocketValidateResponse(
                ok=False,
                stage="row_security",
                error=(
                    "Row-level security denies your account access to every row "
                    "of this model, so the validation probe could not read any "
                    "data. This is a permissions restriction, not a problem with "
                    "the SQL. Ask an administrator to grant access, or validate "
                    "from an account with access to this data."
                ),
            )
        except Exception as exc:
            return PocketValidateResponse(ok=False, stage="probe", error=str(exc))

        return PocketValidateResponse(ok=True, stage="probe", columns=columns)


@router.post(
    "/dry-run",
    response_model=PocketDryRunResponse,
    dependencies=[require_role("modeler")],
)
async def dry_run_pocket_sql(
    project_id: UUID,
    model_id: UUID,
    body: PocketDryRunRequest,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> PocketDryRunResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        model = await _get_scoped_model(db, project_id, model_id)

        sql = (body.defining_sql or "").strip().rstrip(";").strip()
        if not sql:
            return PocketDryRunResponse(ok=False, error="SQL is empty")

        # Bug-5898: validate target_id when supplied — same checks as create.
        if body.target_id is not None:
            target = await db.get(DataTarget, body.target_id)
            if target is None or target.model_id != model_id:
                return PocketDryRunResponse(ok=False, error="Invalid target_id for this model.")
            combo_error = await _pocket_combo_error(db, model_id, target)
            if combo_error is not None:
                return PocketDryRunResponse(ok=False, error=combo_error)

        # Bug-5898: run subset validation before the count probe so dry-run
        # rejects SQL that create/update would also reject.
        try:
            validation = await _validate_via_router(
                model_id, sql, current_user.raw_token
            )
        except RouterUnavailableError as exc:
            # Bug-8162: same as the validate endpoint — no verdict to report.
            raise _router_unavailable_http(exc) from exc
        except ValueError as exc:
            return PocketDryRunResponse(ok=False, error=str(exc))
        if not validation.get("ok"):
            errors = validation.get("errors") or []
            return PocketDryRunResponse(
                ok=False,
                error="; ".join(errors) if errors else "SQL validation failed.",
            )
        slug = (getattr(model, "slug", "") or "").lower()
        violations = _check_pocket_structure(validation, slug)
        if violations:
            return PocketDryRunResponse(
                ok=False,
                error="; ".join(v.message for v in violations),
            )

        count_sql = f"SELECT COUNT(*) AS __c FROM ({sql}) AS __t"

        timeout_s = float(
            await get_setting("gateway.router_client_timeout_xlong", tenant_session=db) or 300
        )

        started = time.monotonic()
        try:
            result = await _route_query(model_id, count_sql, current_user.raw_token, timeout_s)
        except RowSecurityDeniedError:
            # Bug-8453 [wrong-number guard]: COUNT(*) over `WHERE 0 = 1`
            # returns 0. Reporting ok=True/row_count=0 would present a
            # permissions denial as a measured pocket size.
            return PocketDryRunResponse(
                ok=False,
                error=(
                    "Row-level security denies your account access to every row "
                    "of this model, so the pocket size could not be measured. "
                    "The result is NOT zero rows — it is unknown from your "
                    "account. Ask an administrator to grant access, or dry-run "
                    "from an account with access to this data."
                ),
            )
        except Exception as exc:
            return PocketDryRunResponse(ok=False, error=str(exc))

        elapsed_ms = int((time.monotonic() - started) * 1000)
        rows = result.get("rows") or []
        row_count = int(rows[0].get("__c", 0)) if rows else 0
        return PocketDryRunResponse(ok=True, row_count=row_count, elapsed_ms=elapsed_ms)
