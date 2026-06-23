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
from shared.db.models import (
    DataTarget,
    Model,
    PocketDefinition,
    PocketPredicate,
    PocketRefreshPolicy,
    PocketRefreshRun,
    ProjectConnection,
    QueryLog,
    RouteLog,
)
from shared.schemas.connection_type import normalize_connection_type
from shared.db.session import get_tenant_db
from shared.pocket.fingerprint import predicate_set_hash
from shared.pocket.refresh import drop_pocket_storage, refresh_pocket_definition
from shared.pocket.structure import collect_pocket_structure_violations
from shared.schemas.pydantic_models import (
    PocketDefinitionCreate,
    PocketDefinitionResponse,
    PocketDefinitionUpdate,
    PocketDryRunRequest,
    PocketDryRunResponse,
    PocketRefreshPolicyResponse,
    PocketRefreshPolicyUpsert,
    PocketRefreshRunResponse,
    PocketValidateRequest,
    PocketValidateResponse,
    PocketViolationItem,
)
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


async def _validate_via_router(
    model_id: UUID,
    sql: str,
    bearer: str,
    timeout_s: float = 30.0,
) -> dict:
    """POST the SQL to the query-router's /validate endpoint."""
    url = f"{_settings.QUERY_ROUTER_URL}/api/v1/validate"
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
    """
    normalised: list[dict] = []
    for f in (validation.get("filters") or []):
        column_name = str(f.get("dimension_name") or "").strip()
        operator = str(f.get("operator") or "eq").strip().lower()
        if not column_name:
            continue
        normalised.append({
            "column_name": column_name,
            "operator": operator,
            "value": f.get("value"),
        })
    return normalised


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
        total = len(pockets)
        active = [p for p in pockets if p.retired_at is None]
        fresh = sum(1 for p in active if p.status == "fresh")
        stale = sum(1 for p in active if p.status == "stale")
        invalidating = sum(1 for p in active if p.status == "invalidating")
        failed = sum(1 for p in active if p.status == "failed")
        retired = sum(1 for p in pockets if p.retired_at is not None)
        # F-005-08 (Bug-2251): `time_saved_ms_total` is now the per-pocket sum of
        # (source baseline − pocket execution) recorded at route time, so this
        # total is genuine time SAVED, not time spent on pockets.
        time_saved = sum(int(p.time_saved_ms_total or 0) for p in pockets)
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
            if p.status == "fresh" and not _matched_since_refresh(p)
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
                "hit_count": int(p.hit_count or 0),
                "ttl_days": int(p.ttl_days or 0),
                "matched_since_refresh": _matched_since_refresh(p),
            }
            for p in pockets[:10]
        ]
        return PocketMetricsResponse(
            total_pockets=total,
            fresh_pockets=fresh,
            stale_pockets=stale,
            invalidating_pockets=invalidating,
            failed_pockets=failed,
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

        target = await db.get(DataTarget, body.target_id)
        if target is None or target.model_id != model_id:
            raise HTTPException(status_code=400, detail="Invalid target_id for model")

        # Bug-5200: pocket refresh (CTAS materialisation) is currently
        # implemented for postgresql and redshift only (see
        # shared/pocket/refresh.py). Reject unsupported targets at
        # creation rather than letting the pocket persist and fail
        # silently on every refresh attempt.
        _POCKET_REFRESH_CONNECTORS = frozenset({"postgresql", "redshift"})
        target_conn_id = getattr(target, "project_connection_id", None)
        target_conn = await db.get(ProjectConnection, target_conn_id) if target_conn_id else None
        if target_conn is not None:
            target_connector = normalize_connection_type(
                (target_conn.connection_type or "").lower()
            )
            if target_connector not in _POCKET_REFRESH_CONNECTORS:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Pocket tables require a PostgreSQL or Redshift target "
                        f"for materialisation. The selected target uses "
                        f"{target_conn.connection_type!r}, which is not supported."
                    ),
                )

        allowed = await get_setting("pocket.allowed_refresh_policies", tenant_session=db)
        if body.refresh_policy not in set(allowed or []):
            raise HTTPException(
                status_code=400,
                detail=f"refresh_policy must be one of {allowed}",
            )

        ttl_days = int(body.ttl_days)
        if ttl_days <= 0:
            ttl_days = int(await get_setting("pocket.ttl_days", tenant_session=db))

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

        # Bug-5199: when the create payload requests a scheduled refresh,
        # also create the PocketRefreshPolicy child row so the scheduler's
        # refresh_due_pockets query (which filters on enabled
        # PocketRefreshPolicy rows with a cron) actually picks it up.
        if body.refresh_policy == "scheduled" and body.refresh_cron:
            db.add(PocketRefreshPolicy(
                pocket_definition_id=pocket.id,
                cron_expression=body.refresh_cron,
                is_enabled=True,
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

        updates = body.model_dump(exclude_unset=True)
        rebuild_predicate_rows = False
        rebuilt_predicates: list[dict] = []

        if "defining_sql" in updates and updates["defining_sql"]:
            try:
                validation = await _validate_via_router(
                    model_id, updates["defining_sql"], current_user.raw_token
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc))

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
            # Bug-1093 (F-005): the predicate child rows describe the cached
            # slice the matcher routes to. A defining_sql edit changes which
            # rows the next refresh caches, so the predicate rows MUST be
            # rebuilt from the new validated SQL — otherwise they keep
            # describing the OLD rows and the matcher over-claims coverage.
            rebuilt_predicates = _predicates_from_validation(validation)
            rebuild_predicate_rows = True
            updates["predicate_set_hash"] = predicate_set_hash(rebuilt_predicates)
            updates["status"] = "stale"
            updates["failure_reason"] = None

        for k, v in updates.items():
            setattr(pocket, k, v)

        if rebuild_predicate_rows:
            # Delete + reinsert: the new predicate set authoritatively replaces
            # the stale rows so the two never disagree with the cached slice.
            await db.execute(
                delete(PocketPredicate).where(
                    PocketPredicate.pocket_definition_id == pocket_id
                )
            )
            for pred in rebuilt_predicates:
                db.add(PocketPredicate(
                    pocket_definition_id=pocket_id,
                    column_name=pred["column_name"],
                    operator=pred["operator"],
                    value_json={"value": pred["value"]},
                ))

        # F-005-02 (residual): a defining_sql edit can recompute an identity
        # (query_fingerprint + predicate_set_hash) that collides with another
        # pocket on this model's partial unique index. Create catches this and
        # returns 409; PATCH historically did not, surfacing a raw 500. Catch
        # it symmetrically.
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A pocket with the same query shape and predicate set already exists for this model.",
            )
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

        # Best-effort physical cleanup.
        try:
            await drop_pocket_storage(pocket, db)
        except Exception:
            pass

        await db.delete(pocket)
        await db.commit()
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
    """POST the SQL to the query-router's /execute endpoint."""
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
        return resp.json()


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

        try:
            validation = await _validate_via_router(
                model_id, sql, current_user.raw_token
            )
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
        await _get_scoped_model(db, project_id, model_id)

        sql = (body.defining_sql or "").strip().rstrip(";").strip()
        if not sql:
            return PocketDryRunResponse(ok=False, error="SQL is empty")

        count_sql = f"SELECT COUNT(*) AS __c FROM ({sql}) AS __t"

        timeout_s = float(
            await get_setting("gateway.router_client_timeout_xlong", tenant_session=db) or 300
        )

        started = time.monotonic()
        try:
            result = await _route_query(model_id, count_sql, current_user.raw_token, timeout_s)
        except Exception as exc:
            return PocketDryRunResponse(ok=False, error=str(exc))

        elapsed_ms = int((time.monotonic() - started) * 1000)
        rows = result.get("rows") or []
        row_count = int(rows[0].get("__c", 0)) if rows else 0
        return PocketDryRunResponse(ok=True, row_count=row_count, elapsed_ms=elapsed_ms)
