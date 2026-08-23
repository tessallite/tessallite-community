"""Per-model versioning + deploy endpoints.

  GET    /projects/{p}/models/{m}/versions                  — list
  POST   /projects/{p}/models/{m}/versions                  — Save (create snapshot)
  GET    /projects/{p}/models/{m}/versions/{v}              — fetch JSON
  POST   /projects/{p}/models/{m}/versions/{v}/revert       — hard revert
  POST   /projects/{p}/models/{m}/deploy                    — deploy version
  POST   /projects/{p}/models/{m}/undeploy                  — clear deploy pointer

Auth (F-013-04): role is enforced via ``require_role`` on every route, not
just binding existence. Read routes (list / get / diff) require ``viewer``;
Save / deploy / undeploy require ``modeler``; revert — the heaviest
operation in the bundle (it rewrites live state and moves the deploy pointer;
Bug-7906: it appends a new version and never deletes history) —
requires ``admin``. F-021-04 (decision #9): there is NO zero-binding
bootstrap-admin grant — a caller with no binding on a binding-less project is
denied. The per-route ``_ensure_model_access`` call still resolves the model and
raises 404 when it is absent from the project.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.audit.logger import audit
from shared.webhooks.dispatcher import emit_webhook_logged as emit_webhook
from shared.auth.identity import user_identity_matches
from shared.config.settings import get_settings
from shared.aggregate_table_ops import drop_aggregate_physical_table
from shared.artifact_version_gate import artifact_incompatible_sql
from shared.db.models import (
    AggregateDefinition,
    KPI,
    Model,
    ModelVersion,
    NamedQuery,
    NamedQueryArtifact,
    PendingKpiReeval,
    PocketDefinition,
    ProjectAgentModel,
    UserAccessBinding,
)
from shared.db.session import get_system_db, get_tenant_db
from shared.model_snapshot import (
    OneFactViolationError,
    RehydrationMode,
    SnapshotSchemaError,
    SnapshotVersionError,
    consistent_snapshot,
    rehydrate_into_live,
)
from shared.model_snapshot.differ import diff_snapshots
from shared.semantic.graph_order import fact_anchor_violation
from src.auth.middleware import (
    CurrentUser,
    forbid_embed_user,
    is_human_tenant_admin_or_system_admin,
)
from src.api._model_lock import acquire_model_definition_lock
from src.auth.rbac import caller_has_role, require_role
from src.cold_start_trigger import trigger_predictive_cold_start
from src.kpi_reeval_trigger import trigger_post_deploy_kpi_reeval

logger = logging.getLogger(__name__)
_settings = get_settings()


# Bug-7980 (F-013-06) / Bug-8380: _consistent_snapshot is now the shared
# ``consistent_snapshot`` in ``shared.model_snapshot.consistent_read``.
# Kept as a local alias so callsites inside this module read unchanged.
_consistent_snapshot = consistent_snapshot


# Bug-7844 [SECURITY]: the model snapshot embedded in a version row carries the
# tenant's full access-control policy — row-security predicate expressions and
# their claim/dimension/mapping-table wiring (``row_security_rules``), the
# column-level-security classification (``data_tags`` + which persona is denied
# which tag via ``persona_tag_restrictions``), and personas whose
# ``default_filters`` are data-scoping values. A plain viewer legitimately views
# and diffs model VERSIONS (the read-only Model Builder version history/diff),
# but must never read the access-control policy through them. This is the same
# disclosure class fixed for the export bundle (Bug-7300) and the direct
# row-security routes (Bug-7807). Rather than raise the whole route to modeler
# and break the legitimate viewer version view, the security tables are removed
# from a COPY of the snapshot at the response boundary for non-modeler callers.
# The stored snapshot (``ModelVersion.snapshot_json``) and the deploy/rehydrate
# path are never touched — they must keep these tables for correct restore.
_SECURITY_SENSITIVE_SNAPSHOT_KEYS: tuple[str, ...] = (
    "row_security_rules",
    "data_tags",
    "persona_tag_restrictions",
    "personas",
)


def _redact_security_tables(snapshot: Any) -> Any:
    """Return a shallow COPY of ``snapshot`` with access-control tables removed.

    Bug-7844: strips the security-sensitive top-level keys
    (``_SECURITY_SENSITIVE_SNAPSHOT_KEYS``) so a viewer response cannot disclose
    row-security predicates, CLS classification, or persona scoping filters.
    Non-dict input (a malformed/legacy snapshot) is returned unchanged — there
    is nothing to redact and the response contract still holds. The input object
    is never mutated: the stored snapshot and the deploy/rehydrate path rely on
    these tables surviving intact.
    """
    if not isinstance(snapshot, dict):
        return snapshot
    return {
        k: v
        for k, v in snapshot.items()
        if k not in _SECURITY_SENSITIVE_SNAPSHOT_KEYS
    }


async def _verify_attribute_relationships_on_deploy(
    tenant_db: AsyncSession,
    *,
    model_id: UUID,
    deployed_version_id: UUID,
    deploy_epoch: int,
) -> None:
    """Run tenant-global attribute-relationship verification on deploy (§7.6.2).

    Fully fail-open: resolves the source connection + connector, invokes the
    shared verifier, and stages evidence rows into ``tenant_db`` (committed with
    the deploy). Any failure — no relationships, no source connection,
    verification disabled, connector error — is swallowed so it can NEVER block
    or fail a deploy. This is Phase-2 health evidence only; it authorises no
    serving route.
    """
    try:
        from shared.aggregate_connection import resolve_source_connection
        from shared.config.resolver import get_setting
        from shared.db.models import DimensionAttributeRelationship
        from shared.semantic.attribute_relationship_deploy_verify import (
            verify_model_relationships_on_deploy,
        )
        from shared.source_executor import resolve_connector_type

        # Cheap guard: skip entirely when the model has no enabled relationship.
        has_rel = (
            await tenant_db.execute(
                select(DimensionAttributeRelationship.id)
                .where(
                    DimensionAttributeRelationship.model_id == model_id,
                    DimensionAttributeRelationship.enabled.is_(True),
                )
                .limit(1)
            )
        ).first()
        if has_rel is None:
            return

        # Per-model verification can be disabled (spec §15.1); disabling makes
        # data-verified edges unavailable and records nothing.
        try:
            mode = await get_setting(
                "model.attribute_relationship_verification",
                tenant_session=tenant_db, model_id=model_id,
            )
        except Exception:
            mode = "on"
        if str(mode).lower() == "off":
            return

        try:
            verifier_version = await get_setting(
                "model.attribute_relationship_verifier_version",
                tenant_session=tenant_db,
            )
        except Exception:
            verifier_version = "v0"

        conn_obj = await resolve_source_connection(model_id, tenant_db)
        connector = await resolve_connector_type(conn_obj)

        await verify_model_relationships_on_deploy(
            db=tenant_db,
            model_id=model_id,
            deployed_version_id=deployed_version_id,
            deploy_epoch=deploy_epoch,
            verifier_version=str(verifier_version),
            conn_obj=conn_obj,
            connector=connector,
            tenant_session=tenant_db,
        )
    except Exception:
        # Verification is best-effort health evidence; a deploy must proceed even
        # when it cannot run (spec §7.6.2). Log and continue.
        logger.warning(
            "attribute-relationship deploy verification skipped for model %s",
            model_id, exc_info=True,
        )


async def _validate_join_population_on_deploy(
    tenant_db: AsyncSession,
    *,
    model_id: UUID,
    deployed_version_id: UUID,
    deploy_epoch: int,
    system_session: AsyncSession | None = None,
    snapshot: dict[str, Any] | None = None,
) -> list[Any]:
    """Classify this model's joins against the source at deploy time (Bug-8615).

    Governance contract:
    ``docs/architecture/architecture_join-population-governance.md``
    (invariants 1, 4, 5, 6). This hook remains fail-open for measurement and
    stages evidence rows into ``tenant_db``; the deploy handler applies the G5
    policy to the returned rows. The system session is required for the
    authoritative system threshold and probe budget, while validation mode
    remains a model-level tenant setting. ``snapshot`` is the immutable
    definition graph selected for this deploy; source credentials remain
    resolved from live operational state only.

    When validation is switched off for the model, or the source cannot be
    resolved, the classifier still runs with ``measure=False``: that CLEARS the
    model's previous verdicts and records conservative unmeasured ones, so the
    health surface reports "not evaluated" instead of serving a stale verdict
    from an earlier deploy.

    Deploy is a SYNCHRONOUS request, so the classifier is given a wall-clock
    probe budget (``model.join_population_probe_budget_seconds``). Without it a
    wide model on a merely-slow source could hold the deploy open for
    joins x 3 x the source statement timeout; with it the remaining joins record
    an unmeasured verdict and the deploy proceeds.
    """
    from shared.semantic.join_population_validator import (
        DEFAULT_PROBE_BUDGET_SECONDS,
        DEFAULT_ROW_EFFECT_WARNING_THRESHOLD,
    )

    conn_obj = None
    connector = ""
    measure = True
    threshold = DEFAULT_ROW_EFFECT_WARNING_THRESHOLD
    budget = DEFAULT_PROBE_BUDGET_SECONDS
    try:
        from shared.config.resolver import get_setting
        from shared.semantic.join_population_validator import (
            SETTING_PROBE_BUDGET_SECONDS,
            SETTING_ROW_EFFECT_THRESHOLD,
            SETTING_VALIDATION_MODE,
        )

        try:
            mode = await get_setting(
                SETTING_VALIDATION_MODE,
                tenant_session=tenant_db, model_id=model_id,
            )
        except Exception:
            mode = "on"
        if str(mode).lower() == "off":
            measure = False
        else:
            try:
                threshold = float(
                    await get_setting(
                        SETTING_ROW_EFFECT_THRESHOLD,
                        system_session=system_session,
                        tenant_session=tenant_db,
                    )
                )
            except Exception:
                threshold = DEFAULT_ROW_EFFECT_WARNING_THRESHOLD
            try:
                budget = float(
                    await get_setting(
                        SETTING_PROBE_BUDGET_SECONDS,
                        system_session=system_session,
                        tenant_session=tenant_db,
                    )
                )
            except Exception:
                budget = DEFAULT_PROBE_BUDGET_SECONDS
            try:
                from shared.aggregate_connection import resolve_source_connection
                from shared.source_executor import resolve_connector_type

                conn_obj = await resolve_source_connection(model_id, tenant_db)
                connector = await resolve_connector_type(conn_obj)
            except Exception:
                # No usable source connection: record unmeasured verdicts
                # rather than skipping, so the health surface cannot keep
                # showing the previous deploy's numbers.
                logger.warning(
                    "join population validation: no source connection for "
                    "model %s; recording unmeasured verdicts", model_id,
                )
                measure = False
    except Exception:
        logger.warning(
            "join population validation setup failed for model %s",
            model_id, exc_info=True,
        )
        measure = False

    try:
        import shared.semantic.join_population_validator as _jpv

        return await _jpv.validate_model_joins_on_deploy(
            db=tenant_db,
            model_id=model_id,
            deployed_version_id=deployed_version_id,
            deploy_epoch=deploy_epoch,
            conn_obj=conn_obj,
            connector=connector,
            threshold=threshold,
            budget_seconds=budget,
            measure=measure,
            tenant_session=tenant_db,
            snapshot=snapshot,
        )
    except Exception:
        # The validator already swallows its own failures; this is the last
        # backstop so a deploy can never fail because of join classification.
        logger.warning(
            "join population validation skipped for model %s",
            model_id, exc_info=True,
        )
        return []


def _validate_snapshot_for_deploy(
    snapshot: Any, version_id: UUID, version_number: int
) -> None:
    """Bug-7151: fail closed on missing, malformed, or empty snapshots.

    A deployed snapshot is an immutable contract that BI clients rely on.
    If the snapshot is not a dict, has no schema_version, or contains zero
    semantic content (no measures AND no dimensions AND no columns), the
    deploy MUST be rejected rather than silently falling back to mutable
    live metadata.

    Raises HTTPException(409) with a descriptive detail on failure.
    """
    if not isinstance(snapshot, dict):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Version v{version_number} ({version_id}) has a malformed "
                f"snapshot (expected dict, got {type(snapshot).__name__}). "
                "Save a new version before deploying."
            ),
        )
    schema_ver = snapshot.get("schema_version")
    if not schema_ver:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Version v{version_number} ({version_id}) has a snapshot "
                "without a schema_version field. This is a placeholder or "
                "legacy version that cannot be deployed. Save a new version "
                "before deploying."
            ),
        )
    # At least one semantic category must be populated.
    has_measures = bool(snapshot.get("measures"))
    has_dimensions = bool(snapshot.get("dimensions"))
    has_columns = bool(snapshot.get("columns"))
    if not has_measures and not has_dimensions and not has_columns:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Version v{version_number} ({version_id}) has an empty "
                "snapshot with no measures, dimensions, or columns. An empty "
                "model cannot be deployed. Add model content and Save before "
                "deploying."
            ),
        )

    # Bug-8614: a multi-table deployed model must have one declared population
    # anchor. A single-table model is implicitly fact, and query projections
    # are deliberately not involved in this deploy-time invariant.
    anchor_error = fact_anchor_violation(snapshot.get("tables") or [])
    if anchor_error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Version v{version_number} ({version_id}) violates the "
                f"fact-anchor contract: {anchor_error} Save a version with "
                "exactly one declared fact table before deploying."
            ),
        )

    # F-013-14 / F-101-11 / F-016-12 (Bug-9052): refuse a semantically INVALID
    # snapshot. The empty-shape check above only proves the model has content,
    # not that its content is servable. A measure/dimension flagged
    # ``is_invalid`` (the binder skips the matcher and falls back to source for
    # these — wrong-numbers risk) or a UDA that never validated / carries a
    # validation error (a generated date-hierarchy key invalidated by schema
    # drift is the exemplar) publishes cleanly today and then fails or misroutes
    # at query time. Deploy is not a health certificate unless it refuses these.
    invalid: list[str] = []
    for m in snapshot.get("measures") or []:
        if isinstance(m, dict) and m.get("is_invalid"):
            invalid.append(f"measure '{m.get('name') or m.get('id')}'")
    for d in snapshot.get("dimensions") or []:
        if isinstance(d, dict) and d.get("is_invalid"):
            invalid.append(f"dimension '{d.get('name') or d.get('id')}'")
    for u in snapshot.get("user_defined_attributes") or []:
        if not isinstance(u, dict):
            continue
        # A UDA is unservable when it never validated or carries a validation
        # error. ``validated`` is set True only on a successful validate, so
        # ``validated is False`` reliably marks an invalid/unvalidated UDA.
        if u.get("validated") is False or u.get("validation_error"):
            invalid.append(f"attribute '{u.get('name') or u.get('id')}'")
    if invalid:
        shown = ", ".join(invalid[:10])
        more = "" if len(invalid) <= 10 else f" (and {len(invalid) - 10} more)"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Version v{version_number} ({version_id}) contains invalid "
                f"model objects that would fail at query time: {shown}{more}. "
                "Fix or remove these objects and Save a new version before "
                "deploying."
            ),
        )

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
) -> None:
    """Fire-and-forget POST to agent-service /refresh-derived for each project.

    Failures are logged and swallowed — a publish must never fail because an
    optional auto-derived refresh hook could not reach the agent service.

    Bug-6814: uses an internal service token (mirroring Bug-6204's query-router
    pattern) instead of forwarding the caller's bearer. The caller may be a
    project-scoped modeler whose token the agent-service rejects, and forwarding
    a user token to an internal service is a privilege-escalation vector if the
    agent-service trusts model-service without audience scoping.
    """
    if not project_ids:
        return
    import httpx
    from shared.auth.service_principal import (
        SCOPE_AGENT_REFRESH,
        create_service_access_token,
    )

    service_token = create_service_access_token(
        principal="model-service-deploy",
        tenant_id=tenant_id,
        role="tenant_admin",
        ttl_minutes=1,
        scopes=[SCOPE_AGENT_REFRESH],
    )
    headers = {
        "Authorization": f"Bearer {service_token}",
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


async def _evict_query_router_cache(model_id: UUID, tenant_id: str) -> None:
    """Bug-5235: call the query-router's cache-eviction endpoint after
    deploy/undeploy/revert so the semantic-binding cache does not serve stale
    data.

    Best-effort, awaited with a short timeout (3 s).  We intentionally await
    rather than fire-and-forget so the cache is cleared before the next query
    can bind against stale data.  On failure the deploy/undeploy still
    succeeds, but the error is logged at WARNING so ops can investigate.

    Bug-6204: the query-router's ``DELETE /cache/models/{id}`` requires
    ``tenant_admin``, but Deploy/Undeploy/Revert are authorised at ``modeler``
    (or via a project binding). Forwarding the *caller's* bearer therefore made
    the eviction silently 403 for a project-scoped modeler — the deploy
    succeeded but the stale cache was never cleared, so the model kept serving
    the previous shape for up to the cache TTL. Mint a short-lived internal
    tenant-admin service token for this trusted system-to-system call instead of
    the user token (the same inter-service pattern the glossary bootstrap uses).
    """
    if not tenant_id:
        return
    import httpx
    from shared.auth.service_principal import (
        SCOPE_CACHE_EVICT,
        create_service_access_token,
    )

    # Short-lived (1 min): this elevated token is used only for the immediate
    # eviction call and never returned to the user, so it should not carry a
    # full user-session TTL (matches the glossary bootstrap bounded-token pattern).
    service_token = create_service_access_token(
        principal="model-service-deploy",
        tenant_id=tenant_id,
        role="tenant_admin",
        ttl_minutes=1,
        scopes=[SCOPE_CACHE_EVICT],
    )
    url = f"{_settings.QUERY_ROUTER_URL}/api/v1/cache/models/{model_id}"
    headers = {"Authorization": f"Bearer {service_token}"}
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


async def _enqueue_pending_kpi_reeval(
    tenant_db: AsyncSession,
    *,
    model_id: UUID,
    project_id: UUID,
    epoch: int,
) -> None:
    """Bug-7982 finding 6: durable outbox for the post-deploy/revert KPI re-eval.

    Written INSIDE the deploy/revert transaction so it commits atomically with the
    ``deploy_epoch`` bump. The fire-and-forget in-process trigger deletes it on a
    successful re-eval; if the process exits between the commit and the trigger,
    the row survives and the scheduler sweep drains it (logs that the re-eval is
    overdue, re-evaluates, deletes). Without this, a process death in that gap
    leaves ``$KPIs`` silently withheld until the next hourly sweep with NO signal.

    Skipped when the model has no deployed KPIs (nothing to re-evaluate). One row
    per model (unique on model_id); a newer deploy overwrites it with its epoch.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    has_deployed_kpi = (
        await tenant_db.execute(
            select(KPI.id).where(
                KPI.model_id == model_id, KPI.is_deployed.is_(True)
            ).limit(1)
        )
    ).first()
    if has_deployed_kpi is None:
        return
    now = datetime.now(timezone.utc)
    await tenant_db.execute(
        pg_insert(PendingKpiReeval)
        .values(
            model_id=model_id, project_id=project_id,
            requested_for_epoch=epoch, requested_at=now,
        )
        .on_conflict_do_update(
            constraint="uq_pending_kpi_reeval_model",
            set_={
                "project_id": project_id,
                "requested_for_epoch": epoch,
                "requested_at": now,
            },
        )
    )


async def _stale_incompatible_artifacts(
    tenant_db: AsyncSession,
    model_id: UUID,
    new_version_id: UUID,
    new_epoch: int,
) -> None:
    """F-013-02 / F-013-03 (Bug-8250): atomically stale every materialised
    aggregate, pocket AND Named Query artifact that was NOT built for the new
    deployed ``(version_id, epoch)``.

    Deploy/revert moves the model's semantic definition. An aggregate or pocket
    physically built under the previous definition may hold old numbers, so it
    must not serve as the current result. The runtime matchers already fail
    closed on the built-for gate, but staling here drives the refresh lifecycle
    (marks the artifact non-servable + surfaces it for rebuild) so a stale
    artifact is visibly out of date and rebuilds before re-entry, instead of
    silently sitting fresh-but-refused.

    Bug-8431 lane: the "rebuilds before re-entry" half of that contract is
    enforced by the scheduler's due-selection — ``sweep._refresh_tenant`` for
    aggregates and ``pocket_refresh.refresh_due_pockets`` for pockets — which
    treat BOTH the staleness flag and, authoritatively, a build binding that no
    longer matches the deployed pointer as always-due. The derived binding check
    is what makes the guarantee hold even if the staleness write below is later
    clobbered by a concurrent in-flight refresh's success path (Bug-8431): the
    refresh stamps its BUILD-START binding (Bug-8412), so the mismatch survives
    the flag being cleared. A superseded aggregate is additionally forced onto
    the FULL rebuild path (``incremental_refresh_aggregate``), because a partial
    rebuild would leave superseded rows in place and then stamp the artifact
    current.

    Runs inside the caller's deploy/revert transaction so the staling commits
    atomically with the pointer/epoch move. Artifacts already built for the new
    pointer (e.g. a fresh deploy of the same version whose artifacts were built
    after the last refresh) are left untouched. Retired/failed rows are ignored.
    """
    # Bug-8250 re-gate: the incompatibility predicate comes from the SAME module
    # as the Python comparator both runtime matchers call
    # (``shared.artifact_version_gate``), not from a second copy written out
    # here. The duplicated copy this replaces was the counter-example to the
    # "single source of truth" claim: it silently encoded a STRICTER rule than
    # the Python gate (which coerced a NULL epoch to 0 and let such an artifact
    # serve), so deploy staled a row the matcher would then have accepted.
    await tenant_db.execute(
        update(AggregateDefinition)
        .where(
            AggregateDefinition.model_id == model_id,
            AggregateDefinition.status == "active",
            AggregateDefinition.is_stale.is_(False),
            artifact_incompatible_sql(
                AggregateDefinition.built_for_version_id,
                AggregateDefinition.built_for_epoch,
                new_version_id,
                new_epoch,
            ),
        )
        .values(is_stale=True)
    )
    # Pockets: flip fresh -> stale on any row whose build binding differs. The
    # matcher only considers status="fresh" pockets, so "stale" makes them
    # non-servable and eligible for a rebuild.
    await tenant_db.execute(
        update(PocketDefinition)
        .where(
            PocketDefinition.model_id == model_id,
            PocketDefinition.status == "fresh",
            PocketDefinition.retired_at.is_(None),
            artifact_incompatible_sql(
                PocketDefinition.built_for_version_id,
                PocketDefinition.built_for_epoch,
                new_version_id,
                new_epoch,
            ),
        )
        .values(
            status="stale",
            failure_reason="Model definition changed; pocket rebuild required.",
            row_manifest=None,
            active_refresh_run_id=None,
            built_for_version_id=None,
            built_for_epoch=None,
            population_eligibility="unknown",
            population_eligibility_reason=None,
            population_proof_fingerprint=None,
        )
    )
    # F-013-03: Named Query artifacts are the third materialised family and were
    # NOT staled on deploy/revert, so after a Deploy that moved the definition a
    # fresh NQ artifact kept advertising "fresh" while the serve-time built-for
    # gate silently refused it — acceleration missed until the next cron tick
    # (or forever, if no policy). Flip fresh -> stale in the SAME transaction on
    # any artifact whose build binding no longer matches the new pointer.
    # NamedQueryArtifact carries no model_id, so scope via its named_queries.
    await tenant_db.execute(
        update(NamedQueryArtifact)
        .where(
            NamedQueryArtifact.named_query_id.in_(
                select(NamedQuery.id).where(NamedQuery.model_id == model_id)
            ),
            NamedQueryArtifact.status == "fresh",
            NamedQueryArtifact.retired_at.is_(None),
            artifact_incompatible_sql(
                NamedQueryArtifact.built_for_version_id,
                NamedQueryArtifact.built_for_epoch,
                new_version_id,
                new_epoch,
            ),
        )
        .values(status="stale")
    )


async def _prune_old_versions(
    tenant_db: AsyncSession,
    model_id: UUID,
) -> None:
    """F-013-17: enforce the ``versions.retention_count`` policy.

    Keeps the newest N saved versions per model plus the currently-deployed
    version (which must survive even if it is older than the cut-off, so a
    rollback target is never destroyed). When the setting is 0 (the default)
    this is a no-op, so existing tenants accumulate versions exactly as
    before until an admin opts in. Commits its own deletion so a prune
    failure cannot roll back the just-saved version. Never raises — a
    retention sweep must not fail the Save.

    opus5 finding 3: this runs AFTER the Save committed (which released the
    Save's advisory lock, across a synchronous git commit), so it must
    re-establish its OWN consistency: acquire the per-model lock and re-read
    ``Model.deployed_version_id`` in its own transaction. Reading the pointer
    before the lock (as a caller-passed value) risks deleting the version a
    concurrent deploy just published, dangling ``models.deployed_version_id``
    (which has no FK).
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
            return  # 0 = keep all (default) — no prune, so no lock needed

        # We ARE going to delete versions. Acquire the per-model lock so a
        # concurrent deploy/revert cannot move the deployed pointer between our
        # read and our delete, then re-read the pointer UNDER the lock (never a
        # caller-passed value read outside this critical section).
        await acquire_model_definition_lock(tenant_db, model_id)
        deployed_version_id = (
            await tenant_db.execute(
                select(Model.deployed_version_id).where(Model.id == model_id)
            )
        ).scalar_one_or_none()

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
            await tenant_db.rollback()  # nothing to delete; release the lock
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
    # Bug-6295: True for imported history rows whose original shape the bundle
    # did not carry (Bug-7623). Such a version cannot be reverted to and its
    # snapshot is a placeholder; the UI must not offer revert/restore for it.
    snapshot_unavailable: bool = False


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
    # Bug-6295: see VersionItem. When True the ``snapshot`` is a placeholder and
    # the real historical shape is unrecoverable.
    snapshot_unavailable: bool = False


class RevertBody(BaseModel):
    confirm: str  # must equal the version_id or the legacy phrase "revert to v{N}"


class DeployBody(BaseModel):
    version_id: Optional[UUID] = None  # omit -> deploy latest saved version


# G-013-01 (Bug-9171): pending-change surface. ``unsaved`` is the live/draft vs
# the latest SAVED version; ``saved_undeployed`` is the latest saved version vs
# the currently DEPLOYED version. Both ``diff`` payloads carry the same shape the
# version-diff endpoint returns (``diff_snapshots`` output).
class PendingUnsaved(BaseModel):
    base_version: Optional[int]  # latest saved version_number (None = never saved)
    base_version_unavailable: bool  # last saved version is an imported placeholder
    diff: dict[str, Any]


class PendingSavedUndeployed(BaseModel):
    deployed_version: Optional[int]  # currently-serving version_number (None = none)
    saved_version: Optional[int]  # latest saved version_number (None = never saved)
    diff: dict[str, Any]


class PendingChangesResponse(BaseModel):
    unsaved: PendingUnsaved
    saved_undeployed: PendingSavedUndeployed


class DiscardDraftResponse(BaseModel):
    status: str
    restored_to_version: int


# ---------------------------------------------------------------------------
# Authorization (binding-only — F-021-04 hard cutover, decision #9)
# ---------------------------------------------------------------------------
# There is NO zero-binding bootstrap-admin grant: a project with no binding for
# the caller denies. Human tenant/system admins still bypass.

async def _ensure_model_access(
    project_id: UUID, model_id: UUID, current_user: CurrentUser, tenant_db: AsyncSession
) -> Model:
    model = await tenant_db.get(Model, model_id)
    if model is None or model.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model {model_id} not found in project {project_id}",
        )
    if is_human_tenant_admin_or_system_admin(current_user):
        return model
    user_identity = current_user.email or current_user.user_id
    rows = await tenant_db.execute(
        select(UserAccessBinding).where(
            user_identity_matches(UserAccessBinding.user_identity, user_identity),
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
        snapshot_unavailable=bool(getattr(v, "snapshot_unavailable", False)),
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
        # Bug-6202: capture the scalar model attributes we need after the retry
        # loop NOW, while the session state is fresh. A retry rolls the session
        # back on a unique-version collision, which expires every ORM object;
        # touching ``model.display_name`` / ``model.deployed_version_id`` after
        # that triggers a lazy refresh that raises in an async session
        # (MissingGreenlet), turning the handled 409 back into a 500.
        model_display_name = model.display_name
        model_slug = model.slug
        model_canvas_layout = model.canvas_layout or {}
        deployed_version_id = model.deployed_version_id
        # Bug-7150/Bug-7982: acquire the per-model advisory lock before
        # snapshotting. This serialises Save against Save/deploy/revert AND every
        # model-definition/governance writer (named-set create/update/certify/
        # deprecate/revert/refresh), which all take the SAME key via
        # ``acquire_model_definition_lock`` — so two Saves cannot race the version
        # number and no writer can mutate live state mid-Save.
        # pg_advisory_xact_lock is released automatically on commit/rollback.
        await acquire_model_definition_lock(tenant_db, model_id)
        # Re-read the deploy pointer UNDER the lock so the value is committed-fresh
        # (a deploy could have committed between the access check and the lock).
        # NOTE: this value feeds ONLY the ``_to_item`` response ``is_deployed``
        # flag below — the retention prune re-reads the pointer under its OWN lock
        # (opus5 R4 finding 3), so do NOT assume the prune self-protects from this
        # local; keep this refresh for a correct Save response.
        await tenant_db.refresh(model, ["deployed_version_id"])
        deployed_version_id = model.deployed_version_id
        # Bug-7980 (F-013-06): definition/governance writers now DO take the
        # per-model advisory lock, but ``project_rehydrator`` (project import) is a
        # deliberate non-holder, so the lock alone cannot guarantee a
        # point-in-time-consistent multi-SELECT snapshot. Take the snapshot in a
        # dedicated REPEATABLE READ transaction so every read observes one
        # consistent committed state and no concurrent writer (locked or not) is
        # partially observed. Snapshot once; it does not change between retries.
        snap = await _consistent_snapshot(current_user.tenant_id, model_id)

        last_error: Optional[IntegrityError] = None
        _MAX_ATTEMPTS = 2
        for _attempt in range(_MAX_ATTEMPTS):
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
                # opus5 finding 6: the rollback released the transaction-scoped
                # advisory lock. Re-acquire it before the NEXT attempt so the
                # recompute+insert stays serialised against concurrent Saves.
                # Skip on the final attempt (the loop is about to exit to the 409).
                if _attempt + 1 < _MAX_ATTEMPTS:
                    await acquire_model_definition_lock(tenant_db, model_id)
                continue
            await audit(
                tenant_db, action="model.save", severity="info",
                actor_email=current_user.email,
                target_type="model", target_id=model_id,
                target_name=model_display_name,
                detail={"version_number": next_n},
            )
            await tenant_db.commit()
            await tenant_db.refresh(version)
            out = _to_item(version, deployed_version_id)

            # Git version tracking: commit the model snapshot to the tenant's
            # git repo. Best-effort -- git failure never fails the save.
            try:
                from shared.model_snapshot.yaml_serialiser import snapshot_to_yaml
                from shared.git.model_repo import commit_model as git_commit_model
                model_yaml = snapshot_to_yaml(snap, connection_name=None)
                await asyncio.to_thread(
                    git_commit_model,
                    tenant_slug=current_user.tenant_id,
                    model_slug=model_slug,
                    model_yaml=model_yaml,
                    layout_json=model_canvas_layout,
                    summary=body.summary,
                    author_email=actor,
                    version_number=next_n,
                )
            except Exception:
                logger.warning(
                    "Git commit failed for model %s v%d (non-fatal)",
                    model_id, next_n, exc_info=True,
                )

            # F-013-17: bound unbounded version growth. Default keep-all (0)
            # leaves behaviour unchanged; a configured count prunes the oldest
            # versions beyond the newest N, never touching the deployed one.
            # The prune re-reads the deployed pointer under its OWN lock (opus5
            # finding 3), so it is not passed the pre-commit value here.
            await _prune_old_versions(tenant_db, model_id)
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
        # Bug-7844 [SECURITY]: viewers may see version history/shape but not the
        # tenant's access-control policy. Redact the security tables on a COPY
        # for non-modeler callers; modeler+ (and tenant/system admins) get the
        # full snapshot unchanged. The stored snapshot_json is never mutated.
        is_modeler = await caller_has_role(
            tenant_db, current_user, project_id, "modeler", model_id=model_id,
        )
        snapshot_out = (
            v.snapshot_json
            if is_modeler
            else _redact_security_tables(v.snapshot_json)
        )
        out = VersionDetailResponse(
            id=v.id,
            version_number=v.version_number,
            summary=v.summary,
            created_at=v.created_at,
            created_by=v.created_by,
            is_deployed=(model.deployed_version_id == v.id),
            snapshot=snapshot_out,
            snapshot_unavailable=bool(getattr(v, "snapshot_unavailable", False)),
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
        # Bug-7844 [SECURITY]: redact the security tables from BOTH snapshots
        # BEFORE diffing for non-modeler callers, so no row-security predicate,
        # CLS classification, or persona scoping filter can surface as an
        # added/removed/changed diff entry. Modeler+ diff over the full
        # snapshots. Redaction is on copies; the stored snapshots are untouched.
        is_modeler = await caller_has_role(
            tenant_db, current_user, project_id, "modeler", model_id=model_id,
        )
        snap_a = va.snapshot_json or {}
        snap_b = vb.snapshot_json or {}
        if not is_modeler:
            snap_a = _redact_security_tables(snap_a)
            snap_b = _redact_security_tables(snap_b)
        out = {
            "version_a": va.version_number,
            "version_b": vb.version_number,
            "diff": diff_snapshots(snap_a, snap_b),
        }
    assert out is not None
    return out


@router.get(
    "/pending-changes",
    response_model=PendingChangesResponse,
    dependencies=[require_role("viewer")],
)
async def pending_changes(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> PendingChangesResponse:
    """G-013-01 (Bug-9171): the two pending change sets the Model Builder surfaces
    so a modeller can SEE and review what changed before saving / deploying.

      unsaved          = latest SAVED version  ->  live/draft (consistent_snapshot)
      saved_undeployed = deployed version      ->  latest SAVED version

    Both sets reuse the shared snapshot differ (``diff_snapshots``) and the same
    ``consistent_snapshot`` serialiser Save uses — there is no second differ. The
    draft-vs-saved comparison is the ONLY one the existing version-diff endpoint
    cannot serve (the live draft is not a saved version), which is why this read
    endpoint exists; saved-vs-deployed is computed here too so the surface is one
    round-trip with a single partitioned contract.

    Security (Bug-7844): the access-control tables (row-security predicates, CLS
    tags, persona scoping filters) are redacted from a COPY of every snapshot for
    non-modeler callers before diffing, exactly like ``diff_versions``.
    """
    out: Optional[PendingChangesResponse] = None
    async for tenant_db in get_tenant_db(current_user.tenant_id):
        model = await _ensure_model_access(project_id, model_id, current_user, tenant_db)
        is_modeler = await caller_has_role(
            tenant_db, current_user, project_id, "modeler", model_id=model_id,
        )

        # Latest SAVED version (max version_number) and its snapshot.
        latest = (
            await tenant_db.execute(
                select(ModelVersion)
                .where(ModelVersion.model_id == model_id)
                .order_by(ModelVersion.version_number.desc())
                .limit(1)
            )
        ).scalars().first()
        # Deployed version snapshot (None when the model is undeployed).
        deployed: Optional[ModelVersion] = None
        if model.deployed_version_id is not None:
            deployed = await tenant_db.get(ModelVersion, model.deployed_version_id)

        # Live/draft snapshot — same serialiser Save writes, so the unsaved diff
        # is computed against exactly the shape a Save would persist.
        live_snapshot = await _consistent_snapshot(current_user.tenant_id, model_id)

        def _prep(snap: Any) -> dict:
            snap = snap or {}
            if not isinstance(snap, dict):
                return {}
            return snap if is_modeler else _redact_security_tables(snap)

        live_s = _prep(live_snapshot)
        latest_unavailable = bool(
            latest is not None and getattr(latest, "snapshot_unavailable", False)
        )
        latest_s = _prep(latest.snapshot_json) if latest is not None else {}
        deployed_s = _prep(deployed.snapshot_json) if deployed is not None else {}

        # unsaved: what the draft changed since the last Save. When the model has
        # never been saved (latest is None) the whole draft is unsaved -> diff vs
        # an empty snapshot shows every element as added. When the last saved
        # version is an imported placeholder its shape is unrecoverable; flag it
        # and skip the misleading whole-model diff.
        if latest_unavailable:
            unsaved_diff: dict[str, Any] = {}
        else:
            unsaved_diff = diff_snapshots(latest_s, live_s)

        # saved_undeployed: what a Deploy of the latest saved version would change
        # relative to what is serving now. Only meaningful once a version exists;
        # when nothing is deployed the entire saved model is pending its first
        # deploy (diff vs an empty snapshot).
        if latest is None:
            saved_undeployed_diff: dict[str, Any] = {}
        else:
            saved_undeployed_diff = diff_snapshots(deployed_s, latest_s)

        out = PendingChangesResponse(
            unsaved=PendingUnsaved(
                base_version=(latest.version_number if latest is not None else None),
                base_version_unavailable=latest_unavailable,
                diff=unsaved_diff,
            ),
            saved_undeployed=PendingSavedUndeployed(
                deployed_version=(
                    deployed.version_number if deployed is not None else None
                ),
                saved_version=(latest.version_number if latest is not None else None),
                diff=saved_undeployed_diff,
            ),
        )
    assert out is not None
    return out


@router.post(
    "/discard-draft",
    response_model=DiscardDraftResponse,
    dependencies=[require_role("modeler")],
)
async def discard_draft(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> DiscardDraftResponse:
    """G-013-01 (Bug-9171): reset the live/DRAFT model back to the latest SAVED
    version, throwing away unsaved edits.

    This is a DRAFT-only operation. It rewrites live model metadata to the last
    saved snapshot; it does NOT append a version, move the deploy pointer, bump
    ``deploy_epoch``, or change what the gateway serves. The query path binds the
    immutable DEPLOYED snapshot, which is untouched here — so discarding draft
    edits can never change production numbers (only Deploy does that). That is
    also why no query-router cache eviction or artifact staling is required here:
    nothing that is served changed.

    Reuses ``rehydrate_into_live`` (the same primitive revert uses) with the same
    definition-only flags (Bug-6205): governance is preserved and materialised
    aggregates / pockets are left in place. Requires ``modeler`` — the role that
    makes draft edits in the first place. Discarding all the way back to the
    DEPLOYED version is a heavier, history-preserving action handled by the
    existing admin Revert endpoint, not here.
    """
    out: Optional[DiscardDraftResponse] = None
    async for tenant_db in get_tenant_db(current_user.tenant_id):
        model = await _ensure_model_access(project_id, model_id, current_user, tenant_db)
        # F-013-12 pattern: cache display_name before the lock / any rollback can
        # expire the ORM instance and make the audit access lazy-load
        # (MissingGreenlet), as Save/revert already do.
        model_display_name = model.display_name
        # Serialise against Save / deploy / revert and every definition writer
        # under the same per-model advisory lock: a discard is a full live-state
        # rewrite and must not interleave with a concurrent Save or edit.
        await acquire_model_definition_lock(tenant_db, model_id)
        latest = (
            await tenant_db.execute(
                select(ModelVersion)
                .where(ModelVersion.model_id == model_id)
                .order_by(ModelVersion.version_number.desc())
                .limit(1)
            )
        ).scalars().first()
        if latest is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "This model has no saved version to discard to. Save the "
                    "model first, or delete it to abandon it."
                ),
            )
        if getattr(latest, "snapshot_unavailable", False):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "The latest saved version was imported without its model "
                    "shape, so the draft cannot be reset to it."
                ),
            )
        try:
            await rehydrate_into_live(
                model_id, latest.snapshot_json, tenant_db,
                mode=RehydrationMode.RESTORE,
                # Definition-only, exactly like revert (Bug-6205 / F-013-02):
                # keep live governance and the materialised artifacts in place.
                restore_governance=False,
                preserve_aggregates=True,
                preserve_pockets=True,
                # A draft reset retires nothing — the deploy pointer is unchanged,
                # so no artifact built-for the deployed version is orphaned.
                drop_orphan_aggregates=False,
                actor=current_user.email or current_user.user_id,
            )
        except OneFactViolationError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            )
        except (SnapshotSchemaError, SnapshotVersionError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            )
        await audit(
            tenant_db, action="model.discard_draft", severity="info",
            actor_email=current_user.email,
            target_type="model", target_id=model_id,
            target_name=model_display_name,
            detail={"restored_to_version": latest.version_number},
        )
        await tenant_db.commit()
        out = DiscardDraftResponse(
            status="ok", restored_to_version=latest.version_number
        )
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
    """Revert: rehydrate the chosen version into live state and record the
    result as a NEW version at the top of the history.

    Bug-7906 (DATA LOSS): revert used to execute
    ``DELETE FROM model_versions WHERE version_number > N``, permanently
    destroying every version created after the reverted-to point. Version
    history is an audit and recovery record; a revert must never erase it.

    Contract (restore-as-new-version):
    - No existing version row is ever deleted by a revert.
    - Reverting to version ``N`` appends a NEW version ``max(version_number)+1``
      whose ``snapshot_json`` is exactly ``N``'s snapshot (the reverted-to
      DEFINITION). Its summary is ``"Revert to v{N}"``.
    - Because the newest version now represents live state, the deploy pointer
      (when the model was deployed) follows to the NEW version, not to ``N``.
    - Governance (personas, data tags, row security, named-set certification)
      is live operational state and is preserved, not rolled back
      (``restore_governance=False``) — unchanged from prior behaviour.
    - The revert-created version is authentic (``snapshot_unavailable`` defaults
      to False), so it is itself revertible/deployable and lets a user move
      forward again to any state that previously existed.
    """
    async for tenant_db in get_tenant_db(current_user.tenant_id):
        model = await _ensure_model_access(project_id, model_id, current_user, tenant_db)
        model_slug = model.slug
        # F-013-12 (Bug-8998): cache display_name NOW, before the lock/retry and
        # any rollback can expire the ORM instance. The audit calls below run on
        # a possibly-expired ``model``; a lazy ``model.display_name`` load there
        # raises MissingGreenlet and 500s what looks like a failed revert. Save
        # already caches this scalar for the same reason.
        model_display_name = model.display_name
        # Derived-grain routing (spec §7.6.2): revert moves the deploy pointer and
        # bumps deploy_epoch, so it MUST serialise against deploy under the same
        # per-model advisory lock. Without it a concurrent deploy + revert do not
        # exclude each other and can lose an epoch update, leaving attribute-
        # relationship evidence tagged to an epoch the router trust predicate
        # (rule 3) would wrongly accept. Bug-7982: the SAME lock is taken by every
        # named-set definition/governance writer, so a concurrent governance
        # change cannot commit between this revert's governance capture and its
        # commit (which would be silently lost). Released on commit/rollback.
        await acquire_model_definition_lock(tenant_db, model_id)
        # Reread the epoch under the lock so the bump below is against the
        # committed current value, not one read before a racing deploy won.
        await tenant_db.refresh(model, ["deploy_epoch", "deployed_version_id"])
        v = await tenant_db.get(ModelVersion, version_id)
        if v is None or v.model_id != model_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Version not found",
            )
        # Bug-6295: an imported history row has no faithful snapshot (the export
        # bundle omits per-version snapshots — Bug-7623). Its snapshot_json is a
        # placeholder; reverting would rehydrate an empty definition, and the
        # previous behaviour of stamping today's live shape onto every imported
        # version silently served the wrong model under an old label. Refuse the
        # revert outright rather than restore a shape we cannot reproduce.
        if getattr(v, "snapshot_unavailable", False):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Cannot revert to this version: it was imported from a "
                    "backup/bundle that did not carry its historical model "
                    "shape, so the original definition is unrecoverable. "
                    "Reverting would not reproduce that version. Deploy the "
                    "current model or import a bundle that includes per-version "
                    "snapshots instead."
                ),
            )
        # Bug-5655 / Bug-5236: accept a language-neutral confirmation token
        # (the version ID as a string) in addition to the legacy English
        # phrase "revert to v{N}". This removes the hardcoded English
        # dependency so non-English UIs can use the version ID instead.
        legacy_phrase = f"revert to v{v.version_number}"
        confirmed = body.confirm.strip()
        if (
            confirmed.lower() != legacy_phrase.lower()
            and confirmed != str(v.id)
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"confirm must equal the version ID ({v.id!s}) "
                    f"or '{legacy_phrase}'"
                ),
            )
        # Rehydrate first (inside the same transaction).
        # Preserve aggregates and pockets — the materialised artifacts are
        # marked invalid/stale if their dependencies are missing, but not
        # force-retired, so they remain visible and retire naturally via the
        # scheduler when no longer used. Named sets are DEFINITION (Bug-7982),
        # so they are rebuilt from the snapshot with live governance preserved.
        try:
            await rehydrate_into_live(
                model_id, v.snapshot_json, tenant_db,
                # Bug-8768 (B8768-R1-05): an in-place version restore re-points at
                # the SAME source, so a valid append-only declaration is preserved
                # (never reset). This is the single RESTORE caller of the rehydrator.
                mode=RehydrationMode.RESTORE,
                # Mark aggregates absent from the reverted-to snapshot as
                # retired (the scheduler sweep drops their physical table);
                # aggregates present in the snapshot are preserved. F-013-02.
                drop_orphan_aggregates=True,
                preserve_aggregates=True,
                preserve_pockets=True,
                # Bug-7982 (F-013-08): named sets are DEFINITION (expression,
                # dimensions, builder), not a materialised artifact like an
                # aggregate/pocket. The rehydrator now ALWAYS rebuilds the
                # named-set definition from the snapshot IN PLACE
                # (``_upsert_definition_rows`` via ``_insert_named_sets`` in
                # rehydrator.py) and, under restore_governance=False, preserves
                # live named-set GOVERNANCE (certification/owner/replacement) by
                # simply omitting those columns from the write — closing the
                # split-brain where MDX clients kept serving the newer set
                # expression. (There is no standalone
                # ``_restore_named_set_governance`` function; that mechanism was
                # folded into the same generic in-place-upsert path KPIs use.)
                # Bug-6205: revert restores model DEFINITION only, not
                # governance. Personas, data_tags, row_security, and named-set
                # governance are live operational state that must survive a
                # revert — reverting a model's shape must not silently roll back
                # who can see which rows/columns or a set's certification.
                # Deploy and import keep the default (True) and fully restore
                # governance from the snapshot.
                restore_governance=False,
                actor=current_user.email or current_user.user_id,
            )
        except OneFactViolationError as exc:
            # F-013-09: a two-fact snapshot cannot be reverted onto — 409, the
            # same conflict class the deploy gate and one-fact table guard use.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            )
        except (SnapshotSchemaError, SnapshotVersionError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            )
        # Bug-7906 (DATA LOSS): do NOT delete newer versions. Reverting is a
        # forward-moving operation — it appends a NEW version that captures the
        # reverted-to shape, preserving the full history (including every
        # version created after the reverted-to point) so it can be inspected
        # or moved forward to again. The per-model advisory lock acquired above
        # (``acquire_model_definition_lock``) serialises this against Save and
        # every other definition writer, so a plain ``max(version_number)+1``
        # cannot race the version number here.
        actor = current_user.email or current_user.user_id
        last_q = await tenant_db.execute(
            select(ModelVersion.version_number)
            .where(ModelVersion.model_id == model_id)
            .order_by(ModelVersion.version_number.desc())
            .limit(1)
        )
        next_n = (last_q.scalar_one_or_none() or 0) + 1
        restored_version = ModelVersion(
            id=uuid4(),
            model_id=model_id,
            version_number=next_n,
            # The reverted-to DEFINITION verbatim. ``v`` is guaranteed authentic
            # here (a placeholder/imported row was refused above), so the new
            # row is itself a faithful, revertible snapshot.
            snapshot_json=v.snapshot_json,
            summary=f"Revert to v{v.version_number}",
            created_by=actor,
        )
        tenant_db.add(restored_version)
        await tenant_db.flush()
        # Revert is a hard rewrite of live state, so the deploy pointer must
        # follow. Without this, a model deployed to v2 that gets reverted to
        # v3 would still report v2 as deployed while query-router serves v3's
        # live shape — the exact divergence F-8 forbids. The pointer targets the
        # NEW version (whose snapshot equals live state), not the older row.
        was_deployed = model.deployed_version_id is not None
        if was_deployed:
            model.deployed_version_id = restored_version.id
            model.last_deployed_at = datetime.now(timezone.utc)
        # Bug-7140: bump deploy_epoch on every revert so multi-replica
        # caches see the content change even when the deploy pointer
        # happens to target the same version_id (revert-to-same-version).
        model.deploy_epoch = (getattr(model, "deploy_epoch", 0) or 0) + 1
        # F-013-02 / F-013-03 (Bug-8250): revert rewrites the live definition and
        # (when deployed) moves/re-stamps the pointer + epoch, so every preserved
        # aggregate/pocket that was built for the OLD pointer is now
        # incompatible. Atomically stale them in THIS transaction. This runs for a
        # deployed revert (new pointer = v.id) — the only case that serves — using
        # the model's now-current deployed_version_id + epoch. An undeployed
        # revert leaves deployed_version_id None, so nothing serves and the
        # staling is skipped.
        if was_deployed:
            await _stale_incompatible_artifacts(
                tenant_db, model_id=model_id,
                new_version_id=model.deployed_version_id, new_epoch=model.deploy_epoch,
            )
            # Bug-7982 finding 6: durable outbox for the post-revert KPI re-eval,
            # written in THIS transaction so a crash before the fire-and-forget
            # trigger cannot silently strand $KPIs on the old epoch.
            await _enqueue_pending_kpi_reeval(
                tenant_db, model_id=model_id, project_id=project_id,
                epoch=model.deploy_epoch,
            )
        # F-013-08: revert rewrites live state and moves the deploy pointer, so
        # it gets a critical audit event — heavier than model.delete's severity.
        # (Bug-7906: it no longer deletes history; it appends a new version.)
        await audit(
            tenant_db, action="model.revert", severity="critical",
            actor_email=current_user.email,
            target_type="model", target_id=model_id,
            target_name=model_display_name,
            detail={
                "reverted_to_version": v.version_number,
                "restored_as_version": next_n,
                "was_deployed": was_deployed,
            },
        )
        # Bug-7141: evict query-router cache before commit to narrow the
        # stale-data window (same pattern as deploy/undeploy).
        await _evict_query_router_cache(model_id, current_user.tenant_id)
        await tenant_db.commit()

        # Git restore commit: record the revert in the tenant git repo.
        try:
            from shared.model_snapshot.yaml_serialiser import snapshot_to_yaml
            from shared.git.model_repo import commit_restore as git_commit_restore
            model_yaml = snapshot_to_yaml(v.snapshot_json, connection_name=None)
            revert_canvas = v.snapshot_json.get("canvas_layout") or (
                v.snapshot_json.get("model", {}).get("canvas_layout") or {}
            )
            await asyncio.to_thread(
                git_commit_restore,
                tenant_slug=current_user.tenant_id,
                model_slug=model_slug,
                model_yaml=model_yaml,
                layout_json=revert_canvas,
                author_email=current_user.email or current_user.user_id,
                restored_from=v.version_number,
                # Bug-7906: the restore is recorded as the NEWLY-APPENDED version
                # (next_n), not the reverted-to number. Tagging v{v.version_number}
                # would collide with the tag commit_model already created for that
                # save, failing the whole restore commit for a git-working tenant.
                new_version=next_n,
            )
        except Exception:
            logger.warning(
                "Git restore commit failed for model %s (non-fatal)",
                model_id, exc_info=True,
            )

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

        # Bug-6203: revert is a hard rewrite of live model state, so it must run
        # the SAME post-mutation fan-out Deploy/Undeploy perform. Without it the
        # KPI evaluation cache, the agent-service auto-derived context, and the
        # query-router semantic-binding cache all keep serving the pre-revert
        # shape — the divergence deploy already guards against, reopened on the
        # revert path.

        # Invalidate KPI evaluation cache for this model.
        from src.kpi_cache import get_kpi_cache
        get_kpi_cache().invalidate_model(model_id)

        # F-013-08: emit a webhook so downstream consumers (audit pipelines,
        # ops alerting) learn that production state was rewritten.
        await emit_webhook(current_user.tenant_id, "model.reverted", {
            "model_id": str(model_id),
            "model_name": model_display_name,
            "reverted_to_version": v.version_number,
            "actor": current_user.email,
        })

        # Notify agent-service to refresh auto-derived per-model context for
        # every project whose agent has this model on its allow-list. Fire-and-
        # forget; never fail the revert on this call (mirrors deploy/undeploy).
        agent_project_ids = list(
            (
                await tenant_db.execute(
                    select(ProjectAgentModel.project_id)
                    .where(ProjectAgentModel.model_id == model_id)
                    .distinct()
                )
            ).scalars().all()
        )
        if agent_project_ids:
            task = asyncio.create_task(
                _notify_agent_service_refresh_derived(
                    tenant_id=current_user.tenant_id,
                    project_ids=agent_project_ids,
                )
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

        # Bug-8395: a deployed revert is the FOURTH committed deploy-pointer
        # move (after deploy and the two import paths Bug-8029 wired) and the
        # one that most needs fresh predictive candidates: the rehydrate above
        # ran with drop_orphan_aggregates=True, so every predictive aggregate
        # absent from the reverted-to snapshot has just been retired and its
        # physical table dropped. Without this trigger the model waits for the
        # next scheduled predictive sweep tick (up to a day) with nothing built.
        # Only meaningful when the revert moved the serving pointer; an
        # undeployed revert serves nothing. Fire-and-forget / best-effort /
        # idempotent per (deployed version, epoch) — the helper swallows all
        # failures and never blocks or fails the revert.
        if was_deployed:
            cold_start_task = asyncio.create_task(
                trigger_predictive_cold_start(current_user.tenant_id, model_id)
            )
            _background_tasks.add(cold_start_task)
            cold_start_task.add_done_callback(_background_tasks.discard)

        # Bug-7982 completion round (availability): a deployed revert bumps
        # deploy_epoch the same way deploy does, so the $KPIs serve predicate
        # (Bug-7982 residual 2) withholds every kpi_latest row stamped with the
        # OLD epoch until re-evaluated — up to an hour of silent, empty-looking
        # $KPIs if left to the scheduled hourly sweep. Kick a re-evaluation NOW
        # (mirrors deploy_model). Only meaningful when the revert actually moved
        # the deployed/serving pointer; an undeployed revert serves nothing.
        # Fire-and-forget / best-effort: never blocks or fails the revert.
        if was_deployed:
            _deployed_kpi_ids = list(
                (
                    await tenant_db.execute(
                        select(KPI.id).where(
                            KPI.model_id == model_id, KPI.is_deployed.is_(True),
                        )
                    )
                ).scalars().all()
            )
            if _deployed_kpi_ids:
                kpi_reeval_task = asyncio.create_task(
                    trigger_post_deploy_kpi_reeval(
                        current_user.tenant_id, project_id, model_id,
                        _deployed_kpi_ids, deploy_epoch=model.deploy_epoch,
                    )
                )
                _background_tasks.add(kpi_reeval_task)
                kpi_reeval_task.add_done_callback(_background_tasks.discard)

        # Bug-7141: eviction moved before commit above (see Bug-7141 comment)
        # Bug-7142: surface preserved-governance state so the frontend can
        # inform the user that personas, data tags, row-security rules, and
        # KPI governance were NOT rolled back (restore_governance=False).
        return {
            "status": "ok",
            "reverted_to": str(version_id),
            "governance_preserved": True,
            "governance_preserved_note": (
                "Personas, data tags, row-security rules, and KPI governance "
                "were preserved from the live model and not rolled back to the "
                "reverted version."
            ),
        }
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="tenant database session unavailable",
    )


# ---------------------------------------------------------------------------
# Deploy / Undeploy (Phase 5)
# ---------------------------------------------------------------------------

@router.post("/deploy", dependencies=[require_role("modeler")])
async def deploy_model(
    project_id: UUID,
    model_id: UUID,
    body: DeployBody = Body(default_factory=DeployBody),
    current_user: CurrentUser = Depends(forbid_embed_user),
    system_db: AsyncSession = Depends(get_system_db),
) -> dict:
    async for tenant_db in get_tenant_db(current_user.tenant_id):
        model = await _ensure_model_access(project_id, model_id, current_user, tenant_db)
        model_slug = model.slug
        # F-013-12 (Bug-8998): cache display_name before lock/retry/rollback can
        # expire the ORM instance (see revert). Save caches it for the same reason.
        model_display_name = model.display_name
        # Derived-grain routing (spec §7.6.2): serialise deploy verification +
        # pointer/epoch movement under a per-model durable advisory lock so a
        # concurrent deploy/revert cannot interleave and leave attribute-
        # relationship evidence tagged to an epoch the router trust predicate
        # (rule 3) would wrongly accept. The lock is released automatically on
        # commit/rollback. Mirrors the Save-path lock (Bug-7150) keyed on the same
        # 63-bit model id, so Save and deploy also serialise against each other.
        await acquire_model_definition_lock(tenant_db, model_id)
        # Reread the model row under the lock so the epoch we bump below is the
        # committed current epoch, not a value read before a racing deploy won.
        await tenant_db.refresh(model, ["deploy_epoch", "deployed_version_id"])
        version_id = body.version_id
        selected_version: ModelVersion
        if version_id is None:
            # Deploy latest saved version. Bug-6295: imported history rows are
            # non-servable placeholders (snapshot_unavailable) with a {} snapshot
            # — never deploy one. Select the highest-numbered AUTHENTIC version so
            # deploy-latest resolves a real snapshot even when placeholder history
            # sits above it (e.g. a model imported with history but no authentic
            # version appended). ``is_distinct_from(True)`` keeps NULL/False rows.
            latest_q = await tenant_db.execute(
                select(ModelVersion)
                .where(
                    ModelVersion.model_id == model_id,
                    ModelVersion.snapshot_unavailable.is_distinct_from(True),
                )
                .order_by(ModelVersion.version_number.desc())
                .limit(1)
            )
            latest = latest_q.scalar_one_or_none()
            if latest is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "Model has no deployable saved version; click Save "
                        "first. (Imported version history alone cannot be "
                        "deployed — its historical shapes are unavailable.)"
                    ),
                )
            version_id = latest.id
            selected_version = latest
            # Bug-7151: validate snapshot integrity before committing deploy.
            _validate_snapshot_for_deploy(
                latest.snapshot_json, latest.id, latest.version_number,
            )
        else:
            v = await tenant_db.get(ModelVersion, version_id)
            if v is None or v.model_id != model_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Version not found for this model",
                )
            selected_version = v
            # Bug-7151: validate snapshot integrity before committing deploy.
            _validate_snapshot_for_deploy(
                v.snapshot_json, v.id, v.version_number,
            )
        next_deploy_epoch = (getattr(model, "deploy_epoch", 0) or 0) + 1

        # Bug-8615 / G5: classify before mutating the deploy pointer or
        # staging any other publish state.  The validator remains fail-open
        # for source failures, but a successfully measured threshold breach is
        # an explicit modeller action gate.
        join_population_checks = await _validate_join_population_on_deploy(
            tenant_db,
            model_id=model_id,
            deployed_version_id=version_id,
            deploy_epoch=next_deploy_epoch,
            system_session=system_db,
            snapshot=selected_version.snapshot_json,
        )
        from shared.semantic.join_population_validator import (
            DEFAULT_ROW_EFFECT_WARNING_THRESHOLD,
            SETTING_ROW_EFFECT_THRESHOLD,
            blocking_join_population_rows,
            snapshot_join_labels,
        )
        try:
            from shared.config.resolver import get_setting

            authoritative_threshold = float(
                await get_setting(
                    SETTING_ROW_EFFECT_THRESHOLD,
                    system_session=system_db,
                    tenant_session=tenant_db,
                )
            )
        except Exception:
            # The validator uses the same fail-open default when policy
            # resolution fails. Keep the route predicate and its response
            # diagnostic aligned with that fallback.
            authoritative_threshold = DEFAULT_ROW_EFFECT_WARNING_THRESHOLD

        blocking_checks = blocking_join_population_rows(
            join_population_checks, threshold=authoritative_threshold,
        )
        if blocking_checks:
            selected_labels = snapshot_join_labels(selected_version.snapshot_json)
            offending = [
                {
                    **selected_labels.get(
                        str(getattr(row, "join_id", "")),
                        {"join_id": str(getattr(row, "join_id", ""))},
                    ),
                    "population_participation": str(
                        getattr(row, "population_participation", "undeclared")
                    ),
                    "status": str(getattr(row, "status", "BLOCKED")),
                    "row_effect_ratio": getattr(row, "row_effect_ratio", None),
                    "reason": getattr(row, "reason", None),
                }
                for row in blocking_checks
            ]
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "JOIN_POPULATION_BLOCKED",
                    "message": (
                        "Deployment refused because measured join-population "
                        "effects exceed the system threshold. Declare each "
                        "offending join's population role accurately "
                        "(population_defining when it defines base rows) or "
                        "fix the join/source data, then deploy again."
                    ),
                    "threshold": authoritative_threshold,
                    "joins": offending,
                },
            )

        model.deployed_version_id = version_id
        model.last_deployed_at = datetime.now(timezone.utc)
        # Bug-7140: bump deploy_epoch so multi-replica caches keyed on
        # (model_id, deployed_version_id, deploy_epoch) see a new key.
        model.deploy_epoch = next_deploy_epoch

        # Derived-grain routing (Bug-7359, spec §7.6.2): tenant-global
        # complete-data verification of declared attribute relationships against
        # the deployed version, with evidence bound to the NEW deploy epoch and
        # committed in THIS deploy transaction. This is model-HEALTH evidence,
        # not a serving authority and NOT a publish blocker — it never raises and
        # never blocks deploy (Phase 2 authorises no route). The deploy already
        # runs the semantic-binding fan-out below; verification simply records
        # evidence before the same commit.
        await _verify_attribute_relationships_on_deploy(
            tenant_db, model_id=model_id,
            deployed_version_id=version_id, deploy_epoch=model.deploy_epoch,
        )

        # F-013-02 / F-013-03 (Bug-8250): atomically stale every aggregate/pocket
        # built for a different version/epoch than the one now deployed, in THIS
        # transaction so it commits with the pointer move. A materialisation built
        # under the previous definition must not serve as the current result.
        await _stale_incompatible_artifacts(
            tenant_db, model_id=model_id,
            new_version_id=version_id, new_epoch=model.deploy_epoch,
        )

        await audit(
            tenant_db, action="model.deploy", severity="warn",
            actor_email=current_user.email,
            target_type="model", target_id=model_id,
            target_name=model_display_name,
            detail={"version_id": str(version_id)},
        )
        # Bug-7982 finding 6: enqueue the durable post-deploy KPI re-eval outbox
        # row in THIS transaction (before commit) so a process crash between the
        # commit and the fire-and-forget trigger below cannot silently lose the
        # re-eval and leave $KPIs withheld until the next hourly sweep.
        await _enqueue_pending_kpi_reeval(
            tenant_db, model_id=model_id, project_id=project_id,
            epoch=model.deploy_epoch,
        )

        # Bug-7141: evict query-router cache before commit to narrow the
        # window where concurrent queries can bind against stale data.
        await _evict_query_router_cache(model_id, current_user.tenant_id)

        await tenant_db.commit()

        # Git deploy tag: mark this version as deployed in the tenant git repo.
        try:
            from shared.git.model_repo import tag_deploy as git_tag_deploy
            deployed_v = await tenant_db.get(ModelVersion, version_id)
            if deployed_v:
                await asyncio.to_thread(
                    git_tag_deploy,
                    tenant_slug=current_user.tenant_id,
                    model_slug=model_slug,
                    version_number=deployed_v.version_number,
                )
        except Exception:
            logger.warning(
                "Git deploy tag failed for model %s (non-fatal)",
                model_id, exc_info=True,
            )

        # Invalidate KPI evaluation cache for this model
        from src.kpi_cache import get_kpi_cache
        get_kpi_cache().invalidate_model(model_id)

        await emit_webhook(current_user.tenant_id, "model.published", {
            "model_id": str(model_id),
            "model_name": model_display_name,
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
        if agent_project_ids:
            task = asyncio.create_task(
                _notify_agent_service_refresh_derived(
                    tenant_id=current_user.tenant_id,
                    project_ids=agent_project_ids,
                )
            )
            # F-013-13: retain a strong reference until the task finishes so
            # the event loop does not GC it mid-flight.
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

        # Bug-8029: a fresh deploy must kick the optimizer's durable predictive
        # cold-start pipeline so predictive candidates are generated now instead
        # of waiting for the next scheduled predictive sweep tick. Fire-and-
        # forget / best-effort / idempotent per deployed version — the helper
        # swallows all failures and never blocks or fails the deploy.
        cold_start_task = asyncio.create_task(
            trigger_predictive_cold_start(current_user.tenant_id, model_id)
        )
        _background_tasks.add(cold_start_task)
        cold_start_task.add_done_callback(_background_tasks.discard)

        # Bug-7982 completion round (availability): this deploy just bumped
        # deploy_epoch, so the $KPIs serve predicate (Bug-7982 residual 2)
        # withholds every kpi_latest row still stamped with the OLD epoch until
        # re-evaluated — up to an hour of silent, empty-looking $KPIs if left to
        # the scheduled hourly sweep. Kick a re-evaluation NOW so it is
        # repopulated in seconds. Fire-and-forget / best-effort: never blocks or
        # fails the deploy; the sweep remains the fallback.
        _deployed_kpi_ids = list(
            (
                await tenant_db.execute(
                    select(KPI.id).where(
                        KPI.model_id == model_id, KPI.is_deployed.is_(True),
                    )
                )
            ).scalars().all()
        )
        if _deployed_kpi_ids:
            kpi_reeval_task = asyncio.create_task(
                trigger_post_deploy_kpi_reeval(
                    current_user.tenant_id, project_id, model_id, _deployed_kpi_ids,
                    deploy_epoch=model.deploy_epoch,
                )
            )
            _background_tasks.add(kpi_reeval_task)
            kpi_reeval_task.add_done_callback(_background_tasks.discard)

        # Bug-7141: eviction moved before commit above (see Bug-7141 comment)
        #
        # Bug-8615 invariant 6: surface the join-population rollup at the point
        # the modeller acted, read from the rows the deploy transaction just
        # committed. The compatibility ``warn_only`` field is false; measured
        # policy blockers were refused before this success path. Empty dict
        # when the summary could not be read (it never raises).
        from src.api.join_population_health import summarise_join_population

        return {
            "status": "ok",
            "deployed_version_id": str(version_id),
            "last_deployed_at": model.last_deployed_at.isoformat(),
            "join_population": await summarise_join_population(
                tenant_db, model_id, snapshot=selected_version.snapshot_json,
            ),
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
        # F-013-12 (Bug-8998): cache display_name before lock/retry/rollback can
        # expire the ORM instance (see revert/deploy).
        model_display_name = model.display_name
        # Derived-grain routing (spec §7.6.2): undeploy clears the deploy pointer
        # and bumps the epoch, so it serialises against deploy/revert under the
        # same per-model advisory lock (the epoch-moving trio must be mutually
        # exclusive so no evidence is left tagged to a lost epoch).
        await acquire_model_definition_lock(tenant_db, model_id)
        await tenant_db.refresh(model, ["deploy_epoch", "deployed_version_id"])
        model.deployed_version_id = None
        # Bug-7140: bump deploy_epoch so multi-replica caches see the
        # undeploy even when the old (model_id, old_version_id) key
        # lingers in other replicas' in-process caches.
        model.deploy_epoch = (getattr(model, "deploy_epoch", 0) or 0) + 1
        await audit(
            tenant_db, action="model.undeploy", severity="warn",
            actor_email=current_user.email,
            target_type="model", target_id=model_id,
            target_name=model_display_name,
        )

        # Bug-7141: evict the query-router cache BEFORE the DB commit to
        # narrow the window where a concurrent query could bind against
        # stale cached data. For undeploy, the old cache key (model_id,
        # old_version_id) persists until TTL if eviction happens after
        # the pointer is cleared.
        await _evict_query_router_cache(model_id, current_user.tenant_id)

        await tenant_db.commit()

        # Invalidate KPI evaluation cache for this model
        from src.kpi_cache import get_kpi_cache
        get_kpi_cache().invalidate_model(model_id)

        await emit_webhook(current_user.tenant_id, "model.undeployed", {
            "model_id": str(model_id),
            "model_name": model_display_name,
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
        if agent_project_ids:
            task = asyncio.create_task(
                _notify_agent_service_refresh_derived(
                    tenant_id=current_user.tenant_id,
                    project_ids=agent_project_ids,
                )
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

        # Bug-7141: eviction moved before commit above (see Bug-7141 comment)
        return {"status": "ok"}
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="tenant database session unavailable",
    )


# ---------------------------------------------------------------------------
# Git history (B4 + B5)
# ---------------------------------------------------------------------------

class GitCommitEntry(BaseModel):
    sha: str
    type: str
    message: str
    version: Optional[int] = None
    tags: list[str] = []
    author: str
    timestamp: str


class GitLogResponse(BaseModel):
    commits: list[GitCommitEntry]


class GitDiffResponse(BaseModel):
    diff_text: str


@router.get(
    "/git/log",
    response_model=GitLogResponse,
    dependencies=[require_role("viewer")],
)
async def git_log(
    project_id: UUID,
    model_id: UUID,
    limit: int = 50,
    offset: int = 0,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> GitLogResponse:
    """Return the git commit log for a model."""
    async for tenant_db in get_tenant_db(current_user.tenant_id):
        model = await _ensure_model_access(
            project_id, model_id, current_user, tenant_db
        )
        try:
            from shared.git.model_repo import get_log
            entries = await asyncio.to_thread(
                get_log,
                tenant_slug=current_user.tenant_id,
                model_slug=model.slug,
                limit=min(limit, 200),
                offset=max(offset, 0),
            )
        except Exception:
            logger.warning("git_log failed for model %s", model_id, exc_info=True)
            entries = []
        return GitLogResponse(
            commits=[GitCommitEntry(**e) for e in entries]
        )
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="tenant database session unavailable",
    )


@router.get(
    "/git/diff/{sha1}/{sha2}",
    response_model=GitDiffResponse,
    dependencies=[require_role("viewer")],
)
async def git_diff(
    project_id: UUID,
    model_id: UUID,
    sha1: str,
    sha2: str,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> GitDiffResponse:
    """Return the unified diff between two commits for a model."""
    async for tenant_db in get_tenant_db(current_user.tenant_id):
        model = await _ensure_model_access(
            project_id, model_id, current_user, tenant_db
        )
        try:
            from shared.git.model_repo import get_diff
            diff_text = await asyncio.to_thread(
                get_diff,
                tenant_slug=current_user.tenant_id,
                model_slug=model.slug,
                sha1=sha1,
                sha2=sha2,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            )
        except Exception:
            logger.warning(
                "git_diff failed for model %s", model_id, exc_info=True
            )
            diff_text = ""
        return GitDiffResponse(diff_text=diff_text)
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="tenant database session unavailable",
    )
