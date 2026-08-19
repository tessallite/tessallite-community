"""Named Query authoring API (model-service).

Mirrors ``src/api/pockets.py`` structure and RBAC: a Named Query is governed
modeler-authored model content (list/get viewer; create/patch/refresh/policy
modeler+; delete admin). The definition is MODEL-BOUND semantic SQL over the
model's logical surface (never raw dialect SQL); it is re-validated against the
CURRENT deployed model at create/update (via the query-router /validate
endpoint) and at every refresh (drift check).

The materialisation runs on the shared artifact substrate
(``shared/named_query/refresh.py``) — full CTAS in v1; incremental is deferred.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import func as sa_func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from shared.config.resolver import get_setting
from shared.config.settings import get_settings
from shared.db.models import (
    Dimension,
    Measure,
    Model,
    ModelColumn,
    ModelParameter,
    NamedQuery,
    NamedQueryArtifact,
    NamedQueryRefreshPolicy,
    NamedQueryRefreshRun,
    NamedSet,
)
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    NamedQueryCreate,
    NamedQueryResponse,
    NamedQueryUpdate,
    NamedQueryValidateRequest,
    NamedQueryValidateResponse,
    NamedQueryRefreshPolicyResponse,
    NamedQueryRefreshPolicyUpsert,
    NamedQueryRefreshRunResponse,
)
from src.api._model_lock import acquire_model_definition_lock
from src.api._validator_unavailable import validator_unavailable
from src.api.versions import _evict_query_router_cache
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role
from src.named_query_validation import (
    NamedQueryValidationError,
    _TYPE_MAP,
    check_column_cap,
    derive_definition_metadata,
)

logger = logging.getLogger(__name__)
_settings = get_settings()

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/named-queries",
    tags=["named-queries"],
)


class RouterUnavailableError(RuntimeError):
    """The query-router validator could not answer (network / 5xx)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _run_named_query_refresh_in_background(
    tenant_id: str,
    named_query_id: UUID,
    run_id: UUID,
    bearer_token: str | None,
) -> None:
    """Execute a queued Named Query refresh off the request path.

    Mirrors ``pockets._run_pocket_refresh_in_background``: opens its own
    tenant-scoped session and adopts the pre-created ``queued`` run row.
    Never raises out; a crash leaves the run failed, never stuck in queued.
    """
    from shared.named_query.refresh import refresh_named_query_artifact

    try:
        async for db in get_tenant_db(tenant_id):
            try:
                await refresh_named_query_artifact(
                    named_query_id,
                    db,
                    triggered_by="api",
                    bearer_token=bearer_token,
                    tenant_id=tenant_id,
                    existing_run_id=run_id,
                )
            except Exception as exc:
                logger.exception(
                    "Async named-query refresh crashed for named_query=%s "
                    "run=%s: %s",
                    named_query_id, run_id, exc,
                )
                try:
                    run = await db.get(NamedQueryRefreshRun, run_id)
                    if run is not None and run.status in ("queued", "running"):
                        run.status = "failed"
                        run.error_message = str(exc)[:1000]
                        run.completed_at = datetime.now(timezone.utc)
                        nq = await db.get(NamedQuery, named_query_id)
                        if nq is not None:
                            artifact = await _get_artifact(db, named_query_id)
                            if artifact is not None:
                                artifact.status = "failed"
                                artifact.failure_reason = str(exc)[:1000]
                        await db.commit()
                except Exception:
                    logger.exception(
                        "Failed to mark named-query run %s failed", run_id
                    )
    except Exception:
        logger.exception(
            "Async named-query refresh session setup failed for run=%s", run_id
        )


async def _get_scoped_model(db, project_id: UUID, model_id: UUID) -> Model:
    model = await db.get(Model, model_id)
    if model is None or model.project_id != project_id:
        raise HTTPException(status_code=404, detail="Model not found")
    return model


async def _get_artifact(
    db, named_query_id: UUID
) -> Optional[NamedQueryArtifact]:
    result = await db.execute(
        select(NamedQueryArtifact).where(
            NamedQueryArtifact.named_query_id == named_query_id
        )
    )
    return result.scalar_one_or_none()


async def _validate_via_router(
    model_id: UUID,
    sql: str,
    bearer: str,
    timeout_s: float = 30.0,
) -> dict:
    """POST the SQL to the query-router's /validate endpoint.

    Same verdict split as pockets: a 4xx is a verdict on the SQL (ValueError);
    a network error or 5xx is a RouterUnavailableError -> 503, never 400.
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


def _router_unavailable_http(exc: Exception) -> HTTPException:
    return validator_unavailable("named query", exc)


async def _model_field_maps(db, model_id: UUID) -> tuple[dict[str, str], set[str]]:
    """Return (dimension_types, measure_names) keyed case-insensitively.

    Dimension types come from each dimension's source column (or the
    user-defined-attribute column) data_type; measures are numeric.
    """
    dims = (
        await db.execute(
            select(Dimension).where(Dimension.model_id == model_id)
        )
    ).scalars().all()
    col_ids = [d.source_column_id for d in dims if d.source_column_id]
    col_types: dict[UUID, str] = {}
    if col_ids:
        cols = (
            await db.execute(
                select(ModelColumn).where(ModelColumn.id.in_(col_ids))
            )
        ).scalars().all()
        col_types = {c.id: (c.data_type or "string") for c in cols}

    dimension_types: dict[str, str] = {}
    for d in dims:
        raw = col_types.get(d.source_column_id, "")
        if d.is_time_dim or (raw or "").lower() in (
            "date", "timestamp", "timestamptz", "datetime", "time",
        ):
            nq_type = "timestamp" if (raw or "").lower() != "date" else "date"
        else:
            nq_type = _TYPE_MAP.get((raw or "").lower(), "string")
        dimension_types[d.name.lower()] = nq_type
    measures = (
        await db.execute(
            select(Measure).where(Measure.model_id == model_id)
        )
    ).scalars().all()
    measure_names = {m.name.lower() for m in measures}
    return dimension_types, measure_names


async def _effective_column_cap(
    db, model_id: UUID, per_nq_cap: Optional[int]
) -> int:
    if per_nq_cap is not None:
        return int(per_nq_cap)
    return int(
        await get_setting(
            "named_query.max_columns", tenant_session=db, model_id=model_id
        )
    )


def _canonical_ns_identity(name: str) -> str:
    stripped = name.lstrip("@") if name.startswith("@") else name
    return stripped.lower()


async def _check_at_namespace_collision(
    db,
    model_id: UUID,
    name: str,
    *,
    exclude_named_query_id: Optional[UUID] = None,
) -> None:
    """Reject if the canonical name collides across the shared @ namespace.

    Named Query names share the ``@`` namespace with model parameters and
    named sets (case-insensitive, Bug-7663 precedent). A collision at query
    time would be ambiguous — a hard 400/409, never a silent winner.
    """
    canonical = _canonical_ns_identity(name)
    param_row = (
        await db.execute(
            select(ModelParameter.id, ModelParameter.name).where(
                ModelParameter.model_id == model_id,
                sa_func.lower(sa_func.ltrim(ModelParameter.name, "@"))
                == canonical,
            ).limit(1)
        )
    ).first()
    if param_row is not None:
        _, param_name = param_row
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A model parameter called '{param_name}' already exists in "
                f"this model. Named Queries, named lists and parameters share "
                f"the @ namespace and must have unique names "
                f"(case-insensitive)."
            ),
        )
    ns_row = (
        await db.execute(
            select(NamedSet.id, NamedSet.name).where(
                NamedSet.model_id == model_id,
                sa_func.lower(NamedSet.name) == canonical,
            ).limit(1)
        )
    ).first()
    if ns_row is not None:
        _, ns_name = ns_row
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A named set called '{ns_name}' already exists in this "
                f"model. Named Queries, named lists and parameters share the "
                f"@ namespace and must have unique names (case-insensitive)."
            ),
        )
    nq_stmt = select(NamedQuery.id, NamedQuery.name).where(
        NamedQuery.model_id == model_id,
        sa_func.lower(NamedQuery.name) == canonical,
    )
    if exclude_named_query_id is not None:
        nq_stmt = nq_stmt.where(NamedQuery.id != exclude_named_query_id)
    nq_row = (await db.execute(nq_stmt.limit(1))).first()
    if nq_row is not None:
        _, nq_name = nq_row
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A Named Query called '{nq_name}' already exists in this "
                f"model (names are case-insensitive)."
            ),
        )


def _to_response(nq: NamedQuery) -> NamedQueryResponse:
    """Build the response including nested artifact + policy, loaded eagerly
    by the callers via selectinload."""
    return NamedQueryResponse(
        id=nq.id,
        model_id=nq.model_id,
        name=nq.name,
        display_name=nq.display_name,
        description=nq.description,
        display_folder=nq.display_folder,
        definition_sql=nq.definition_sql,
        output_columns=nq.output_columns,
        shape=nq.shape,
        row_cap=nq.row_cap,
        column_cap=nq.column_cap,
        certification_status=nq.certification_status,
        created_by=nq.created_by,
        artifact=(
            {
                "id": nq.artifact.id,
                "target_id": nq.artifact.target_id,
                "physical_table_name": nq.artifact.physical_table_name,
                "target_schema": nq.artifact.target_schema,
                "row_count": nq.artifact.row_count,
                "status": nq.artifact.status,
                "failure_reason": nq.artifact.failure_reason,
                "last_refresh_at": nq.artifact.last_refresh_at,
                "retired_at": nq.artifact.retired_at,
            }
            if nq.artifact is not None
            else None
        ),
        refresh_policy=(
            {
                "cron_expression": nq.refresh_policy_row.cron_expression,
                "is_enabled": nq.refresh_policy_row.is_enabled,
            }
            if nq.refresh_policy_row is not None
            else None
        ),
        created_at=nq.created_at,
        updated_at=nq.updated_at,
    )


def _load_options():
    return (
        selectinload(NamedQuery.artifact),
        selectinload(NamedQuery.refresh_policy_row),
    )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.get("", response_model=list[NamedQueryResponse])
async def list_named_queries(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> list[NamedQueryResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_model(db, project_id, model_id)
        result = await db.execute(
            select(NamedQuery)
            .where(NamedQuery.model_id == model_id)
            .options(*_load_options())
            .order_by(NamedQuery.created_at, NamedQuery.id)
        )
        return [_to_response(nq) for nq in result.scalars().all()]


@router.get("/{named_query_id}", response_model=NamedQueryResponse)
async def get_named_query(
    project_id: UUID,
    model_id: UUID,
    named_query_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> NamedQueryResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_model(db, project_id, model_id)
        result = await db.execute(
            select(NamedQuery)
            .where(NamedQuery.id == named_query_id, NamedQuery.model_id == model_id)
            .options(*_load_options())
        )
        nq = result.scalar_one_or_none()
        if nq is None:
            raise HTTPException(status_code=404, detail="Named Query not found")
        return _to_response(nq)


@router.post(
    "/validate",
    response_model=NamedQueryValidateResponse,
    dependencies=[require_role("modeler")],
)
async def validate_named_query_sql(
    project_id: UUID,
    model_id: UUID,
    body: NamedQueryValidateRequest,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> NamedQueryValidateResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_model(db, project_id, model_id)
        return await _run_definition_validation(
            db, model_id, body.definition_sql, current_user.raw_token
        )


async def _run_definition_validation(
    db,
    model_id: UUID,
    definition_sql: str,
    bearer: str,
    *,
    column_cap: Optional[int] = None,
) -> NamedQueryValidateResponse:
    """Shared create/update/validate core: router bind + metadata derive."""
    try:
        validation = await _validate_via_router(model_id, definition_sql, bearer)
    except RouterUnavailableError as exc:
        raise _router_unavailable_http(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not validation.get("ok"):
        return NamedQueryValidateResponse(
            is_valid=False, errors=validation.get("errors", [])
        )
    dimension_types, measure_names = await _model_field_maps(db, model_id)
    try:
        derived = derive_definition_metadata(
            definition_sql,
            dimension_types=dimension_types,
            measure_names=measure_names,
        )
    except NamedQueryValidationError as exc:
        return NamedQueryValidateResponse(is_valid=False, errors=[str(exc)])
    if column_cap is not None:
        try:
            check_column_cap(
                derived["output_columns"], effective_column_cap=column_cap
            )
        except NamedQueryValidationError as exc:
            return NamedQueryValidateResponse(is_valid=False, errors=[str(exc)])
    return NamedQueryValidateResponse(
        is_valid=True,
        output_columns=derived["output_columns"],
        shape=derived["shape"],
    )


@router.post(
    "",
    response_model=NamedQueryResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_named_query(
    project_id: UUID,
    model_id: UUID,
    body: NamedQueryCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> NamedQueryResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_model(db, project_id, model_id)
        # Bug-7982: serialise definition writes against Save/revert.
        await acquire_model_definition_lock(db, model_id)

        await _check_at_namespace_collision(db, model_id, body.name)

        effective_col_cap = await _effective_column_cap(
            db, model_id, body.column_cap
        )
        validation = await _run_definition_validation(
            db,
            model_id,
            body.definition_sql,
            current_user.raw_token,
            column_cap=effective_col_cap,
        )
        if not validation.is_valid:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "Named Query SQL failed validation.",
                    "errors": validation.errors,
                },
            )

        nq = NamedQuery(
            model_id=model_id,
            name=body.name,
            display_name=body.display_name,
            description=body.description,
            display_folder=body.display_folder,
            definition_sql=body.definition_sql,
            output_columns=[
                {"name": c.name, "type": c.type}
                for c in validation.output_columns
            ],
            shape=validation.shape or "projection",
            row_cap=body.row_cap,
            column_cap=body.column_cap,
            certification_status=body.certification_status,
            created_by=current_user.email,
        )
        db.add(nq)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "A Named Query with this name already exists for this "
                    "model (names are case-insensitive)."
                ),
            )

        if body.refresh_policy == "schedule":
            db.add(NamedQueryRefreshPolicy(
                named_query_id=nq.id,
                cron_expression=body.refresh_cron,
                is_enabled=(
                    body.refresh_policy_enabled
                    if body.refresh_policy_enabled is not None
                    else True
                ),
            ))
        await db.commit()
        await db.refresh(nq)
        result = await db.execute(
            select(NamedQuery).where(NamedQuery.id == nq.id).options(*_load_options())
        )
        return _to_response(result.scalar_one())


@router.patch(
    "/{named_query_id}",
    response_model=NamedQueryResponse,
    dependencies=[require_role("modeler")],
)
async def update_named_query(
    project_id: UUID,
    model_id: UUID,
    named_query_id: UUID,
    body: NamedQueryUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> NamedQueryResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_model(db, project_id, model_id)
        result = await db.execute(
            select(NamedQuery)
            .where(NamedQuery.id == named_query_id, NamedQuery.model_id == model_id)
            .options(*_load_options())
        )
        nq = result.scalar_one_or_none()
        if nq is None:
            raise HTTPException(status_code=404, detail="Named Query not found")
        # Bug-7982: serialise definition writes against Save/revert.
        await acquire_model_definition_lock(db, model_id)

        if body.name is not None:
            await _check_at_namespace_collision(
                db, model_id, body.name, exclude_named_query_id=nq.id
            )

        new_definition = body.definition_sql
        if new_definition is not None:
            effective_col_cap = await _effective_column_cap(
                db,
                model_id,
                body.column_cap if body.column_cap is not None else nq.column_cap,
            )
            validation = await _run_definition_validation(
                db,
                model_id,
                new_definition,
                current_user.raw_token,
                column_cap=effective_col_cap,
            )
            if not validation.is_valid:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": "Named Query SQL failed validation.",
                        "errors": validation.errors,
                    },
                )
            nq.definition_sql = new_definition
            nq.output_columns = [
                {"name": c.name, "type": c.type}
                for c in validation.output_columns
            ]
            nq.shape = validation.shape or "projection"
            # A definition edit must take effect only after a deploy (snapshot
            # semantics); the materialised artifact is stale until the next
            # refresh either way. Mark it stale NOW so an edited definition is
            # never served from a table built under the old one.
            if nq.artifact is not None and nq.artifact.status in (
                "fresh", "invalidating",
            ):
                nq.artifact.status = "stale"
                nq.artifact.failure_reason = None

        for field in (
            "display_name", "description", "display_folder",
            "certification_status",
        ):
            value = getattr(body, field)
            if value is not None:
                setattr(nq, field, value)
        if body.row_cap is not None:
            nq.row_cap = body.row_cap
        if body.column_cap is not None:
            nq.column_cap = body.column_cap

        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "A Named Query with this name already exists for this "
                    "model (names are case-insensitive)."
                ),
            )
        await db.refresh(nq)
        result = await db.execute(
            select(NamedQuery).where(NamedQuery.id == nq.id).options(*_load_options())
        )
        return _to_response(result.scalar_one())


@router.delete(
    "/{named_query_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("admin")],
)
async def delete_named_query(
    project_id: UUID,
    model_id: UUID,
    named_query_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_model(db, project_id, model_id)
        result = await db.execute(
            select(NamedQuery)
            .where(NamedQuery.id == named_query_id, NamedQuery.model_id == model_id)
            .options(*_load_options())
        )
        nq = result.scalar_one_or_none()
        if nq is None:
            raise HTTPException(status_code=404, detail="Named Query not found")
        # Bug-7982: serialise definition writes against Save/revert.
        await acquire_model_definition_lock(db, model_id)

        # Best-effort physical cleanup through the shared eviction path.
        if nq.artifact is not None:
            try:
                from shared.named_query.refresh import drop_named_query_storage
                await drop_named_query_storage(nq.artifact, db)
            except Exception:
                logger.warning(
                    "Failed to drop physical table for named query %s",
                    named_query_id, exc_info=True,
                )

        await db.delete(nq)
        await db.commit()
        await _evict_query_router_cache(model_id, current_user.tenant_id)
        return None


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

@router.post(
    "/{named_query_id}/refresh",
    response_model=NamedQueryRefreshRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[require_role("modeler")],
)
async def refresh_named_query(
    project_id: UUID,
    model_id: UUID,
    named_query_id: UUID,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> NamedQueryRefreshRunResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_model(db, project_id, model_id)
        nq = await db.get(NamedQuery, named_query_id)
        if nq is None or nq.model_id != model_id:
            raise HTTPException(status_code=404, detail="Named Query not found")
        # Bug-7982: serialise the queued run write against Save/revert (the
        # heavy materialisation runs on the background task's own session).
        await acquire_model_definition_lock(db, model_id)

        run = NamedQueryRefreshRun(
            named_query_id=named_query_id,
            refresh_mode="full",
            status="queued",
            triggered_by="api",
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        run_id = run.id

        background_tasks.add_task(
            _run_named_query_refresh_in_background,
            current_user.tenant_id,
            named_query_id,
            run_id,
            current_user.raw_token or None,
        )
        return NamedQueryRefreshRunResponse.model_validate(run)


@router.get(
    "/{named_query_id}/refresh/runs",
    response_model=list[NamedQueryRefreshRunResponse],
)
async def list_named_query_runs(
    project_id: UUID,
    model_id: UUID,
    named_query_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> list[NamedQueryRefreshRunResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_model(db, project_id, model_id)
        nq = await db.get(NamedQuery, named_query_id)
        if nq is None or nq.model_id != model_id:
            raise HTTPException(status_code=404, detail="Named Query not found")
        result = await db.execute(
            select(NamedQueryRefreshRun)
            .where(NamedQueryRefreshRun.named_query_id == named_query_id)
            .order_by(NamedQueryRefreshRun.started_at.desc())
            .limit(100)
        )
        return [
            NamedQueryRefreshRunResponse.model_validate(r)
            for r in result.scalars().all()
        ]


@router.get(
    "/{named_query_id}/refresh/policy",
    response_model=NamedQueryRefreshPolicyResponse,
)
async def get_named_query_refresh_policy(
    project_id: UUID,
    model_id: UUID,
    named_query_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> NamedQueryRefreshPolicyResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_model(db, project_id, model_id)
        nq = await db.get(NamedQuery, named_query_id)
        if nq is None or nq.model_id != model_id:
            raise HTTPException(status_code=404, detail="Named Query not found")
        result = await db.execute(
            select(NamedQueryRefreshPolicy).where(
                NamedQueryRefreshPolicy.named_query_id == named_query_id
            )
        )
        policy = result.scalar_one_or_none()
        return NamedQueryRefreshPolicyResponse(
            cron_expression=policy.cron_expression if policy else None,
            is_enabled=policy.is_enabled if policy else False,
        )


@router.put(
    "/{named_query_id}/refresh/policy",
    response_model=NamedQueryRefreshPolicyResponse,
    dependencies=[require_role("modeler")],
)
async def upsert_named_query_refresh_policy(
    project_id: UUID,
    model_id: UUID,
    named_query_id: UUID,
    body: NamedQueryRefreshPolicyUpsert,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> NamedQueryRefreshPolicyResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_model(db, project_id, model_id)
        nq = await db.get(NamedQuery, named_query_id)
        if nq is None or nq.model_id != model_id:
            raise HTTPException(status_code=404, detail="Named Query not found")
        # Bug-7982: the policy row is snapshot-owned (serialised + rehydrated).
        await acquire_model_definition_lock(db, model_id)

        result = await db.execute(
            select(NamedQueryRefreshPolicy).where(
                NamedQueryRefreshPolicy.named_query_id == named_query_id
            )
        )
        policy = result.scalar_one_or_none()
        if policy is None:
            policy = NamedQueryRefreshPolicy(named_query_id=named_query_id)
            db.add(policy)
        if body.cron_expression is not None:
            policy.cron_expression = body.cron_expression
        if body.is_enabled is not None:
            policy.is_enabled = body.is_enabled
        await db.commit()
        await db.refresh(policy)
        return NamedQueryRefreshPolicyResponse(
            cron_expression=policy.cron_expression,
            is_enabled=policy.is_enabled,
        )
