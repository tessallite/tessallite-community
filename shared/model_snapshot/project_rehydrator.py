"""Project-level import rehydrator.

Imports a ProjectBundle into the target tenant, creating or replacing a
project with all its children. Runs inside a single DB transaction --
the caller's session must NOT commit until this function returns.

See docs/architecture/architecture_project-import-export.md for the
full import flow specification.
"""
from __future__ import annotations

import base64
import uuid
from typing import Any, Optional
from uuid import UUID

from cryptography.fernet import Fernet
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    AgentConversation,
    AgentJudgeRubric,
    AgentWebhookDlq,
    LLMProviderConfig,
    LocalUser,
    Model,
    Persona,
    Project,
    ProjectAgentConfig,
    ProjectAgentModel,
    ProjectAgentModelContext,
    ProjectConnection,
    ProjectCrossModelRecipe,
    ProjectSetting,
    UserAccessBinding,
)
from shared.model_snapshot.cascade_delete import delete_model_cascade
from shared.model_snapshot.importer import prepare_snapshot_for_import
from shared.model_snapshot.rehydrator import (
    insert_model_versions,
    rehydrate_into_live,
)

PROJECT_EXPORT_FORMAT = "tessallite-project/v1"


class ProjectImportError(ValueError):
    """Raised when the bundle cannot be imported."""


def _validate_bundle(bundle: dict[str, Any]) -> None:
    if bundle.get("export_format") != PROJECT_EXPORT_FORMAT:
        raise ProjectImportError(
            f"Unsupported export_format: {bundle.get('export_format')!r}"
        )
    if bundle.get("schema_version") != 1:
        raise ProjectImportError(
            f"Unsupported schema_version: {bundle.get('schema_version')}"
        )
    for section_key in bundle.get("included_sections", []):
        val = bundle.get(section_key)
        if val is None:
            raise ProjectImportError(
                f"included_sections lists '{section_key}' but the field is "
                f"null/missing"
            )


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
) -> tuple[list[dict[str, Any]], list[str], int]:
    """Model how each exported connection resolves against the target, plus
    the count of pre-existing connections that would be pruned as orphans in
    replace mode. Mirrors step 4 / step 4b of ``import_project``.
    """
    if "connections" not in included:
        # No connections section: replace mode does a clean-slate delete of
        # every pre-existing connection (step 4b else-branch).
        orphan_count = 0
        if mode == "replace" and project_id is not None:
            orphan_count = await _count_rows(
                tenant_db, ProjectConnection, project_id
            )
        return [], [], orphan_count

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

    warnings: list[str] = []
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
            warnings.append(
                "dry-run: connection "
                f"'{ec.get('display_name')}' will be created with deferred "
                "credentials; configure real credentials before querying "
                f"(export id {export_id})"
            )
        elif action == "requires_mapping":
            warnings.append(
                "dry-run: connection "
                f"'{ec.get('display_name')}' has no target match and the "
                "bundle carries no credentials; a connection_mapping entry "
                f"is required for export id {export_id}"
            )

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
        model_slugs.append((model_slug_overrides or {}).get(old_slug, old_slug))

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

    return {
        "mode": mode,
        "target_project_id": str(project_id) if project_id else None,
        "target_project_slug": target_slug,
        "target_project_display_name": target_display,
        "target_project_exists": project is not None,
        "will_create_project": mode == "create",
        "will_replace_project": mode == "replace",
        "delete_counts": delete_counts,
        "incoming_counts": _incoming_counts(bundle, included),
        "connection_actions": connection_actions,
        "model_slugs": model_slugs,
        "post_import_actions": _post_import_actions_for_bundle(bundle),
        "warnings": warnings,
    }


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
    actor: str = "project-import",
) -> dict[str, Any]:
    """Import a ProjectBundle into the tenant.

    Returns an ImportProjectResponse-shaped dict. The caller is
    responsible for committing the session.
    """
    _validate_bundle(bundle)

    has_creds = bundle.get("credentials_included", False)
    if has_creds and (not passphrase_fernet or not system_fernet):
        raise ProjectImportError(
            "Credentials in bundle but no passphrase/fernet provided"
        )

    warnings: list[str] = []
    included = set(bundle.get("included_sections", []))
    project_data = bundle["project"]
    target_slug = project_slug_override or project_data["slug"]
    target_display = (
        project_display_name_override or project_data["display_name"]
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
        model_ids_q = await tenant_db.execute(
            select(Model.id).where(Model.project_id == project_id)
        )
        for (mid,) in model_ids_q.all():
            errors = await delete_model_cascade(tenant_db, mid, fail_fast=True)
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
                if (
                    override_connections
                    and has_creds
                    and "credentials" in ec
                ):
                    target_conn_id = UUID(connection_mapping[export_id])
                    plaintext = passphrase_fernet.decrypt(
                        base64.b64decode(ec["credentials"])
                    )
                    await tenant_db.execute(
                        update(ProjectConnection)
                        .where(ProjectConnection.id == target_conn_id)
                        .values(
                            config=ec.get("config", {}),
                            encrypted_credentials=system_fernet.encrypt(
                                plaintext
                            ),
                        )
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
                    await tenant_db.execute(
                        update(ProjectConnection)
                        .where(ProjectConnection.id == matched.id)
                        .values(
                            config=ec.get("config", {}),
                            encrypted_credentials=system_fernet.encrypt(
                                plaintext
                            ),
                        )
                    )
                continue

            if has_creds and "credentials" in ec:
                plaintext = passphrase_fernet.decrypt(
                    base64.b64decode(ec["credentials"])
                )
                new_conn = ProjectConnection(
                    project_id=project_id,
                    display_name=ec["display_name"],
                    connection_type=ec["connection_type"],
                    config=ec.get("config", {}),
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
                new_conn = ProjectConnection(
                    project_id=project_id,
                    display_name=ec["display_name"],
                    connection_type=ec["connection_type"],
                    config=ec.get("config", {}),
                    encrypted_credentials=b"",
                )
                tenant_db.add(new_conn)
                await tenant_db.flush()
                conn_id_remap[export_id] = str(new_conn.id)
                warnings.append(
                    f"Connection '{ec['display_name']}' "
                    f"({ec['connection_type']}) created with deferred "
                    f"credentials; configure real credentials before "
                    f"querying (export id={export_id})."
                )
            else:
                raise ProjectImportError(
                    f"Connection '{ec['display_name']}' "
                    f"({ec['connection_type']}) has no match in target and "
                    f"bundle has no credentials to create it. Provide a "
                    f"connection_mapping entry for id={export_id}."
                )

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
            # No connections in the bundle: nothing remaps to a pre-existing
            # connection, so replicate the original clean-slate delete.
            await tenant_db.execute(
                delete(ProjectConnection).where(
                    ProjectConnection.project_id == project_id
                )
            )
            await tenant_db.flush()

    # ---------------------------------------------------------------
    # Step 5a: LLM Configs (must precede Models so llm_config_id FK resolves)
    # ---------------------------------------------------------------
    llm_id_remap: dict[str, str] = {}

    if "llm_configs" in included:
        for lc in bundle.get("llm_configs", []):
            old_id = lc["id"]
            new_id = uuid.uuid4()
            llm_id_remap[old_id] = str(new_id)
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
                config=lc.get("config", {}),
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

    model_id_remap: dict[str, str] = {}
    models_requiring_deploy: list[str] = []
    persona_slug_map: dict[str, str] = {}

    for model_snap in bundle.get("models", []):
        snap_model = model_snap.get("model", {})
        old_model_id = snap_model.get("id", "")
        old_slug = snap_model.get("slug", "")
        new_slug = (model_slug_overrides or {}).get(old_slug, old_slug)

        new_model_id = uuid.uuid4()
        model_id_remap[old_model_id] = str(new_model_id)

        rewritten, missing = prepare_snapshot_for_import(
            model_snap,
            new_model_id=new_model_id,
            connection_mapping=conn_id_remap,
        )
        if missing:
            raise ProjectImportError(
                f"Model '{old_slug}': unmapped connections: "
                f"{', '.join(missing)}"
            )

        rewritten.setdefault("model", {})
        rewritten["model"]["slug"] = new_slug
        if snap_model.get("display_name"):
            rewritten["model"]["display_name"] = snap_model["display_name"]

        new_model = Model(
            id=new_model_id,
            project_id=project_id,
            slug=new_slug,
            display_name=snap_model.get("display_name", new_slug),
            seed=str(uuid.uuid4()),
        )
        tenant_db.add(new_model)
        await tenant_db.flush()

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
            await insert_model_versions(
                new_model_id, model_snap["model_versions"], tenant_db
            )

        exported_dvid = model_snap.get("exported_deployed_version_id")
        if exported_dvid:
            models_requiring_deploy.append(new_slug)

        if persona_slug_overrides:
            personas_q = await tenant_db.execute(
                select(Persona).where(Persona.model_id == new_model_id)
            )
            for p in personas_q.scalars().all():
                new_persona_slug = persona_slug_overrides.get(p.slug)
                if new_persona_slug:
                    persona_slug_map[p.slug] = new_persona_slug
                    p.slug = new_persona_slug

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

    if "agent_config" in included and bundle.get("agent_config"):
        ac = bundle["agent_config"]

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
            pac_kwargs["webhook_signing_secret"] = system_fernet.encrypt(
                plaintext
            )

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
                    )
                )
        await tenant_db.flush()

    # ---------------------------------------------------------------
    # Step 9: Cross-model recipes
    # ---------------------------------------------------------------
    if "cross_model_recipes" in included:
        for rec in bundle.get("cross_model_recipes", []):
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
                warnings.append(
                    f"access_binding skipped: user '{user_id}' not found "
                    f"in target tenant"
                )
                continue
            model_id_val: Optional[UUID] = None
            if ab.get("model_slug"):
                model_id_val = model_slug_to_new_id.get(ab["model_slug"])
                if model_id_val is None:
                    warnings.append(
                        f"access_binding skipped: model_slug "
                        f"'{ab['model_slug']}' not found"
                    )
                    continue
            tenant_db.add(
                UserAccessBinding(
                    user_identity=user_id,
                    role=ab["role"],
                    project_id=project_id,
                    model_id=model_id_val,
                )
            )
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
        "warnings": warnings,
    }
