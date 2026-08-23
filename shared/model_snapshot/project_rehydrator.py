"""Project-level import rehydrator.

Imports a ProjectBundle into the target tenant, creating or replacing a
project with all its children. Runs inside a single DB transaction --
the caller's session must NOT commit until this function returns.

See docs/architecture/architecture_project-import-export.md for the
full import flow specification.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timezone
import logging
import uuid
from typing import Any, Optional
from uuid import UUID

from cryptography.fernet import Fernet
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.identity import (
    canonical_user_identity,
    is_service_identity,
    user_identity_matches,
)
from shared.schemas.domains.tenants_projects import (
    _find_sensitive_config_keys,
    _strip_sensitive_keys,
)
from shared.db.models import (
    AgentConversation,
    AgentJudgeRubric,
    AgentWebhookDlq,
    AggregateDefinition,
    LLMProviderConfig,
    LocalUser,
    Model,
    PocketDefinition,
    Project,
    ProjectAgentConfig,
    ProjectAgentModel,
    ProjectAgentModelContext,
    ProjectConnection,
    ProjectCrossModelRecipe,
    ProjectSetting,
    QueryLog,
    QueryMissLog,
    RouteLog,
    UserAccessBinding,
)
from shared.artifact_target_binding import invalidate_artifacts_for_connection
from shared.model_snapshot.cascade_delete import delete_model_cascade
from shared.model_snapshot.importer import prepare_snapshot_for_import
from shared.importers.import_warnings import (
    ImportWarningResponse,
    make_import_warning,
    normalize_import_warnings,
)
from shared.db.model_write_lock_guard import model_write_lock_exempt
from shared.model_snapshot.rehydrator import (
    append_authentic_import_version,
    insert_model_versions,
    rehydrate_into_live,
)
from shared.model_snapshot.slug_utils import (
    insert_model_with_slug_retry,
    validate_bi_safe_slug,
)
from shared.semantic.graph_order import fact_anchor_violation
from shared.recipes.schema import CombineSchemaError, collect_combine_references
# Bug-8350 R2 MED-3 — reuse the canonical placeholder-secret validator so
# the import path's strength check cannot drift from the one dispatch time
# already applies (Bug-5951 / Bug-8349).
from shared.webhooks.agent_event_types import (
    InvalidAgentEventFilters,
    validate_agent_event_filters,
)
from shared.webhooks.dispatcher import is_valid_signing_secret

# Bug-8350-R2-adjacent (found while fixing the webhook-secret-import strength
# check below; filed separately -- see
# docs/execution/issue-intake/2026-07-28-project-rehydrator-undefined-logger.md):
# `logger` was referenced (`logger.warning(...)`) at four call sites in this
# module's secret-stripping paths with no module-level `logger` ever defined,
# so any import bundle that actually triggered `_find_sensitive_config_keys`
# (the exact security guard those call sites implement) would crash with
# NameError instead of stripping the leaked keys and logging a warning.
logger = logging.getLogger(__name__)

PROJECT_EXPORT_FORMAT = "tessallite-project/v1"

# Bug-8350 R2 MED-3 — minimum character length for an imported
# webhook_signing_secret, mirroring shared/config/settings.py's own
# JWT_SECRET_KEY startup check (>= 32 chars). The in-app generator
# (shared.webhooks.dispatcher.generate_signing_secret,
# secrets.token_urlsafe(32)) always produces ~43 characters, so this floor
# rejects anything drastically weaker without rejecting a legitimately
# generated secret.
_MIN_IMPORTED_WEBHOOK_SECRET_LEN = 32


def _imported_agent_context_derived_at(payload: dict[str, Any]) -> datetime | None:
    """Restore the prompt-visible derived-state marker without old timestamps.

    New bundles carry ``context_derived`` so a derived context with empty lists
    survives import as available context. Older bundles have no marker; infer
    the historical intent from non-empty derived fields and otherwise keep the
    row in the safe never-derived state.
    """
    marker = payload.get("context_derived")
    if marker is None:
        marker = any(
            payload.get(field)
            for field in (
                "aggregates_summary", "calendar_aliases", "dimension_aliases",
            )
        )
    else:
        # The exporter emits a JSON boolean. Treat malformed values (for
        # example the string ``"false"``) as not-derived rather than allowing
        # truthiness to upgrade a degraded imported context.
        marker = marker is True
    return datetime.now(timezone.utc) if marker else None

# Bundle FORMAT versions this importer understands. v1 = no per-version
# snapshots (every imported version honest-degrades). v2 (Bug-7623) = each
# version may carry its own portable ``snapshot_json`` so a restore reproduces
# that version's real shape. Both import; the per-version handling routes on
# ``_bundle_carries_version_snapshots`` below.
_SUPPORTED_BUNDLE_VERSIONS: frozenset[int] = frozenset({1, 2})
# Bundles at or above this version carry per-version ``snapshot_json`` payloads.
_MIN_VERSION_SNAPSHOT_BUNDLE_VERSION = 2


class ProjectImportError(ValueError):
    """Raised when the bundle cannot be imported."""


# judge_mode is a two-value enum enforced by the API (agent_config.py pattern
# ^(async|sync)$) and, since migration 0208, a DB CHECK
# (judge_mode IN ('sync','async')). Kept beside the other import-hardening
# helpers so every reader knows import routes untyped judge_mode through here.
_VALID_JUDGE_MODES = ("sync", "async")


def coerce_imported_judge_mode(value: Any) -> tuple[str, bool]:
    """Return ``(judge_mode, was_coerced)`` for an imported value.

    An imported bundle is untyped input. A value outside ``('sync','async')``
    would fail the migration-0208 CHECK with an opaque IntegrityError, so it is
    coerced to the safe validated-first default ``'sync'`` — exactly as the
    migration coerces legacy rows — and the caller warns the importing admin.
    A valid value passes through unchanged.
    """
    if value in _VALID_JUDGE_MODES:
        return value, False
    return "sync", True


def sanitise_imported_config(
    config: Any, *, label: str, name: Any = None
) -> dict[str, Any]:
    """Strip secret-like keys from a plaintext ``config`` bag arriving in a bundle.

    Every JSONB ``config`` column on this platform (``ProjectConnection.config``,
    ``LLMProviderConfig.config``) is stored in plaintext and echoed back to
    lower-privileged readers; secrets belong in the Fernet-encrypted column
    beside it. The API write paths enforce that with
    ``_validate_non_sensitive_config``, but import bypasses those schemas
    entirely, so a hand-crafted or legacy bundle is a way to reintroduce a
    plaintext secret into JSONB (Fable FINDING-2 / F-014-01, extended to LLM
    provider configs by Bug-8259).

    Sanitising rather than rejecting is deliberate: a whole project import must
    not fail because one stale bag carries a dead key. The strip is logged so an
    operator can see what was dropped.

    Every import site that writes one of those columns must route its bag
    through here — guarded by
    ``tests/unit/test_rehydrator_config_bag_sanitised.py``.
    """
    cfg = config or {}
    leaked = _find_sensitive_config_keys(cfg)
    if leaked:
        logger.warning(
            "Stripping secret-like keys from imported %s config: %s (%s)",
            label, leaked, name,
        )
        cfg = _strip_sensitive_keys(cfg)
    return cfg


def _bundle_carries_version_snapshots(bundle: dict[str, Any]) -> bool:
    """True when the bundle's FORMAT version is new enough to carry each model
    version's own portable ``snapshot_json`` (Bug-7623). Old (v1) bundles never
    do, so every imported version honest-degrades for them.
    """
    try:
        version = int(bundle.get("schema_version") or 0)
    except (TypeError, ValueError):
        return False
    return version >= _MIN_VERSION_SNAPSHOT_BUNDLE_VERSION


def _validate_bundle(bundle: dict[str, Any]) -> None:
    if bundle.get("export_format") != PROJECT_EXPORT_FORMAT:
        raise ProjectImportError(
            f"Unsupported export_format: {bundle.get('export_format')!r}"
        )
    if bundle.get("schema_version") not in _SUPPORTED_BUNDLE_VERSIONS:
        raise ProjectImportError(
            f"Unsupported schema_version: {bundle.get('schema_version')}"
        )
    for section_key in bundle.get("included_sections", []):
        val = bundle.get(section_key)
        if val is None:
            # Bug-6290 backwards compat: bundles exported before the fix
            # carry agent_config=None when no ProjectAgentConfig row
            # existed.  Treat that as "section absent" rather than
            # rejecting the bundle, so already-produced bundles re-import.
            if section_key == "agent_config":
                continue
            raise ProjectImportError(
                f"included_sections lists '{section_key}' but the field is "
                f"null/missing"
            )

    # Bug-6631: validate model id presence and uniqueness. A bundle whose
    # model snapshots lack an ``id`` (malformed export or hand-edited JSON)
    # would crash deep in the rehydrator with a cryptic KeyError. Duplicate
    # model ids (corrupt merge, copy-paste) would silently overwrite the
    # first model with the second during import.
    # The model id lives at ms["model"]["id"] (the nested model dict), not
    # at the snapshot top level.
    models = bundle.get("models") or []
    seen_model_ids: set[str] = set()
    for idx, ms in enumerate(models):
        model_dict = ms.get("model") or {}
        mid = model_dict.get("id")
        if not mid:
            raise ProjectImportError(
                f"Model snapshot at index {idx} is missing a required "
                f"'model.id' field"
            )
        mid_str = str(mid)
        if mid_str in seen_model_ids:
            raise ProjectImportError(
                f"Duplicate model id {mid_str!r} in bundle (index {idx})"
            )
        seen_model_ids.add(mid_str)

        # Bug-8614 / Bug-8134: enforce the deploy fact-anchor contract before
        # staging any project row (F-013-11's partial unique index still caps
        # explicitly declared facts).
        # The API create/update paths guard this with `_assert_at_most_one_
        # fact` (services/model-service/src/api/tables.py), but import
        # bypasses those schemas/endpoints entirely -- same class of gap
        # `sanitise_imported_config` documents for connection/LLM config
        # bags. Left unchecked, a two-fact-table model snapshot reaches
        # `_insert_tables_and_columns` (shared/model_snapshot/rehydrator.py),
        # whose invalid per-row INSERT trips the partial unique index
        # AFTER the first fact row (and every sibling row already inserted
        # in that loop) has been staged into this transaction: the caller
        # sees a raw IntegrityError instead of a clean, actionable error.
        # Checking here -- in `_validate_bundle`, which both `import_project`
        # and `plan_project_import` call before any row (or even the Project
        # row) is created -- catches it before a single row is staged.
        anchor_error = fact_anchor_violation(ms.get("tables") or [])
        if anchor_error:
            fact_names = [
                str(t.get("physical_name") or t.get("alias") or "?")
                for t in (ms.get("tables") or [])
            ]
            raise ProjectImportError(
                f"Model snapshot at index {idx} (id {mid_str!r}) violates "
                f"the fact-anchor contract: {anchor_error}"
            )


def _referenced_export_connection_ids(bundle: dict[str, Any]) -> set[str]:
    """Bug-7296: export-side connection ids referenced by any model's sources
    /targets.

    The ``project_connection_id`` on each data_source / data_target names an
    export-side connection id. A ``connection_mapping`` key is "consumed" only
    if it appears here — a key naming a connection no model references is a
    no-op the import should surface as a warning rather than silently ignore.
    """
    referenced: set[str] = set()
    for ms in bundle.get("models", []) or []:
        for section in ("data_sources", "data_targets"):
            for row in ms.get(section, []) or []:
                cid = row.get("project_connection_id")
                if cid:
                    referenced.add(str(cid))
    return referenced


def _post_import_actions_for_bundle(
    bundle: dict[str, Any],
    *,
    models_requiring_deploy: list[str] | None = None,
) -> list[str]:
    """Derive the post-import follow-up actions for a bundle.

    Shared by the real importer (which knows exactly which models need a
    redeploy) and the dry-run planner (which conservatively flags a redeploy
    whenever any model carries a deployed-version snapshot).
    """
    actions: list[str] = []
    if models_requiring_deploy is None:
        needs_deploy = any(
            ms.get("exported_deployed_version_id") for ms in bundle.get("models", [])
        )
    else:
        needs_deploy = bool(models_requiring_deploy)
    if needs_deploy:
        actions.append("redeploy_required")
    if any(
        ms.get("aggregates") or ms.get("pockets")
        for ms in bundle.get("models", [])
    ):
        actions.append("refresh_aggregates")
    return actions


async def _count_rows(
    tenant_db: AsyncSession, model_cls: Any, project_id: UUID
) -> int:
    result = await tenant_db.execute(
        select(func.count())
        .select_from(model_cls)
        .where(model_cls.project_id == project_id)
    )
    return int(result.scalar_one() or 0)


async def _count_rows_by_model(
    tenant_db: AsyncSession, model_cls: Any, model_id: UUID
) -> int:
    result = await tenant_db.execute(
        select(func.count())
        .select_from(model_cls)
        .where(model_cls.model_id == model_id)
    )
    return int(result.scalar_one() or 0)


async def _model_cascade_counts(
    tenant_db: AsyncSession, project_id: UUID
) -> dict[str, Any]:
    """Per-model cascade row volume that ``delete_model_cascade`` would remove.

    Bug-4263: ``delete_counts["models"]`` reports only how many model rows a
    replace deletes — it understates blast radius because each replaced model
    cascades away its aggregates, pockets and query/route logs. This counts the
    high-volume child tables per model so the dry-run plan shows the true
    deletion scope.

    Runs on the (non-hot) dry-run preview path only. ``route_logs`` has no
    ``model_id`` column; it is reached through ``query_logs`` exactly as the
    cascade SQL does, so its count mirrors what the delete removes.
    """
    rows = (await tenant_db.execute(
        select(Model.id, Model.slug, Model.display_name)
        .where(Model.project_id == project_id)
    )).all()

    per_model: list[dict[str, Any]] = []
    totals = {
        "aggregates": 0,
        "pockets": 0,
        "query_logs": 0,
        "query_miss_logs": 0,
        "route_logs": 0,
    }
    for model_id, slug, display_name in rows:
        aggregates = await _count_rows_by_model(
            tenant_db, AggregateDefinition, model_id
        )
        pockets = await _count_rows_by_model(
            tenant_db, PocketDefinition, model_id
        )
        query_logs = await _count_rows_by_model(tenant_db, QueryLog, model_id)
        query_miss_logs = await _count_rows_by_model(
            tenant_db, QueryMissLog, model_id
        )
        # route_logs reach a model via query_logs.id (no direct model_id),
        # matching the cascade-delete subquery.
        route_logs = int((await tenant_db.execute(
            select(func.count())
            .select_from(RouteLog)
            .where(
                RouteLog.query_log_id.in_(
                    select(QueryLog.id).where(QueryLog.model_id == model_id)
                )
            )
        )).scalar_one() or 0)

        counts = {
            "aggregates": aggregates,
            "pockets": pockets,
            "query_logs": query_logs,
            "query_miss_logs": query_miss_logs,
            "route_logs": route_logs,
        }
        for key, value in counts.items():
            totals[key] += value
        per_model.append({
            "model_id": str(model_id),
            "slug": slug,
            "display_name": display_name,
            "counts": counts,
        })

    return {"per_model": per_model, "totals": totals}


def _incoming_counts(bundle: dict[str, Any], included: set[str]) -> dict[str, int]:
    """Rows the bundle would create, gated by included_sections."""
    agent_config = bundle.get("agent_config") or {}
    return {
        "models": len(bundle.get("models", []) or []),
        "connections": (
            len(bundle.get("connections", []) or [])
            if "connections" in included
            else 0
        ),
        "llm_configs": (
            len(bundle.get("llm_configs", []) or [])
            if "llm_configs" in included
            else 0
        ),
        "project_settings": (
            len(bundle.get("project_settings", []) or [])
            if "project_settings" in included
            else 0
        ),
        "access_bindings": (
            len(bundle.get("access_bindings", []) or [])
            if "access_bindings" in included
            else 0
        ),
        "judge_rubrics": (
            len(agent_config.get("judge_rubrics", []) or [])
            if "agent_config" in included
            else 0
        ),
        "agent_model_links": (
            len(agent_config.get("models", []) or [])
            if "agent_config" in included
            else 0
        ),
        "agent_model_contexts": (
            len(agent_config.get("model_contexts", []) or [])
            if "agent_config" in included
            else 0
        ),
        "cross_model_recipes": (
            len(bundle.get("cross_model_recipes", []) or [])
            if "cross_model_recipes" in included
            else 0
        ),
    }


async def _replace_delete_counts(
    tenant_db: AsyncSession,
    project_id: UUID,
    included: set[str],
) -> dict[str, int]:
    """Count rows the replace mode would delete, honouring the section-scoped
    delete safety (F-020-05). Models are always replaced; every other entity
    is only deleted when its owning section is present in the bundle. The
    counts therefore mirror exactly what ``import_project`` would remove.
    """
    counts: dict[str, int] = {}
    # Models (and their cascade) are always deleted on replace.
    counts["models"] = await _count_rows(tenant_db, Model, project_id)

    if "agent_config" in included:
        counts["agent_conversations"] = await _count_rows(
            tenant_db, AgentConversation, project_id
        )
        counts["agent_webhook_dlq"] = await _count_rows(
            tenant_db, AgentWebhookDlq, project_id
        )
        counts["agent_model_contexts"] = await _count_rows(
            tenant_db, ProjectAgentModelContext, project_id
        )
        counts["agent_model_links"] = await _count_rows(
            tenant_db, ProjectAgentModel, project_id
        )
        counts["agent_configs"] = await _count_rows(
            tenant_db, ProjectAgentConfig, project_id
        )
        counts["judge_rubrics"] = await _count_rows(
            tenant_db, AgentJudgeRubric, project_id
        )

    if "cross_model_recipes" in included:
        counts["cross_model_recipes"] = await _count_rows(
            tenant_db, ProjectCrossModelRecipe, project_id
        )

    if "llm_configs" in included:
        counts["llm_configs"] = await _count_rows(
            tenant_db, LLMProviderConfig, project_id
        )

    if "project_settings" in included:
        counts["project_settings"] = await _count_rows(
            tenant_db, ProjectSetting, project_id
        )

    if "access_bindings" in included:
        counts["access_bindings"] = await _count_rows(
            tenant_db, UserAccessBinding, project_id
        )

    return counts


async def _connection_plan(
    bundle: dict[str, Any],
    tenant_db: AsyncSession,
    *,
    project_id: UUID | None,
    mode: str,
    included: set[str],
    connection_mapping: dict[str, str] | None,
    has_creds: bool,
) -> tuple[list[dict[str, Any]], list[ImportWarningResponse], int]:
    """Model how each exported connection resolves against the target, plus
    the count of pre-existing connections that would be pruned as orphans in
    replace mode. Mirrors step 4 / step 4b of ``import_project``.
    """
    if "connections" not in included:
        # Bug-7308: No connections section means "leave connections untouched"
        # — consistent with every other absent optional section.  No
        # connections are pruned regardless of mode.
        return [], [], 0

    export_conns = bundle.get("connections", []) or []
    existing_by_key: dict[tuple[str, str], ProjectConnection] = {}
    by_id: dict[str, ProjectConnection] = {}
    if project_id is not None:
        rows = await tenant_db.execute(
            select(ProjectConnection).where(
                ProjectConnection.project_id == project_id
            )
        )
        conns = list(rows.scalars().all())
        existing_by_key = {
            (c.display_name, c.connection_type): c for c in conns
        }
        by_id = {str(c.id): c for c in conns}

    warnings: list[ImportWarningResponse] = []
    actions: list[dict[str, Any]] = []
    retained_ids: set[str] = set()
    for ec in export_conns:
        export_id = str(ec["id"])
        mapped_id = (connection_mapping or {}).get(export_id)
        if has_creds:
            action = "create_from_credentials"
        elif mode == "create":
            action = "create_deferred_credentials"
        else:
            action = "requires_mapping"
        target_id: str | None = None

        if mapped_id:
            action = "use_mapping"
            target_id = mapped_id
            retained_ids.add(mapped_id)
        elif project_id is not None:
            matched = existing_by_key.get(
                (ec.get("display_name"), ec.get("connection_type"))
            )
            if matched:
                action = "auto_match"
                target_id = str(matched.id)
                retained_ids.add(target_id)

        if action == "create_deferred_credentials":
            connection_name = str(ec.get("display_name") or export_id)
            warnings.append(make_import_warning(
                code="project_import.connection_deferred",
                params={"connection": connection_name},
                detail=(
                    f"dry-run: connection '{connection_name}' will be created "
                    "with deferred credentials; configure real credentials "
                    f"before querying (export id {export_id})"
                ),
            ))
        elif action == "requires_mapping":
            connection_name = str(ec.get("display_name") or export_id)
            warnings.append(make_import_warning(
                code="project_import.connection_mapping_required",
                params={"connection": connection_name},
                detail=(
                    f"dry-run: connection '{connection_name}' has no target "
                    "match and the bundle carries no credentials; a "
                    f"connection_mapping entry is required for export id {export_id}"
                ),
            ))

        actions.append({
            "export_connection_id": export_id,
            "display_name": ec.get("display_name"),
            "connection_type": ec.get("connection_type"),
            "action": action,
            "target_connection_id": target_id,
        })

    orphan_count = 0
    if mode == "replace" and project_id is not None:
        orphan_count = len(set(by_id) - retained_ids)
    return actions, warnings, orphan_count


async def plan_project_import(
    bundle: dict[str, Any],
    tenant_db: AsyncSession,
    *,
    mode: str = "create",
    project_slug_override: str | None = None,
    project_display_name_override: str | None = None,
    model_slug_overrides: dict[str, str] | None = None,
    persona_slug_overrides: dict[str, str] | None = None,
    connection_mapping: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return the non-mutating import plan for dry-run callers (F-020-E3).

    Computes what a real ``import_project`` would delete and create without
    touching the tenant DB. Replace-mode deletion counts honour the
    section-scoped delete safety (F-020-05) and the deferred connection
    orphan-prune (F-020-01), so the plan reflects exactly what would happen.
    The caller is responsible for rolling back the (read-only) transaction.
    """
    _validate_bundle(bundle)
    _validate_slug_overrides(model_slug_overrides, persona_slug_overrides)

    included = set(bundle.get("included_sections", []))
    project_data = bundle["project"]
    target_slug = project_slug_override or project_data["slug"]
    target_display = project_display_name_override or project_data["display_name"]
    has_creds = bundle.get("credentials_included", False)

    project: Project | None = (
        await tenant_db.execute(
            select(Project).where(Project.slug == target_slug)
        )
    ).scalar_one_or_none()

    project_id: UUID | None = None
    delete_counts: dict[str, int] = {}
    # Bug-4263: cascade row volume the replace would delete per model.
    model_cascade_counts: dict[str, Any] = {"per_model": [], "totals": {}}

    if mode == "create":
        if project is not None:
            raise ProjectImportError(
                f"Project slug '{target_slug}' already exists "
                f"(use mode='replace' to overwrite)"
            )
    elif mode == "replace":
        if project is None:
            raise ProjectImportError(
                f"Project slug '{target_slug}' not found for replace"
            )
        project_id = project.id
        delete_counts = await _replace_delete_counts(
            tenant_db, project_id, included
        )
        model_cascade_counts = await _model_cascade_counts(
            tenant_db, project_id
        )
    else:
        raise ProjectImportError(f"Unknown import mode: {mode!r}")

    connection_actions, warnings, orphan_connection_count = await _connection_plan(
        bundle,
        tenant_db,
        project_id=project_id,
        mode=mode,
        included=included,
        connection_mapping=connection_mapping,
        has_creds=has_creds,
    )
    if mode == "replace":
        delete_counts["orphan_connections"] = orphan_connection_count

    model_slugs: list[str] = []
    for model_snap in bundle.get("models", []) or []:
        old_slug = model_snap.get("model", {}).get("slug", "")
        resolved = (model_slug_overrides or {}).get(old_slug, old_slug)
        # Bug-6291: validate resolved model slugs in the dry-run planner
        # so that the preview rejects BI-unsafe slugs rather than passing
        # a plan that the actual import would 422.
        try:
            validate_bi_safe_slug(resolved, label="Model slug")
        except ValueError as exc:
            raise ProjectImportError(str(exc)) from exc
        model_slugs.append(resolved)

    # Bug-5267: detect slug collisions in the dry-run planner too.
    _plan_seen: dict[str, int] = {}
    for _pi, _ps in enumerate(model_slugs):
        if _ps in _plan_seen:
            raise ProjectImportError(
                f"Model slug collision: models at positions "
                f"{_plan_seen[_ps]} and {_pi} both resolve to "
                f"slug '{_ps}'. Adjust model_slug_overrides to "
                f"avoid duplicates."
            )
        _plan_seen[_ps] = _pi

    # Bug-6291: persona slug override values are already validated by
    # _validate_slug_overrides at the top; here we validate persona
    # slugs already in the bundle content (not just overrides) so the
    # (not just overrides) so the dry-run cannot pass for a bundle
    # whose actual import would be rejected by _insert_personas.
    _overrides = persona_slug_overrides or {}
    for model_snap in bundle.get("models", []) or []:
        for p in model_snap.get("personas", []) or []:
            raw_slug = p.get("slug") or ""
            final_slug = _overrides.get(raw_slug, raw_slug)
            try:
                validate_bi_safe_slug(final_slug, label="Persona slug")
            except ValueError as exc:
                raise ProjectImportError(str(exc)) from exc
    if persona_slug_overrides:
        for old, new in persona_slug_overrides.items():
            try:
                validate_bi_safe_slug(
                    new,
                    label=f"Persona slug override '{old}' -> '{new}'",
                )
            except ValueError as exc:
                raise ProjectImportError(str(exc)) from exc

    return {
        "mode": mode,
        "target_project_id": str(project_id) if project_id else None,
        "target_project_slug": target_slug,
        "target_project_display_name": target_display,
        "target_project_exists": project is not None,
        "will_create_project": mode == "create",
        "will_replace_project": mode == "replace",
        "delete_counts": delete_counts,
        "model_cascade_counts": model_cascade_counts,
        "incoming_counts": _incoming_counts(bundle, included),
        "connection_actions": connection_actions,
        "model_slugs": model_slugs,
        "post_import_actions": _post_import_actions_for_bundle(bundle),
        "warnings": normalize_import_warnings(
            warnings, source="project_import"
        ),
    }


def _validate_slug_overrides(
    model_slug_overrides: dict[str, str] | None,
    persona_slug_overrides: dict[str, str] | None,
) -> None:
    """Bug-6291: preflight validation of ALL override values.

    Runs at the start of both the dry-run planner and the actual import
    so that every override value is validated even if it targets a
    model/persona not present in the bundle (unused overrides).
    """
    if model_slug_overrides:
        for old, new in model_slug_overrides.items():
            try:
                validate_bi_safe_slug(
                    new, label=f"Model slug override '{old}' -> '{new}'"
                )
            except ValueError as exc:
                raise ProjectImportError(str(exc)) from exc
    if persona_slug_overrides:
        for old, new in persona_slug_overrides.items():
            try:
                validate_bi_safe_slug(
                    new,
                    label=f"Persona slug override '{old}' -> '{new}'",
                )
            except ValueError as exc:
                raise ProjectImportError(str(exc)) from exc


def _prepare_cross_model_recipes_for_import(
    bundle: dict[str, Any],
    model_id_remap: dict[str, str],
) -> list[dict[str, Any]]:
    """Pure preflight: remap and validate every durable recipe reference."""
    definitions: dict[str, set[str]] = {}
    for model_snapshot in bundle.get("models", []) or []:
        model_data = model_snapshot.get("model") or {}
        old_model_id = str(model_data.get("id") or "")
        definitions[old_model_id] = {
            measure["name"]
            for measure in (model_snapshot.get("measures") or [])
            if isinstance(measure, dict)
            and isinstance(measure.get("name"), str)
            and measure["name"]
        }

    rewritten_recipes = deepcopy(bundle.get("cross_model_recipes", []) or [])
    for recipe_index, recipe in enumerate(rewritten_recipes):
        if not isinstance(recipe, dict):
            raise ProjectImportError(
                f"cross_model_recipe:index-{recipe_index}$ must be an object"
            )
        identity = str(
            recipe.get("id") or recipe.get("name") or f"index-{recipe_index}"
        )
        prefix = f"cross_model_recipe:{identity}"
        steps = recipe.get("steps")
        if not isinstance(steps, list):
            raise ProjectImportError(f"{prefix}$.steps must be a list")

        steps_by_name: dict[str, list[dict[str, Any]]] = {}
        step_name_indices: dict[str, int] = {}
        for step_index, step in enumerate(steps):
            step_path = f"$.steps[{step_index}]"
            if not isinstance(step, dict):
                raise ProjectImportError(f"{prefix}{step_path} must be an object")
            step_name = step.get("name")
            if not isinstance(step_name, str) or not step_name:
                raise ProjectImportError(
                    f"{prefix}{step_path}.name must be a non-empty string"
                )
            normalized_name = step_name.lower()
            if normalized_name in step_name_indices:
                raise ProjectImportError(
                    f"{prefix}{step_path}.name duplicates step name {step_name!r} "
                    f"at index {step_name_indices[normalized_name]}"
                )
            step_name_indices[normalized_name] = step_index
            old_model_id = step.get("model_id")
            if not isinstance(old_model_id, str) or not old_model_id:
                raise ProjectImportError(
                    f"{prefix}{step_path}.model_id must be a non-empty string"
                )
            new_model_id = model_id_remap.get(old_model_id)
            if new_model_id is None:
                raise ProjectImportError(
                    f"{prefix}{step_path}.model_id is unmapped: {old_model_id!r}"
                )
            measures = step.get("measures")
            if not isinstance(measures, list):
                raise ProjectImportError(
                    f"{prefix}{step_path}.measures must be a list"
                )
            for measure_index, measure_name in enumerate(measures):
                measure_path = f"{step_path}.measures[{measure_index}]"
                if not isinstance(measure_name, str) or not measure_name:
                    raise ProjectImportError(
                        f"{prefix}{measure_path} must be a non-empty string"
                    )
                if measure_name not in definitions.get(old_model_id, set()):
                    raise ProjectImportError(
                        f"{prefix}{measure_path} references missing measure "
                        f"{measure_name!r}"
                    )
            step["model_id"] = new_model_id
            steps_by_name.setdefault(step_name, []).append(step)

        combine = recipe.get("combine")
        if combine is None:
            continue
        try:
            references = collect_combine_references(combine)
        except CombineSchemaError as exc:
            raise ProjectImportError(f"{prefix}{exc.path}: {exc.detail}") from exc
        for reference in references:
            candidates = steps_by_name.get(reference.step, [])
            if len(candidates) != 1:
                raise ProjectImportError(
                    f"{prefix}{reference.path}.step must identify exactly one "
                    f"recipe step; got {reference.step!r}"
                )
            if reference.measure not in candidates[0]["measures"]:
                raise ProjectImportError(
                    f"{prefix}{reference.path}.measure references undeclared "
                    f"measure {reference.measure!r}"
                )
    return rewritten_recipes


async def _strip_blocked_import_host(
    connector, credential_bytes, config, display_name, warnings,
    *, reject_blocked: bool = False,
):
    """Apply the source-egress policy to a connection arriving via IMPORT.

    Bug-6216: ``connections._reject_blocked_source_host`` gates the create and
    update ENDPOINTS, but project import is a second, equally tenant-reachable
    way to persist a ProjectConnection -- the shared-primitive gap CLAUDE.md
    names. A bundle naming ``127.0.0.1`` or the metadata endpoint would
    otherwise land a row the API refuses to create, which the optimizer and the
    query path would then dial.

    R5 review finding 1: this was literal-only, which left import as the way in
    for the very host the connection endpoint had just been taught to refuse --
    ``127.0.0.1.nip.io`` and ``localtest.me`` resolve to loopback every time,
    pass a literal check, and the execution gate is literal too, so the socket
    opens. It resolves now, exactly like the write gate.

    The original objection ("an import must not fail because a host in someone
    else's bundle is unreachable from here") never needed the literal-only
    choice: this function STRIPS and warns rather than raising, so an
    unresolvable host is simply kept. Three outcomes, matching the write gate:
    resolves-to-blocked -> strip + warn; does not resolve -> keep; clean -> keep.
    The connection lands unusable-until-corrected rather than
    usable-and-dangerous.

    Bug-8837: existing-connection overrides use ``reject_blocked=True``. An
    override is an edit to a live connection, so it follows the ordinary
    connection PATCH contract and fails before issuing the SQL UPDATE instead
    of silently replacing the submitted endpoint. New/placeholder imports
    retain their established strip-and-warn behaviour.

    Returns the (possibly rewritten) credential bytes.
    """
    import json as _json

    from shared.schemas.connection_type import normalize_connection_type
    from shared.security.source_host_policy import (
        SourceHostBlockedError,
        assert_service_account_endpoints_allowed,
        assert_source_host_allowed,
        check_source_host_literal,
    )

    normalised = normalize_connection_type((connector or "").lower()) or ""

    creds = {}
    if credential_bytes:
        try:
            parsed = _json.loads(credential_bytes.decode("utf-8"))
            if isinstance(parsed, dict):
                creds = parsed
        except (ValueError, UnicodeDecodeError):
            creds = {}

    def _reject(reason: Exception) -> None:
        raise ProjectImportError(
            f"Connection '{display_name}' was rejected by the source egress "
            f"policy: {reason}"
        ) from reason

    # The normal connection write boundary treats the URLs inside a BigQuery
    # service-account blob as its endpoint-bearing fields. Imports must do the
    # same: BigQuery has no top-level ``host``, and google-auth POSTs to the
    # tenant-controlled token_uri on first use. Unlike a host key, those
    # nested fields cannot be stripped while leaving a usable credential, so
    # unsafe service-account JSON is rejected in both import modes.
    if normalised == "bigquery":
        sa_info = creds.get("service_account_json", creds)
        if isinstance(sa_info, str):
            try:
                sa_info = _json.loads(sa_info)
            except ValueError as exc:
                _reject(
                    ValueError(
                        f"The service-account key is not valid JSON: {exc}"
                    )
                )
        try:
            assert_service_account_endpoints_allowed(sa_info)
        except SourceHostBlockedError as exc:
            _reject(exc)
        return credential_bytes

    host = creds.get("host") or (config or {}).get("host")
    if not host:
        return credential_bytes

    def _strip(reason):
        if reject_blocked:
            _reject(reason)
        if config is not None:
            config.pop("host", None)
        connection_name = str(display_name or "unnamed")
        warnings.append(make_import_warning(
            code="project_import.connection_host_removed",
            params={"connection": connection_name},
            detail=(
                f"Connection '{connection_name}': the imported host was "
                f"removed because it is not an allowed source address ({reason}). "
                "Set a reachable host before using this connection."
            ),
        ))

    try:
        check_source_host_literal(str(host), connector=normalised)
    except SourceHostBlockedError as exc:
        _strip(exc)
        if creds.pop("host", None) is not None:
            return _json.dumps(creds).encode("utf-8")
        return credential_bytes

    # Then resolve, for the same reason the connection write gate does: a NAME
    # is judged here or nowhere, because the execution gate is literal-only.
    try:
        await assert_source_host_allowed(str(host), connector=normalised)
    except SourceHostBlockedError as exc:
        if "could not be resolved" in str(exc) or "did not resolve" in str(exc):
            logger.info(
                "Imported connection host %r does not resolve from this "
                "deployment; keeping it (the bundle may target another network).",
                host,
            )
            return credential_bytes
        _strip(exc)
        if creds.pop("host", None) is not None:
            return _json.dumps(creds).encode("utf-8")
    return credential_bytes


# Default ``actor`` for import_project when no human importer is threaded in
# (internal/seed contexts). The F-021-04 importer-admin-binding guarantee below
# is skipped for this sentinel — there is no human owner to grant admin to.
_IMPORT_ACTOR_DEFAULT = "project-import"


async def import_project(
    bundle: dict[str, Any],
    tenant_db: AsyncSession,
    *,
    mode: str = "create",
    project_slug_override: str | None = None,
    project_display_name_override: str | None = None,
    model_slug_overrides: dict[str, str] | None = None,
    persona_slug_overrides: dict[str, str] | None = None,
    connection_mapping: dict[str, str] | None = None,
    override_connections: bool = False,
    system_fernet: Fernet | None = None,
    passphrase_fernet: Fernet | None = None,
    actor: str = _IMPORT_ACTOR_DEFAULT,
) -> dict[str, Any]:
    """Import a ProjectBundle into the tenant.

    Returns an ImportProjectResponse-shaped dict. The caller is
    responsible for committing the session.
    """
    _validate_bundle(bundle)
    _validate_slug_overrides(model_slug_overrides, persona_slug_overrides)

    # Bug-7623: does this bundle's FORMAT version carry per-version snapshots?
    carries_version_snapshots = _bundle_carries_version_snapshots(bundle)

    has_creds = bundle.get("credentials_included", False)
    if has_creds and (not passphrase_fernet or not system_fernet):
        raise ProjectImportError(
            "Credentials in bundle but no passphrase/fernet provided"
        )

    warnings: list[ImportWarningResponse] = []
    included = set(bundle.get("included_sections", []))
    project_data = bundle["project"]
    target_slug = project_slug_override or project_data["slug"]
    target_display = (
        project_display_name_override or project_data["display_name"]
    )

    # Bug-8096: allocate every replacement model id and validate/remap recipe
    # JSON before create/replace performs its first database mutation. A bad
    # recipe bundle therefore cannot create a project or delete an existing one.
    model_id_remap = {
        str(model_snapshot["model"]["id"]): str(uuid.uuid4())
        for model_snapshot in (bundle.get("models") or [])
    }
    rewritten_cross_model_recipes = (
        _prepare_cross_model_recipes_for_import(bundle, model_id_remap)
        if "cross_model_recipes" in included
        else []
    )

    # ---------------------------------------------------------------
    # Step 3: Mode dispatch
    # ---------------------------------------------------------------
    if mode == "create":
        existing = (
            await tenant_db.execute(
                select(Project).where(Project.slug == target_slug)
            )
        ).scalar_one_or_none()
        if existing:
            raise ProjectImportError(
                f"Project slug '{target_slug}' already exists "
                f"(use mode='replace' to overwrite)"
            )
        project = Project(
            slug=target_slug,
            display_name=target_display,
            is_active=project_data.get("is_active", True),
        )
        tenant_db.add(project)
        await tenant_db.flush()
        project_id = project.id

    elif mode == "replace":
        project = (
            await tenant_db.execute(
                select(Project).where(Project.slug == target_slug)
            )
        ).scalar_one_or_none()
        if project is None:
            raise ProjectImportError(
                f"Project slug '{target_slug}' not found for replace"
            )
        project_id = project.id

        # ---------------------------------------------------------------
        # Step 3: Section-scoped deletion (F-020-05).
        # ---------------------------------------------------------------
        # Only delete what the bundle actually carries. The previous code
        # unconditionally wiped access bindings, agent conversations, agent
        # config, LLM configs, cross-model recipes, and settings on every
        # replace, regardless of included_sections — so the standard export
        # (which omits access_bindings) silently erased every project ACL,
        # leaving the project bootstrap-open, and destroyed conversation
        # history even when the bundle had no agent section. Each delete is
        # now guarded by the presence of its owning section.

        # Models are always exported and replaced (no toggleable section), so
        # the model set is always deleted. F-020-06: use the canonical
        # programmatic cascade (delete_model_cascade) instead of a raw
        # delete(Model) — raw deletion trips the
        # ai_optimizer_runs.telemetry_snapshot_id NO ACTION FK and stale
        # log-table constraints (Bug-411 class). The per-model cascade also
        # removes each model's DataSource/DataTarget rows that RESTRICT-FK to
        # connections, so connections become referenceless for step 4.
        # Sorted by the UUID's string form, byte-identically to
        # ``0194._lock_models`` and ``delete_project_cascade``:
        # ``delete_model_cascade`` acquires each model's advisory lock, so this
        # loop holds N of them until the import transaction ends and must agree
        # on acquisition order with every other multi-model locker.
        model_ids_q = await tenant_db.execute(
            select(Model.id).where(Model.project_id == project_id)
        )
        for mid in sorted((r[0] for r in model_ids_q.all()), key=str):
            errors = await delete_model_cascade(
                tenant_db,
                mid,
                fail_fast=True,
                cleanup_reason="project_replace",
            )
            if errors:
                raise ProjectImportError(
                    f"Replace failed deleting existing model {mid}: "
                    f"{'; '.join(errors)}"
                )

        if "agent_config" in included:
            # Agent conversations + history are part of the agent section.
            await tenant_db.execute(
                delete(AgentConversation).where(
                    AgentConversation.project_id == project_id
                )
            )
            await tenant_db.execute(
                delete(AgentWebhookDlq).where(
                    AgentWebhookDlq.project_id == project_id
                )
            )
            await tenant_db.execute(
                delete(ProjectAgentModelContext).where(
                    ProjectAgentModelContext.project_id == project_id
                )
            )
            await tenant_db.execute(
                delete(ProjectAgentModel).where(
                    ProjectAgentModel.project_id == project_id
                )
            )
            await tenant_db.execute(
                delete(ProjectAgentConfig).where(
                    ProjectAgentConfig.project_id == project_id
                )
            )
            await tenant_db.execute(
                delete(AgentJudgeRubric).where(
                    AgentJudgeRubric.project_id == project_id
                )
            )

        if "cross_model_recipes" in included:
            await tenant_db.execute(
                delete(ProjectCrossModelRecipe).where(
                    ProjectCrossModelRecipe.project_id == project_id
                )
            )

        if "llm_configs" in included:
            await tenant_db.execute(
                delete(LLMProviderConfig).where(
                    LLMProviderConfig.project_id == project_id
                )
            )

        # F-020-01: ProjectConnection deletion is DEFERRED to step 4b so the
        # "existing connections" lookup in step 4 can match/remap against live
        # rows before any orphans are pruned.

        if "project_settings" in included:
            await tenant_db.execute(
                delete(ProjectSetting).where(
                    ProjectSetting.project_id == project_id
                )
            )

        if "access_bindings" in included:
            await tenant_db.execute(
                delete(UserAccessBinding).where(
                    UserAccessBinding.project_id == project_id
                )
            )

        project.display_name = target_display
        project.is_active = project_data.get("is_active", True)
        await tenant_db.flush()
    else:
        raise ProjectImportError(f"Unknown import mode: {mode!r}")

    # ---------------------------------------------------------------
    # Step 4: Connections
    # ---------------------------------------------------------------
    conn_id_remap: dict[str, str] = {}
    # F-020-01: pre-existing target connection ids that mapping/auto-match
    # retains. In replace mode any pre-existing connection NOT in this set is
    # an orphan and is pruned at step 4b (after the connection delete was
    # deferred out of step 3).
    pre_existing_conn_ids: set[str] = set()
    retained_conn_ids: set[str] = set()
    # Bug-7296 (Fable R1 finding 3): export-side connection_mapping keys that
    # were actually consumed on the connections-present path (matched an export
    # connection and retained/overwrote a live target). A key here is NOT a
    # no-op even if no model references it, so it must not be flagged "unused".
    consumed_mapping_keys: set[str] = set()

    # F-020-11: validate caller-supplied connection_mapping target ids are
    # well-formed up front. The previous code accepted arbitrary strings into
    # conn_id_remap, surfacing a raw 500 IntegrityError at insert; a malformed
    # id now fails with a clear error. Ownership is checked below against the
    # live pre-existing connections of the target project.
    mapping_target_ids: set[str] = set()
    if connection_mapping:
        for raw_target in connection_mapping.values():
            try:
                mapping_target_ids.add(str(UUID(str(raw_target))))
            except (ValueError, AttributeError, TypeError):
                raise ProjectImportError(
                    f"connection_mapping target {raw_target!r} is not a valid "
                    f"connection id"
                )

    if "connections" in included:
        export_conns = bundle.get("connections", [])

        existing_conns_q = await tenant_db.execute(
            select(ProjectConnection).where(
                ProjectConnection.project_id == project_id
            )
        )
        existing_conns_all = existing_conns_q.scalars().all()
        existing_conns = {
            (c.display_name, c.connection_type): c
            for c in existing_conns_all
        }
        pre_existing_conn_ids = {str(c.id) for c in existing_conns_all}

        # F-020-11: every mapping target must belong to the target project
        # (matches the model-import endpoint's clean-400 behaviour). Checked
        # against the live pre-existing connections fetched above so a mapping
        # at a connection from another project (or a non-existent id) fails
        # loud instead of raising a raw FK IntegrityError on model insert.
        unknown = mapping_target_ids - pre_existing_conn_ids
        if unknown:
            raise ProjectImportError(
                "connection_mapping points at connection ids that do not "
                "belong to this project: " + ", ".join(sorted(unknown))
            )

        for ec in export_conns:
            export_id = ec["id"]

            if connection_mapping and export_id in connection_mapping:
                conn_id_remap[export_id] = connection_mapping[export_id]
                retained_conn_ids.add(connection_mapping[export_id])
                # Bug-7296 (Fable R1 finding 3): this mapping key matched an
                # exported connection and retained/overwrote a live target — it
                # is genuinely consumed even if no model references it, so it
                # must not be flagged "unused" below.
                consumed_mapping_keys.add(str(export_id))
                if (
                    override_connections
                    and has_creds
                    and "credentials" in ec
                ):
                    target_conn_id = UUID(connection_mapping[export_id])
                    plaintext = passphrase_fernet.decrypt(
                        base64.b64decode(ec["credentials"])
                    )
                    # Fable R2 FINDING-1: strip secrets from override config.
                    ov_config = sanitise_imported_config(
                        ec.get("config", {}),
                        label="connection override",
                        name=ec.get("display_name"),
                    )
                    # Bug-8837: explicit mapping is a connection write path.
                    # Refuse a blocked host/credential endpoint before the
                    # UPDATE can repoint the live connection.
                    plaintext = await _strip_blocked_import_host(
                        ec["connection_type"], plaintext, ov_config,
                        ec.get("display_name"), warnings,
                        reject_blocked=True,
                    )
                    await tenant_db.execute(
                        update(ProjectConnection)
                        .where(ProjectConnection.id == target_conn_id)
                        .values(
                            config=ov_config,
                            encrypted_credentials=system_fernet.encrypt(
                                plaintext
                            ),
                        )
                    )
                    # A connection override changes the database behind every
                    # aggregate or pocket bound to this connection. Invalidate
                    # those artifacts in the same transaction so rows built
                    # from the previous endpoint cannot remain routable.
                    await invalidate_artifacts_for_connection(
                        tenant_db,
                        target_conn_id,
                        reason="import_override_connections",
                    )
                continue

            match_key = (ec["display_name"], ec["connection_type"])
            matched = existing_conns.get(match_key)
            if matched:
                conn_id_remap[export_id] = str(matched.id)
                retained_conn_ids.add(str(matched.id))
                if (
                    override_connections
                    and has_creds
                    and "credentials" in ec
                ):
                    plaintext = passphrase_fernet.decrypt(
                        base64.b64decode(ec["credentials"])
                    )
                    # Fable R2 FINDING-1: strip secrets from auto-match config.
                    am_config = sanitise_imported_config(
                        ec.get("config", {}),
                        label="connection auto-match",
                        name=ec.get("display_name"),
                    )
                    # Bug-8837: name/type auto-match reaches the same live-row
                    # override and must cross the same egress-policy boundary.
                    plaintext = await _strip_blocked_import_host(
                        ec["connection_type"], plaintext, am_config,
                        ec.get("display_name"), warnings,
                        reject_blocked=True,
                    )
                    await tenant_db.execute(
                        update(ProjectConnection)
                        .where(ProjectConnection.id == matched.id)
                        .values(
                            config=am_config,
                            encrypted_credentials=system_fernet.encrypt(
                                plaintext
                            ),
                        )
                    )
                    await invalidate_artifacts_for_connection(
                        tenant_db,
                        matched.id,
                        reason="import_override_connections",
                    )
                continue

            if has_creds and "credentials" in ec:
                plaintext = passphrase_fernet.decrypt(
                    base64.b64decode(ec["credentials"])
                )
                # Fable FINDING-2 (F-014-01): strip secret-like keys from
                # imported config so a bundle cannot reintroduce plaintext
                # secrets into JSONB.
                import_config = sanitise_imported_config(
                    ec.get("config", {}),
                    label="connection",
                    name=ec.get("display_name"),
                )
                # Bug-6216: an imported bundle is tenant-supplied input just
                # like the connection form, so the same egress policy applies.
                # Without this, project import was a way to persist a
                # loopback/metadata source host that the create endpoint
                # refuses -- and the optimizer would then dial it.
                plaintext = await _strip_blocked_import_host(
                    ec["connection_type"], plaintext, import_config,
                    ec.get("display_name"), warnings,
                )
                new_conn = ProjectConnection(
                    project_id=project_id,
                    display_name=ec["display_name"],
                    connection_type=ec["connection_type"],
                    config=import_config,
                    encrypted_credentials=system_fernet.encrypt(plaintext),
                )
                tenant_db.add(new_conn)
                await tenant_db.flush()
                conn_id_remap[export_id] = str(new_conn.id)
            elif mode == "create":
                # Bug-5265: credentialless bundles (the default export) can
                # round-trip into a new project by creating placeholder
                # connections with no credentials. The admin must configure
                # real credentials before queries will work, but the import
                # itself succeeds and preserves the model topology.
                # Fable FINDING-2: strip secrets from credentialless import too.
                import_config_cl = sanitise_imported_config(
                    ec.get("config", {}),
                    label="credentialless connection",
                    name=ec.get("display_name"),
                )
                # Bug-6216: placeholder connections carry no credentials, but
                # the bundle's config can still name a host.
                await _strip_blocked_import_host(
                    ec["connection_type"], None, import_config_cl,
                    ec.get("display_name"), warnings,
                )
                new_conn = ProjectConnection(
                    project_id=project_id,
                    display_name=ec["display_name"],
                    connection_type=ec["connection_type"],
                    config=import_config_cl,
                    encrypted_credentials=b"",
                )
                tenant_db.add(new_conn)
                await tenant_db.flush()
                conn_id_remap[export_id] = str(new_conn.id)
                warnings.append(make_import_warning(
                    code="project_import.connection_deferred",
                    params={"connection": str(ec["display_name"])},
                    detail=(
                        f"Connection '{ec['display_name']}' "
                        f"({ec['connection_type']}) created with deferred "
                        "credentials; configure real credentials before "
                        f"querying (export id={export_id})."
                    ),
                ))
            else:
                raise ProjectImportError(
                    f"Connection '{ec['display_name']}' "
                    f"({ec['connection_type']}) has no match in target and "
                    f"bundle has no credentials to create it. Provide a "
                    f"connection_mapping entry for id={export_id}."
                )

    elif connection_mapping:
        # Bug-6296: the bundle omits a connections section (e.g. a model-only
        # or connections-excluded export), but the caller still supplied a
        # connection_mapping. The old code silently ignored it here, leaving
        # conn_id_remap empty — so every model DataSource that referenced an
        # exported connection id surfaced as an "unmapped connections" import
        # failure, rendering such bundles effectively unimportable. Apply the
        # mapping so models rehydrate onto the caller-nominated target
        # connections. Validate every target belongs to this project (same
        # clean-error contract as the connections-present path).
        existing_ids_q = await tenant_db.execute(
            select(ProjectConnection.id).where(
                ProjectConnection.project_id == project_id
            )
        )
        pre_existing_conn_ids = {str(cid) for cid in existing_ids_q.scalars().all()}
        unknown = mapping_target_ids - pre_existing_conn_ids
        if unknown:
            raise ProjectImportError(
                "connection_mapping points at connection ids that do not "
                "belong to this project: " + ", ".join(sorted(unknown))
            )
        for export_id, target_id in connection_mapping.items():
            conn_id_remap[export_id] = target_id
            retained_conn_ids.add(target_id)

    # ---------------------------------------------------------------
    # Step 4b: Prune orphan connections (replace mode only)
    # ---------------------------------------------------------------
    # F-020-01: the step-3 ProjectConnection delete was deferred so that
    # mapping/auto-match in step 4 could resolve against the live pre-existing
    # rows. Now delete only those pre-existing connections that were NOT
    # retained by a mapping or auto-match. The Model delete already cascaded
    # away every DataSource/DataTarget, so these orphans carry no incoming
    # FK references. New models bind to retained or freshly-created
    # connections via conn_id_remap.
    if mode == "replace":
        if "connections" in included:
            orphan_ids = pre_existing_conn_ids - retained_conn_ids
            if orphan_ids:
                await tenant_db.execute(
                    delete(ProjectConnection).where(
                        ProjectConnection.id.in_([UUID(o) for o in orphan_ids])
                    )
                )
                await tenant_db.flush()
        else:
            # Bug-7308: No connections section in the bundle. Treat absent
            # ``connections`` exactly like every other absent optional section
            # (settings, ACL, agent config): PRESERVE all existing target
            # connections.  The prior code did a clean-slate delete of all
            # connections minus retained ones, which was DATA-LOSS when the
            # administrator intentionally excluded connections from the export
            # (e.g. to avoid transporting topology).  Models are already
            # rebound via conn_id_remap if a connection_mapping was supplied;
            # the connections themselves stay untouched.
            pass

    # ---------------------------------------------------------------
    # Step 5a: LLM Configs (must precede Models so llm_config_id FK resolves)
    # ---------------------------------------------------------------
    llm_id_remap: dict[str, str] = {}

    if "llm_configs" in included:
        from shared.llm.sa_auth import (
            neutralise_service_account_config,
            service_account_auth_allowed,
            uses_service_account_auth,
        )

        for lc in bundle.get("llm_configs", []):
            old_id = lc["id"]
            new_id = uuid.uuid4()
            llm_id_remap[old_id] = str(new_id)
            # Bug-8259: the LLM provider config bag is the same plaintext-JSONB
            # class as the connection one, and import is the same bypass of the
            # schema-level gate, so it gets the same strip.
            lc_config = sanitise_imported_config(
                lc.get("config", {}),
                label="LLM provider",
                name=lc.get("display_name"),
            )
            # Anti-exploitation (Bug-5462): the import path bypasses the LLM-config
            # CRUD guard, so a bundle could carry a service-account/OAuth (Vertex
            # ADC) config that bills this deployment's cloud project. When SA auth
            # is not enabled, neutralise it on import (strip the vertex_ai keys) so
            # the row stays FK-valid but cannot activate cloud-credential billing.
            if uses_service_account_auth(lc["provider"], lc_config) and not service_account_auth_allowed():
                lc_config = neutralise_service_account_config(lc_config)
                logging.getLogger(__name__).warning(
                    "Imported LLM config %r uses service-account auth "
                    "(google_mode=vertex_ai) but LLM_ALLOW_SERVICE_ACCOUNT_AUTH is "
                    "off; neutralised to API-key mode on import (project=%s).",
                    lc.get("display_name"), project_id,
                )
            new_llm = LLMProviderConfig(
                id=new_id,
                project_id=project_id,
                provider=lc["provider"],
                display_name=lc["display_name"],
                base_url=lc.get("base_url"),
                model_name=lc["model_name"],
                max_tokens=lc.get("max_tokens", 4096),
                temperature=lc.get("temperature", 0.2),
                timeout_seconds=lc.get("timeout_seconds", 60),
                config=lc_config,
            )
            if (
                has_creds
                and "api_key" in lc
                and passphrase_fernet
                and system_fernet
            ):
                plaintext = passphrase_fernet.decrypt(
                    base64.b64decode(lc["api_key"])
                )
                new_llm.encrypted_api_key = system_fernet.encrypt(plaintext)
            tenant_db.add(new_llm)
        await tenant_db.flush()

    # ---------------------------------------------------------------
    # Step 5b: Models
    # ---------------------------------------------------------------
    # Bug-5267: pre-check for model slug collisions before any Model
    # rows are created. Without this, two models mapping to the same
    # slug surface as a raw IntegrityError on flush.
    _final_slugs: list[str] = []
    for _ms in bundle.get("models", []) or []:
        _old = _ms.get("model", {}).get("slug", "")
        _final_slugs.append(
            (model_slug_overrides or {}).get(_old, _old)
        )
    _seen_slugs: dict[str, int] = {}
    for _idx, _slug in enumerate(_final_slugs):
        if _slug in _seen_slugs:
            raise ProjectImportError(
                f"Model slug collision: models at positions "
                f"{_seen_slugs[_slug]} and {_idx} both resolve to "
                f"slug '{_slug}'. Adjust model_slug_overrides to "
                f"avoid duplicates."
            )
        _seen_slugs[_slug] = _idx

    models_requiring_deploy: list[str] = []
    persona_slug_map: dict[str, str] = {}

    # Bug-5725: collect existing slugs in this project so
    # insert_model_with_slug_retry can resolve collisions race-safely.
    existing_slugs_q = await tenant_db.execute(
        select(Model.slug).where(Model.project_id == project_id)
    )
    existing_slug_set: set[str] = {row[0] for row in existing_slugs_q.all()}

    for model_snap in bundle.get("models", []):
        snap_model = model_snap.get("model", {})
        old_model_id = snap_model.get("id", "")
        old_slug = snap_model.get("slug", "")
        new_slug = (model_slug_overrides or {}).get(old_slug, old_slug)

        new_model_id = UUID(model_id_remap[old_model_id])

        # Bug-7623 R2: the live shape and every version snapshot of THIS model
        # must re-key their PKs through ONE shared map so a source id maps to the
        # SAME new id everywhere. A revert to an imported version preserves live
        # governance and aggregates and re-attaches them BY ID; without shared
        # ids the re-attach would drop CLS tag links (fail-open), detach RLS
        # rules, replace KPI governance, and retire/duplicate aggregates/targets.
        model_pk_map: dict[str, str] = {}

        rewritten, missing = prepare_snapshot_for_import(
            model_snap,
            new_model_id=new_model_id,
            shared_pk_map=model_pk_map,
            connection_mapping=conn_id_remap,
        )
        if missing:
            raise ProjectImportError(
                f"Model '{old_slug}': unmapped connections: "
                f"{', '.join(missing)}"
            )

        rewritten.setdefault("model", {})
        if snap_model.get("display_name"):
            rewritten["model"]["display_name"] = snap_model["display_name"]

        # Bug-6291: apply persona slug overrides to the snapshot BEFORE
        # rehydration so that _insert_personas validates the final slug,
        # not the original.  This mirrors how model slug overrides are
        # applied before insert_model_with_slug_retry.
        if persona_slug_overrides:
            for p in rewritten.get("personas", []) or []:
                old_p_slug = p.get("slug", "")
                new_p_slug = persona_slug_overrides.get(old_p_slug)
                if new_p_slug:
                    persona_slug_map[old_p_slug] = new_p_slug
                    p["slug"] = new_p_slug

        # Bug-5725: use the race-safe slug retry utility (SAVEPOINT +
        # retry on unique violation) instead of a raw Model insert.
        # The returned model may have a different slug if a concurrent
        # import grabbed the candidate between our pre-check and insert.
        new_model, final_slug = await insert_model_with_slug_retry(
            tenant_db,
            project_id=project_id,
            base_slug=new_slug,
            existing_slugs=existing_slug_set,
            display_name=snap_model.get("display_name", new_slug),
            new_model_id=new_model_id,
        )
        new_slug = final_slug
        rewritten["model"]["slug"] = new_slug

        # Bug-7982 R7: DELIBERATE non-holder. This rehydrates into a model created in THIS transaction, so no other writer can reference it yet and there is nothing to serialise against. Declared explicitly so the runtime write guard does not report (and thereby drown out) a benign wholesale rebuild.
        async with model_write_lock_exempt(
            tenant_db, "import: wholesale rebuild into a model created in this transaction"
        ):
            await rehydrate_into_live(
                new_model_id,
                rewritten,
                tenant_db,
                drop_orphan_aggregates=False,
                actor=actor,
                connection_id_remap=conn_id_remap,
                llm_id_remap=llm_id_remap,
                force_aggregate_pending=True,
                force_pocket_stale=True,
                preserve_destination_seed=True,
            )

        if model_snap.get("model_versions"):
            # Bug-7623: on a NEW-format (v2+) bundle each version carries its own
            # snapshot; persist it (restorable) after rebinding portable refs via
            # the same connection remap used for the live shape. On an OLD-format
            # (v1) bundle no per-version snapshot exists, so every version
            # honest-degrades to a non-restorable placeholder (H2 preserved).
            await insert_model_versions(
                new_model_id, model_snap["model_versions"], tenant_db,
                bundle_carries_version_snapshots=carries_version_snapshots,
                connection_mapping=conn_id_remap,
                llm_id_remap=llm_id_remap,
                model_slug=new_slug,
                shared_pk_map=model_pk_map,
            )

        exported_dvid = model_snap.get("exported_deployed_version_id")
        if exported_dvid:
            models_requiring_deploy.append(new_slug)
            # Bug-6295: imported history rows are non-servable placeholders
            # (snapshot_unavailable). Deploy-latest resolves the highest
            # version_number, so without an authentic version above the
            # placeholders the model is undeployable (deploy validator rejects
            # the {} snapshot). Append one real, deployable version of the live
            # shape and point the deploy pointer at it so the redeploy the caller
            # triggers (and any bare deploy-latest) resolves a valid snapshot.
            authentic_vid = await append_authentic_import_version(
                new_model_id,
                tenant_db,
                summary=f"Imported {new_slug} (current shape)",
                created_by=actor or "import",
            )
            new_model.deployed_version_id = authentic_vid

    # Bug-7296: report connection_mapping keys that were never consumed. A
    # caller-supplied mapping is keyed by the EXPORT-side connection id. A key is
    # consumed if EITHER (a) it matched an exported connection on the
    # connections-present path and retained/overwrote a live target
    # (``consumed_mapping_keys``, protecting that connection from the replace
    # orphan-prune / driving a credential override), OR (b) some model's
    # data_sources/data_targets referenced it. A key that no model references AND
    # that the connections step did not retain is a genuine no-op — a stale or
    # typo'd entry that wastes the caller's effort with no signal (the
    # genuinely-unmapped model connections still fail with a clear per-model
    # error from prepare_snapshot_for_import above, so the import never crashes).
    # NOTE: on the connections-ABSENT path every mapping key is copied into
    # conn_id_remap speculatively for model rebinding, so conn_id_remap is NOT a
    # reliable "consumed" signal there — only model references count. Using the
    # precise ``consumed_mapping_keys`` (populated on the connections-present
    # retention only) plus model references avoids both a false positive on a
    # legitimately-retained project-level connection (Fable R1 finding 3) and a
    # false negative on a truly-unused key on the connections-absent path.
    if connection_mapping:
        referenced_export_ids = _referenced_export_connection_ids(bundle)
        consumed_keys = consumed_mapping_keys | referenced_export_ids
        unused_mapping_keys = [
            str(k) for k in connection_mapping
            if str(k) not in consumed_keys
        ]
        if unused_mapping_keys:
            warnings.append(make_import_warning(
                code="project_import.connection_mapping_unused",
                params={"count": len(unused_mapping_keys)},
                detail=(
                    "connection_mapping entries were not used by any imported "
                    "model and did not match any target connection (no data "
                    "source/target referenced these export connection ids, and "
                    "no connection was retained by them): "
                    + ", ".join(sorted(unused_mapping_keys))
                ),
            ))

    # ---------------------------------------------------------------
    # Step 6 (now 5a): LLM Configs moved before Models — see above.
    # ---------------------------------------------------------------

    # ---------------------------------------------------------------
    # Step 7: Project Settings
    # ---------------------------------------------------------------
    if "project_settings" in included:
        for ps in bundle.get("project_settings", []):
            tenant_db.add(
                ProjectSetting(
                    project_id=project_id,
                    key=ps["key"],
                    value_json=ps["value"],
                    updated_by=actor,
                )
            )
        await tenant_db.flush()

    # ---------------------------------------------------------------
    # Step 8: Agent Config
    # ---------------------------------------------------------------
    rubric_id_remap: dict[str, str] = {}

    # Bug-6290: skip creation when the shape is the empty placeholder
    # emitted by the serialiser for projects with no agent config.
    # Without this guard, every no-agent-config round-trip creates a
    # phantom ProjectAgentConfig row populated only with DB defaults.
    _ac_raw = bundle.get("agent_config") if "agent_config" in included else None
    _has_agent_content = bool(
        _ac_raw
        and (
            _ac_raw.get("config")
            or _ac_raw.get("models")
            or _ac_raw.get("model_contexts")
            or _ac_raw.get("judge_rubrics")
        )
    )
    if _has_agent_content:
        ac = _ac_raw

        # 8a: Judge rubrics
        for rub in ac.get("judge_rubrics", []):
            old_id = rub["id"]
            new_id = uuid.uuid4()
            rubric_id_remap[old_id] = str(new_id)
            tenant_db.add(
                AgentJudgeRubric(
                    id=new_id,
                    project_id=project_id,
                    name=rub["name"],
                    sections=rub.get("sections", []),
                )
            )
        await tenant_db.flush()

        # 8b: ProjectAgentConfig
        config_data = ac.get("config", {})
        pac_kwargs: dict[str, Any] = {"project_id": project_id}
        for field in (
            "enabled",
            "display_name",
            "project_brief",
            "agent_role",
            "tone_preset",
            "tone_overrides",
            "brand_guidelines",
            "safety_policy",
            "content_rules",
            "default_locale",
            "disclosure_text",
            "judge_mode",
            "judge_block_visibility",
            "show_thought_process",
            "show_semantic_query",
            "show_physical_query",
            "feedback_enabled",
            "conversation_retention_days",
            "webhook_url",
        ):
            if field in config_data:
                pac_kwargs[field] = config_data[field]

        # v5A#51 — judge_mode now carries a DB CHECK (migration 0208:
        # judge_mode IN ('sync','async')). A bundle is untyped input, so an
        # imported value outside that domain would fail the INSERT with an
        # opaque IntegrityError. Coerce an out-of-domain value to the safe
        # validated-first default ('sync') — exactly as migration 0208 coerces
        # legacy rows — and tell the importing admin, mirroring the
        # webhook_event_filters fallback below. A missing key keeps the column
        # server_default.
        if "judge_mode" in config_data:
            coerced, was_bad = coerce_imported_judge_mode(
                pac_kwargs.get("judge_mode")
            )
            pac_kwargs["judge_mode"] = coerced
            if was_bad:
                bad_judge_mode = config_data.get("judge_mode")
                logger.warning(
                    "Imported judge_mode=%r for project %s is not in "
                    "('sync','async') -- coercing to the validated-first "
                    "default 'sync'.",
                    bad_judge_mode, project_id,
                )
                warnings.append(make_import_warning(
                    code="project_import.judge_mode_reset",
                    params={"value": str(bad_judge_mode)},
                    detail=(
                        f"Agent judge mode {bad_judge_mode!r} was not recognised "
                        "and was set to 'sync' (validated-first). Review it in "
                        "project settings if you intended 'async'."
                    ),
                ))

        # Bug-8411 — the event subscription travels with the URL; without it
        # an imported project silently reverts to "all events". It is handled
        # separately from the plain copy loop above because a bundle is
        # untyped input and this column has a fail-OPEN reader: the Codex
        # cross-family gate showed that an imported JSON STRING (say
        # "turn.feedback", which reads as "only feedback please") is not a
        # list, so `agent_event_subscribed` fell through to its
        # deliver-everything branch and the receiver got every conversation
        # event the operator had deselected. Validate through the SAME shared
        # primitive the config API uses, and on rejection restore nothing and
        # tell the importing admin -- same channel and same posture as the
        # imported-signing-secret check below, because a server log is not an
        # admin-visible signal.
        if "webhook_event_filters" in config_data:
            try:
                pac_kwargs["webhook_event_filters"] = (
                    validate_agent_event_filters(
                        config_data["webhook_event_filters"]
                    )
                )
            except InvalidAgentEventFilters as exc:
                logger.warning(
                    "Imported webhook_event_filters for project %s rejected "
                    "(%s) -- leaving the subscription at its default rather "
                    "than restoring an unreadable value.",
                    project_id, exc,
                )
                warnings.append(make_import_warning(
                    code="project_import.webhook_subscription_reset",
                    params={},
                    detail=(
                        "Agent webhook event subscription was not restored "
                        f"({exc}). The webhook will send every agent event until "
                        "you set the subscription again in project settings."
                    ),
                ))

        old_primary = config_data.get("primary_model_id")
        if old_primary and old_primary in model_id_remap:
            pac_kwargs["primary_model_id"] = UUID(
                model_id_remap[old_primary]
            )
        old_answer_llm = config_data.get("answer_llm_config_id")
        if old_answer_llm and old_answer_llm in llm_id_remap:
            pac_kwargs["answer_llm_config_id"] = UUID(
                llm_id_remap[old_answer_llm]
            )
        old_judge_llm = config_data.get("judge_llm_config_id")
        if old_judge_llm and old_judge_llm in llm_id_remap:
            pac_kwargs["judge_llm_config_id"] = UUID(
                llm_id_remap[old_judge_llm]
            )
        old_aggregate_llm = config_data.get("aggregate_llm_config_id")
        if old_aggregate_llm and old_aggregate_llm in llm_id_remap:
            pac_kwargs["aggregate_llm_config_id"] = UUID(
                llm_id_remap[old_aggregate_llm]
            )
        old_glossary_llm = config_data.get("glossary_llm_config_id")
        if old_glossary_llm and old_glossary_llm in llm_id_remap:
            pac_kwargs["glossary_llm_config_id"] = UUID(
                llm_id_remap[old_glossary_llm]
            )
        old_rubric = config_data.get("judge_rubric_id")
        if old_rubric and old_rubric in rubric_id_remap:
            pac_kwargs["judge_rubric_id"] = UUID(
                rubric_id_remap[old_rubric]
            )

        if (
            has_creds
            and "webhook_signing_secret" in config_data
            and config_data["webhook_signing_secret"]
        ):
            plaintext = passphrase_fernet.decrypt(
                base64.b64decode(config_data["webhook_signing_secret"])
            )
            # Bug-8350 R2 MED-3 — project import previously restored a
            # decrypted webhook_signing_secret with NO strength validation,
            # falsifying this whole lane's fix's implicit assumption that a
            # weak/predictable secret is unreachable through the app: every
            # in-app path (auto-generation in agent_config.py,
            # rotate-secret) only ever produces one via
            # ``generate_signing_secret`` (``secrets.token_urlsafe(32)``,
            # ~43 chars of real entropy), but a hand-crafted or
            # externally-authored import bundle could carry
            # ``webhook_signing_secret: "abc123"`` and it would sail
            # straight through, undetected, into a live tenant. Apply the
            # same two checks dispatch time already applies (rejects
            # placeholders — Bug-5951's ``is_valid_signing_secret``) PLUS a
            # minimum-length floor mirroring ``JWT_SECRET_KEY``'s own
            # startup validation in ``shared/config/settings.py`` (>= 32
            # chars) — a placeholder-list check alone cannot catch an
            # arbitrary short string that isn't literally "changeme".
            # Never silently strengthen or reject the whole import over
            # this: drop the weak secret and leave the field unset, which
            # ``is_valid_signing_secret`` already fail-closes dispatch on
            # (Bug-8349) — the receiver was never going to be able to trust
            # a guessable secret anyway, and the modeller can rotate to a
            # real one from the UI.
            #
            # ``plaintext`` is BYTES (Fernet.decrypt's return type), but
            # ``is_valid_signing_secret`` compares against a frozenset of
            # ``str`` placeholders (``stripped.lower() in
            # _PLACEHOLDER_SECRETS``) — a bytes/str comparison never matches
            # regardless of content, so passing bytes straight through would
            # silently defeat the placeholder half of this check (every
            # bytes value would look "valid"). Decode explicitly for the
            # checks; a non-UTF-8 payload is itself not a usable HMAC
            # secret, so treat a decode failure the same as "too weak".
            try:
                plaintext_str = plaintext.decode("utf-8")
            except UnicodeDecodeError:
                plaintext_str = ""
            if is_valid_signing_secret(plaintext_str) and len(plaintext_str) >= _MIN_IMPORTED_WEBHOOK_SECRET_LEN:
                pac_kwargs["webhook_signing_secret"] = system_fernet.encrypt(
                    plaintext
                )
            else:
                logger.warning(
                    "Imported webhook_signing_secret for project %s failed "
                    "the minimum strength check (placeholder or under %d "
                    "characters) -- discarding it rather than restoring a "
                    "weak secret. The agent webhook stays refused-unsigned "
                    "until a modeller rotates the secret from the UI.",
                    project_id, _MIN_IMPORTED_WEBHOOK_SECRET_LEN,
                )
                # Bug-8350 R2 MED-3 follow-up (fresh-reviewer finding) — a
                # server log is not an admin-visible signal. webhook_url IS
                # restored a few lines above (it is not gated on this
                # check), so post-import the project looks fully
                # configured while every agent event is silently refused
                # and parked in the DLQ. import_project already returns a
                # `warnings` list used for less consequential situations
                # (a skipped access binding, a deferred connection
                # credential) — use the same channel here so the importing
                # admin actually sees it in the API response.
                warnings.append(make_import_warning(
                    code="project_import.webhook_secret_not_restored",
                    params={},
                    detail=(
                        "Agent webhook signing secret was not restored: the "
                        "imported secret is a placeholder or shorter than "
                        f"{_MIN_IMPORTED_WEBHOOK_SECRET_LEN} characters. Agent "
                        "webhooks will be refused (not sent) until you rotate "
                        "the secret and re-configure the receiver."
                    ),
                ))

        tenant_db.add(ProjectAgentConfig(**pac_kwargs))
        await tenant_db.flush()

        # 8c: ProjectAgentModel rows
        for am in ac.get("models", []):
            old_mid = am["model_id"]
            new_mid = model_id_remap.get(old_mid)
            if new_mid:
                tenant_db.add(
                    ProjectAgentModel(
                        project_id=project_id, model_id=UUID(new_mid)
                    )
                )
        await tenant_db.flush()

        # 8d: ProjectAgentModelContext rows
        for amc in ac.get("model_contexts", []):
            old_mid = amc.get("model_id", "")
            new_mid = model_id_remap.get(str(old_mid))
            if new_mid:
                tenant_db.add(
                    ProjectAgentModelContext(
                        project_id=project_id,
                        model_id=UUID(new_mid),
                        model_overview=amc.get("model_overview"),
                        analytical_capabilities=amc.get(
                            "analytical_capabilities"
                        ),
                        abbreviation_conflict_rules=amc.get(
                            "abbreviation_conflict_rules"
                        ),
                        example_questions=amc.get("example_questions", []),
                        aggregates_summary=amc.get("aggregates_summary", []),
                        calendar_aliases=amc.get("calendar_aliases", []),
                        dimension_aliases=amc.get("dimension_aliases", []),
                        derived_at=_imported_agent_context_derived_at(amc),
                    )
                )
        await tenant_db.flush()

    # ---------------------------------------------------------------
    # Step 9: Cross-model recipes
    # ---------------------------------------------------------------
    if "cross_model_recipes" in included:
        for rec in rewritten_cross_model_recipes:
            tenant_db.add(
                ProjectCrossModelRecipe(
                    project_id=project_id,
                    name=rec["name"],
                    description=rec.get("description"),
                    parameters=rec.get("parameters", []),
                    steps=rec.get("steps", []),
                    combine=rec.get("combine"),  # ExprNode tree or None (Bug-5346)
                    notes=rec.get("notes"),
                )
            )
        await tenant_db.flush()

    # ---------------------------------------------------------------
    # Step 10: Access bindings
    # ---------------------------------------------------------------
    if "access_bindings" in included:
        model_slug_to_new_id: dict[str, UUID] = {}
        for model_snap in bundle.get("models", []):
            old_slug = model_snap.get("model", {}).get("slug", "")
            new_slug = (model_slug_overrides or {}).get(old_slug, old_slug)
            old_id = model_snap.get("model", {}).get("id", "")
            new_id = model_id_remap.get(old_id)
            if new_id:
                model_slug_to_new_id[old_slug] = UUID(new_id)
                if new_slug != old_slug:
                    model_slug_to_new_id[new_slug] = UUID(new_id)

        local_users_q = await tenant_db.execute(select(LocalUser.email))
        known_emails = {row[0] for row in local_users_q.all()}

        for ab in bundle.get("access_bindings", []):
            user_id = ab["user_identity"]
            if user_id not in known_emails:
                warnings.append(make_import_warning(
                    code="project_import.access_binding_user_missing",
                    params={"user": str(user_id)},
                    detail=(
                        f"access_binding skipped: user '{user_id}' not found "
                        "in target tenant"
                    ),
                ))
                continue
            model_id_val: Optional[UUID] = None
            if ab.get("model_slug"):
                model_id_val = model_slug_to_new_id.get(ab["model_slug"])
                if model_id_val is None:
                    warnings.append(make_import_warning(
                        code="project_import.access_binding_model_missing",
                        params={"model": str(ab["model_slug"])},
                        detail=(
                            "access_binding skipped: model_slug "
                            f"'{ab['model_slug']}' not found"
                        ),
                    ))
                    continue
            tenant_db.add(
                UserAccessBinding(
                    user_identity=canonical_user_identity(user_id),
                    role=ab["role"],
                    project_id=project_id,
                    model_id=model_id_val,
                    # Bug-6599: preserve provenance across export/import.
                    # Legacy bundles (pre-Bug-6303) carry no "source"; default
                    # to "manual" so no imported grant is ever treated as
                    # SSO-managed (and thus auto-revocable) without evidence.
                    source=ab.get("source", "manual"),
                )
            )
        await tenant_db.flush()

    # ---------------------------------------------------------------
    # Step 10b: Guarantee the importing user an admin binding (F-021-04, F2b)
    # ---------------------------------------------------------------
    # Mirror create_project's ATOMIC creator-admin binding. Under the F-021-04
    # hard cutover a project with no binding for a caller denies them, and a
    # bootstrap-era export can carry ZERO access_bindings — so restoring only the
    # bundle's bindings above (Step 10, skipped entirely when the bundle omits the
    # section) would import a project that locks out every ordinary user AND
    # leaves the importer no admin binding to run the repair op from. This makes
    # import a project-CREATING path that, like create_project, never yields a
    # project the importer cannot govern. Idempotent: skip if the bundle already
    # granted the importer project-scoped admin; upgrade a lower project-scoped
    # role to admin; otherwise create the admin binding. Runs regardless of
    # whether "access_bindings" was in the bundle. Skipped for the non-human
    # default actor (internal/seed contexts have no human owner to guarantee) AND
    # for any 'service:<principal>' actor (F1): the route gate is human-only, but
    # this is defence in depth — a service identity must never receive a persisted
    # admin binding. Such a junk binding both violates the human-only contract and
    # permanently defeats the binding-less repair op (which 409s when ANY binding
    # exists). Embed actors carry no reserved prefix and cannot be detected here;
    # they are rejected at the route gate (require_tenant_admin / forbid_embed_user).
    if actor and actor != _IMPORT_ACTOR_DEFAULT and not is_service_identity(actor):
        importer_identity = canonical_user_identity(actor)
        existing_importer_binding = (
            await tenant_db.execute(
                select(UserAccessBinding).where(
                    user_identity_matches(
                        UserAccessBinding.user_identity, importer_identity
                    ),
                    UserAccessBinding.project_id == project_id,
                    UserAccessBinding.model_id.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing_importer_binding is None:
            tenant_db.add(
                UserAccessBinding(
                    user_identity=importer_identity,
                    role="admin",
                    project_id=project_id,
                    model_id=None,
                    source="manual",
                )
            )
        elif existing_importer_binding.role != "admin":
            existing_importer_binding.role = "admin"
        await tenant_db.flush()

    # ---------------------------------------------------------------
    # Build response
    # ---------------------------------------------------------------
    post_import_actions = _post_import_actions_for_bundle(
        bundle, models_requiring_deploy=models_requiring_deploy
    )

    return {
        "project_id": str(project_id),
        "project_slug": target_slug,
        "id_map": {
            "models": model_id_remap,
            "connections": conn_id_remap,
            "personas": persona_slug_map,
            "llm_configs": llm_id_remap,
            "judge_rubrics": rubric_id_remap,
        },
        "models_imported": len(bundle.get("models", [])),
        "models_requiring_deploy": models_requiring_deploy,
        "post_import_actions": post_import_actions,
        "warnings": normalize_import_warnings(
            warnings, source="project_import"
        ),
    }
