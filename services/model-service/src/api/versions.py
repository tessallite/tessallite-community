"""Per-model versioning + deploy endpoints.

  GET    /projects/{p}/models/{m}/versions                  — list
  POST   /projects/{p}/models/{m}/versions                  — Save (create snapshot)
  GET    /projects/{p}/models/{m}/versions/{v}              — fetch JSON
  POST   /projects/{p}/models/{m}/versions/{v}/revert       — hard revert
  POST   /projects/{p}/models/{m}/deploy                    — deploy version
  POST   /projects/{p}/models/{m}/undeploy                  — clear deploy pointer

Auth (F-013-04): role is enforced via ``require_role`` on every route, not
just binding existence. Read routes (list / get / diff) require ``viewer``;
Save / deploy / undeploy require ``modeler``; revert — the most destructive
operation in the bundle (it deletes newer versions and rewrites live state) —
requires ``admin``. ``require_role`` also carries the bootstrap-admin rule
(first user on a binding-less project is treated as admin), so the prior
behaviour for un-bound projects is preserved. The per-route ``_ensure_model_access``
call still resolves the model and raises 404 when it is absent from the project.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.audit.logger import audit
from shared.webhooks.dispatcher import emit_webhook
from shared.config.settings import get_settings
from shared.aggregate_table_ops import drop_aggregate_physical_table
from shared.db.models import (
    AggregateDefinition,
    Model,
    ModelVersion,
    ProjectAgentModel,
    UserAccessBinding,
)
from shared.db.session import get_tenant_db
from shared.model_snapshot import (
    SnapshotSchemaError,
    SnapshotVersionError,
    rehydrate_into_live,
    snapshot_model,
)
from shared.model_snapshot.differ import diff_snapshots
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

logger = logging.getLogger(__name__)
_settings = get_settings()

# F-013-13: hold strong references to the post-response fire-and-forget
# refresh-derived tasks. ``asyncio.create_task`` only keeps a weak reference;
# without retaining the task it can be garbage-collected before it runs, and
# the hook silently never fires. We add the task here and discard it on
# completion so the set does not grow unbounded.
_background_tasks: set[asyncio.Task] = set()
router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}",
    tags=["versions"],
)


async def _notify_agent_service_refresh_derived(
    tenant_id: str,
    project_ids: list[UUID],
    bearer: str,
) -> None:
    """Fire-and-forget POST to agent-service /refresh-derived for each project.

    Failures are logged and swallowed — a publish must never fail because an
    optional auto-derived refresh hook could not reach the agent service.
    """
    if not project_ids or not bearer:
        return
    import httpx
    headers = {
        "Authorization": f"Bearer {bearer}",
        "X-Tenant-Id": tenant_id,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            for pid in project_ids:
                try:
                    await client.post(
                        f"{_settings.AGENT_SERVICE_URL}/api/v1/projects/{pid}/agent/refresh-derived",
                        headers=headers,
                    )
                except Exception as exc:
                    logger.warning(
                        "agent-service refresh-derived failed for project=%s: %s",
                        pid, exc,
                    )
    except Exception as exc:
        logger.warning("agent-service refresh-derived hook crashed: %s", exc)


async def _evict_query_router_cache(model_id: UUID, bearer: str | None) -> None:
    """Bug-5235: call the query-router's cache-eviction endpoint after
    deploy/undeploy so the semantic-binding cache does not serve stale data.

    Best-effort, awaited with a short timeout (3 s).  We intentionally await
    rather than fire-and-forget so the cache is cleared before the next query
    can bind against stale data.  On failure the deploy/undeploy still
    succeeds, but the error is logged at WARNING so ops can investigate.
    """
    if not bearer:
        return
    import httpx
    url = f"{_settings.QUERY_ROUTER_URL}/api/v1/cache/models/{model_id}"
    headers = {"Authorization": f"Bearer {bearer}"}
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.delete(url, headers=headers)
            if resp.status_code >= 400:
                logger.warning(
                    "query-router cache eviction returned %s for model=%s",
                    resp.status_code, model_id,
                )
    except Exception as exc:
        logger.warning(
            "query-router cache eviction failed for model=%s: %s",
            model_id, exc,
        )


async def _prune_old_versions(
    tenant_db: AsyncSession,
    model_id: UUID,
    deployed_version_id: Optional[UUID],
) -> None:
    """F-013-17: enforce the ``versions.retention_count`` policy.

    Keeps the newest N saved versions per model plus the currently-deployed
    version (which must survive even if it is older than the cut-off, so a
    rollback target is never destroyed). When the setting is 0 (the default)
    this is a no-op, so existing tenants accumulate versions exactly as
    before until an admin opts in. Commits its own deletion so a prune
    failure cannot roll back the just-saved version. Never raises — a
    retention sweep must not fail the Save.
    """
    try:
        from shared.config.resolver import get_setting

        keep = await get_setting(
            "versions.retention_count",
            tenant_session=tenant_db,
            model_id=model_id,
        )
        keep = int(keep or 0)
        if keep <= 0:
            return  # 0 = keep all (default)

        # Newest `keep` version ids are retained; everything older is a
        # candidate for deletion, except the deployed version.
        rows = await tenant_db.execute(
            select(ModelVersion.id)
            .where(ModelVersion.model_id == model_id)
            .order_by(ModelVersion.version_number.desc())
        )
        all_ids = [r[0] for r in rows.all()]
        retained = set(all_ids[:keep])
        if deployed_version_id is not None:
            retained.add(deployed_version_id)
        to_delete = [vid for vid in all_ids[keep:] if vid not in retained]
        if not to_delete:
            return

        await tenant_db.execute(
            delete(ModelVersion).where(ModelVersion.id.in_(to_delete))
        )
        await tenant_db.commit()
        logger.info(
            "Pruned %d old version(s) for model %s (retention_count=%d)",
            len(to_delete), model_id, keep,
        )
    except Exception:
        logger.exception(
            "Version retention prune failed for model %s (non-fatal)", model_id
        )
        try:
            await tenant_db.rollback()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class VersionItem(BaseModel):
    id: UUID
    version_number: int
    summary: Optional[str]
    created_at: datetime
    created_by: str
    is_deployed: bool


class VersionsListResponse(BaseModel):
    items: list[VersionItem]


class CreateVersionBody(BaseModel):
    summary: Optional[str] = None


class VersionDetailResponse(BaseModel):
    id: UUID
    version_number: int
    summary: Optional[str]
    created_at: datetime
    created_by: str
    is_deployed: bool
    snapshot: dict[str, Any]


class RevertBody(BaseModel):
    confirm: str  # must equal "revert to v{N}"


class DeployBody(BaseModel):
    version_id: Optional[UUID] = None  # omit -> deploy latest saved version


# ---------------------------------------------------------------------------
# Authorization (matches the bootstrap-admin rule used elsewhere)
# ---------------------------------------------------------------------------

async def _ensure_model_access(
    project_id: UUID, model_id: UUID, current_user: CurrentUser, tenant_db: AsyncSession
) -> Model:
    model = await tenant_db.get(Model, model_id)
    if model is None or model.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model {model_id} not found in project {project_id}",
        )
    if current_user.role in ("system_admin", "tenant_admin"):
        return model
    user_identity = current_user.email or current_user.user_id
    any_binding = (
        await tenant_db.execute(
            select(UserAccessBinding).where(
                UserAccessBinding.project_id == project_id
            ).limit(1)
        )
    ).scalar_one_or_none()
    if any_binding is None:
        return model  # bootstrap-admin
    rows = await tenant_db.execute(
        select(UserAccessBinding).where(
            UserAccessBinding.user_identity == user_identity,
            (UserAccessBinding.project_id == project_id)
            | (UserAccessBinding.model_id == model_id),
        )
    )
    if rows.scalars().first() is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No access binding for this model",
        )
    return model


def _to_item(v: ModelVersion, deployed_id: Optional[UUID]) -> VersionItem:
    return VersionItem(
        id=v.id,
        version_number=v.version_number,
        summary=v.summary,
        created_at=v.created_at,
        created_by=v.created_by,
        is_deployed=(deployed_id == v.id),
    )


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

@router.get(
    "/versions",
    response_model=VersionsListResponse,
    dependencies=[require_role("viewer")],
)
async def list_versions(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> VersionsListResponse:
    items: list[VersionItem] = []
    async for tenant_db in get_tenant_db(current_user.tenant_id):
        model = await _ensure_model_access(project_id, model_id, current_user, tenant_db)
        rows = await tenant_db.execute(
            select(ModelVersion)
            .where(ModelVersion.model_id == model_id)
            .order_by(ModelVersion.version_number.desc())
        )
        items = [_to_item(v, model.deployed_version_id) for v in rows.scalars().all()]
    return VersionsListResponse(items=items)


@router.post(
    "/versions",
    response_model=VersionItem,
    dependencies=[require_role("modeler")],
)
async def create_version(
    project_id: UUID,
    model_id: UUID,
    body: CreateVersionBody = Body(default_factory=CreateVersionBody),
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> VersionItem:
    """Save: take a snapshot of the live state into a new version row.

    F-013-10: ``version_number`` is computed as ``max+1`` then inserted, a
    check-then-act gap. The ``UNIQUE(model_id, version_number)`` constraint
    means two concurrent Saves race; the loser used to surface as an
    unhandled 500. We now catch the unique violation and retry once with a
    freshly-recomputed number (the doc claimed this retry existed; now it
    does).

    F-013-08: a Save emits a ``model.save`` (info) audit event so version
    history is traceable in the audit trail, matching the deploy/undeploy
    events.
    """
    out: Optional[VersionItem] = None
    async for tenant_db in get_tenant_db(current_user.tenant_id):
        model = await _ensure_model_access(project_id, model_id, current_user, tenant_db)
        actor = current_user.email or current_user.user_id
        # Snapshot the live state once; it does not change between retries.
        snap = await snapshot_model(model_id, tenant_db)

        last_error: Optional[IntegrityError] = None
        for _attempt in range(2):
            last_q = await tenant_db.execute(
                select(ModelVersion.version_number)
                .where(ModelVersion.model_id == model_id)
                .order_by(ModelVersion.version_number.desc())
                .limit(1)
            )
            next_n = (last_q.scalar_one_or_none() or 0) + 1
            version = ModelVersion(
                model_id=model_id,
                version_number=next_n,
                snapshot_json=snap,
                summary=body.summary,
                created_by=actor,
            )
            tenant_db.add(version)
            try:
                await tenant_db.flush()
            except IntegrityError as exc:
                # Concurrent Save took this version number first. Roll back
                # the failed INSERT and recompute max+1 on the next pass.
                last_error = exc
                await tenant_db.rollback()
                continue
            await audit(
                tenant_db, action="model.save", severity="info",
                actor_email=current_user.email,
                target_type="model", target_id=model_id,
                target_name=model.display_name,
                detail={"version_number": next_n},
            )
            await tenant_db.commit()
            await tenant_db.refresh(version)
            out = _to_item(version, model.deployed_version_id)
            # F-013-17: bound unbounded version growth. Default keep-all (0)
            # leaves behaviour unchanged; a configured count prunes the oldest
            # versions beyond the newest N, never touching the deployed one.
            await _prune_old_versions(
                tenant_db, model_id, model.deployed_version_id,
            )
            break
        else:
            # Both attempts collided — surface a clear 409 rather than a 500.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Concurrent Save collided on a version number; retry.",
            ) from last_error
    assert out is not None
    return out


@router.get(
    "/versions/{version_id}",
    response_model=VersionDetailResponse,
    dependencies=[require_role("viewer")],
)
async def get_version(
    project_id: UUID,
    model_id: UUID,
    version_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> VersionDetailResponse:
    out: Optional[VersionDetailResponse] = None
    async for tenant_db in get_tenant_db(current_user.tenant_id):
        model = await _ensure_model_access(project_id, model_id, current_user, tenant_db)
        v = await tenant_db.get(ModelVersion, version_id)
        if v is None or v.model_id != model_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Version not found",
            )
        out = VersionDetailResponse(
            id=v.id,
            version_number=v.version_number,
            summary=v.summary,
            created_at=v.created_at,
            created_by=v.created_by,
            is_deployed=(model.deployed_version_id == v.id),
            snapshot=v.snapshot_json,
        )
    assert out is not None
    return out


@router.get(
    "/versions/{version_a_id}/diff/{version_b_id}",
    dependencies=[require_role("viewer")],
)
async def diff_versions(
    project_id: UUID,
    model_id: UUID,
    version_a_id: UUID,
    version_b_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> dict:
    """Return a structured diff between version A (old) and version B (new)."""
    out: Optional[dict] = None
    async for tenant_db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_access(project_id, model_id, current_user, tenant_db)
        va = await tenant_db.get(ModelVersion, version_a_id)
        vb = await tenant_db.get(ModelVersion, version_b_id)
        if va is None or va.model_id != model_id:
            raise HTTPException(status_code=404, detail="Version A not found")
        if vb is None or vb.model_id != model_id:
            raise HTTPException(status_code=404, detail="Version B not found")
        out = {
            "version_a": va.version_number,
            "version_b": vb.version_number,
            "diff": diff_snapshots(va.snapshot_json or {}, vb.snapshot_json or {}),
        }
    assert out is not None
    return out


@router.post(
    "/versions/{version_id}/revert",
    dependencies=[require_role("admin")],
)
async def revert_to_version(
    project_id: UUID,
    model_id: UUID,
    version_id: UUID,
    body: RevertBody = Body(...),
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> dict:
    """Revert: delete every newer version, rehydrate the chosen one, retire orphan aggregates."""
    async for tenant_db in get_tenant_db(current_user.tenant_id):
        model = await _ensure_model_access(project_id, model_id, current_user, tenant_db)
        v = await tenant_db.get(ModelVersion, version_id)
        if v is None or v.model_id != model_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Version not found",
            )
        expected = f"revert to v{v.version_number}"
        if body.confirm.strip().lower() != expected.lower():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"confirm must equal {expected!r}",
            )
        # Rehydrate first (inside the same transaction).
        # Preserve aggregates, pockets, and named sets — they will be
        # marked invalid/stale if their dependencies are missing, but
        # not force-retired. This allows them to remain visible and
        # retire naturally via the scheduler when no longer used.
        try:
            await rehydrate_into_live(
                model_id, v.snapshot_json, tenant_db,
                # Mark aggregates absent from the reverted-to snapshot as
                # retired (the scheduler sweep drops their physical table);
                # aggregates present in the snapshot are preserved. F-013-02.
                drop_orphan_aggregates=True,
                preserve_aggregates=True,
                preserve_pockets=True,
                preserve_named_sets=True,
                actor=current_user.email or current_user.user_id,
            )
        except (SnapshotSchemaError, SnapshotVersionError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            )
        # Delete every newer version
        await tenant_db.execute(
            delete(ModelVersion).where(
                ModelVersion.model_id == model_id,
                ModelVersion.version_number > v.version_number,
            )
        )
        # Revert is a hard rewrite of live state, so the deploy pointer must
        # follow. Without this, a model deployed to v2 that gets reverted to
        # v3 would still report v2 as deployed while query-router serves v3's
        # live shape — the exact divergence F-8 forbids.
        was_deployed = model.deployed_version_id is not None
        if was_deployed:
            model.deployed_version_id = v.id
            model.last_deployed_at = datetime.now(timezone.utc)
        # F-013-08: revert is the most destructive operation in the bundle
        # (it deletes every newer version and rewrites live state), so it
        # gets a critical audit event — heavier than model.delete's severity.
        await audit(
            tenant_db, action="model.revert", severity="critical",
            actor_email=current_user.email,
            target_type="model", target_id=model_id,
            target_name=model.display_name,
            detail={
                "reverted_to_version": v.version_number,
                "was_deployed": was_deployed,
            },
        )
        await tenant_db.commit()

        # Bug-5346: a revert marks aggregates absent from the reverted-to
        # snapshot as `retired` (rehydrator, drop_orphan_aggregates) but cannot
        # drop their tables inside the metadata transaction (no target access —
        # F-013-02). Now that the revert has committed, reclaim those tables so
        # "retired means the table is actually dropped" holds for revert too.
        # Best-effort: a failed drop leaves the table for the retirement sweep.
        retired_orphans = (
            await tenant_db.execute(
                select(AggregateDefinition).where(
                    AggregateDefinition.model_id == model_id,
                    AggregateDefinition.status == "retired",
                    AggregateDefinition.physical_table_purged_at.is_(None),
                )
            )
        ).scalars().all()
        if retired_orphans:
            for agg in retired_orphans:
                await drop_aggregate_physical_table(agg, tenant_db, reason="revert_orphan")
            await tenant_db.commit()

        # F-013-08: emit a webhook so downstream consumers (audit pipelines,
        # ops alerting) learn that production state was rewritten.
        await emit_webhook(current_user.tenant_id, "model.reverted", {
            "model_id": str(model_id),
            "model_name": model.display_name,
            "reverted_to_version": v.version_number,
            "actor": current_user.email,
        })
    return {
        "status": "ok",
        "reverted_to": str(version_id),
    }


# ---------------------------------------------------------------------------
# Deploy / Undeploy (Phase 5)
# ---------------------------------------------------------------------------

@router.post("/deploy", dependencies=[require_role("modeler")])
async def deploy_model(
    project_id: UUID,
    model_id: UUID,
    body: DeployBody = Body(default_factory=DeployBody),
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> dict:
    async for tenant_db in get_tenant_db(current_user.tenant_id):
        model = await _ensure_model_access(project_id, model_id, current_user, tenant_db)
        version_id = body.version_id
        if version_id is None:
            # Deploy latest saved version
            latest_q = await tenant_db.execute(
                select(ModelVersion)
                .where(ModelVersion.model_id == model_id)
                .order_by(ModelVersion.version_number.desc())
                .limit(1)
            )
            latest = latest_q.scalar_one_or_none()
            if latest is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Model has no saved versions; click Save first.",
                )
            version_id = latest.id
        else:
            v = await tenant_db.get(ModelVersion, version_id)
            if v is None or v.model_id != model_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Version not found for this model",
                )
        model.deployed_version_id = version_id
        model.last_deployed_at = datetime.now(timezone.utc)
        await audit(
            tenant_db, action="model.deploy", severity="warn",
            actor_email=current_user.email,
            target_type="model", target_id=model_id,
            target_name=model.display_name,
            detail={"version_id": str(version_id)},
        )
        await tenant_db.commit()

        # Invalidate KPI evaluation cache for this model
        from src.kpi_cache import get_kpi_cache
        get_kpi_cache().invalidate_model(model_id)

        await emit_webhook(current_user.tenant_id, "model.published", {
            "model_id": str(model_id),
            "model_name": model.display_name,
            "version_id": str(version_id),
            "actor": current_user.email,
        })

        # B4.5 — notify agent-service to refresh auto-derived per-model
        # context (aggregates_summary, calendar_aliases, dimension_aliases)
        # for every project whose agent has this model on its allow-list.
        # Fire-and-forget; never fail the publish on this call.
        agent_project_ids = list(
            (
                await tenant_db.execute(
                    select(ProjectAgentModel.project_id)
                    .where(ProjectAgentModel.model_id == model_id)
                    .distinct()
                )
            ).scalars().all()
        )
        if agent_project_ids and current_user.raw_token:
            task = asyncio.create_task(
                _notify_agent_service_refresh_derived(
                    tenant_id=current_user.tenant_id,
                    project_ids=agent_project_ids,
                    bearer=current_user.raw_token,
                )
            )
            # F-013-13: retain a strong reference until the task finishes so
            # the event loop does not GC it mid-flight.
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

        # Bug-5235: evict query-router semantic-binding cache
        await _evict_query_router_cache(model_id, current_user.raw_token)

        return {
            "status": "ok",
            "deployed_version_id": str(version_id),
            "last_deployed_at": model.last_deployed_at.isoformat(),
        }
    # F-013-15: get_tenant_db always yields exactly one session, so the loop
    # body always returns. Raise rather than return a sentinel that callers
    # would otherwise have to interpret.
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="tenant database session unavailable",
    )


@router.post("/undeploy", dependencies=[require_role("modeler")])
async def undeploy_model(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> dict:
    async for tenant_db in get_tenant_db(current_user.tenant_id):
        model = await _ensure_model_access(project_id, model_id, current_user, tenant_db)
        model.deployed_version_id = None
        await audit(
            tenant_db, action="model.undeploy", severity="warn",
            actor_email=current_user.email,
            target_type="model", target_id=model_id,
            target_name=model.display_name,
        )
        await tenant_db.commit()

        # Invalidate KPI evaluation cache for this model
        from src.kpi_cache import get_kpi_cache
        get_kpi_cache().invalidate_model(model_id)

        await emit_webhook(current_user.tenant_id, "model.undeployed", {
            "model_id": str(model_id),
            "model_name": model.display_name,
            "actor": current_user.email,
        })

        # Bug-5414: notify agent-service to refresh auto-derived context,
        # mirroring deploy_model's pattern. Without this, the agent's
        # aggregates_summary / dimension_aliases stay stale after undeploy.
        agent_project_ids = list(
            (
                await tenant_db.execute(
                    select(ProjectAgentModel.project_id)
                    .where(ProjectAgentModel.model_id == model_id)
                    .distinct()
                )
            ).scalars().all()
        )
        if agent_project_ids and current_user.raw_token:
            task = asyncio.create_task(
                _notify_agent_service_refresh_derived(
                    tenant_id=current_user.tenant_id,
                    project_ids=agent_project_ids,
                    bearer=current_user.raw_token,
                )
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

        # Bug-5235: evict query-router semantic-binding cache
        await _evict_query_router_cache(model_id, current_user.raw_token)

    return {"status": "ok"}
