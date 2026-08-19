"""Project-level export serialiser.

Walks every project child table and calls snapshot_model() per model.
Produces a ProjectBundle dict matching the spec in
docs/architecture/architecture_project-import-export.md.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    AgentJudgeRubric,
    LLMProviderConfig,
    Model,
    Project,
    ProjectAgentConfig,
    ProjectAgentModel,
    ProjectAgentModelContext,
    ProjectConnection,
    ProjectCrossModelRecipe,
    ProjectSetting,
    UserAccessBinding,
)
from shared.model_snapshot.serialiser import _j, _row_to_dict, snapshot_model
from shared.model_snapshot.project_rehydrator import sanitise_imported_config

# Bundle FORMAT version. Bumped 1 -> 2 (Bug-7623): a v2 bundle carries each
# model version's OWN portable ``snapshot_json`` (see snapshot_model's
# ``include_versions`` branch), so a restore can reproduce version N's real
# shape. v1 bundles carry no per-version snapshots — the importer degrades
# every version for them (honest, non-restorable). The importer accepts both
# versions and routes on this number; the ``export_format`` family string is
# unchanged so the two are one continuous format line, not a hard break.
PROJECT_BUNDLE_VERSION = 2
PROJECT_EXPORT_FORMAT = "tessallite-project/v1"

_AGENT_CONFIG_FIELDS = (
    "enabled", "display_name", "project_brief", "agent_role",
    "tone_preset", "tone_overrides", "brand_guidelines", "safety_policy",
    "content_rules", "default_locale", "disclosure_text",
    "judge_mode", "judge_block_visibility",
    "show_thought_process", "show_semantic_query", "show_physical_query",
    "feedback_enabled", "conversation_retention_days",
    # Bug-8411 — the webhook event subscription travels with the project;
    # restoring webhook_url without it would silently re-subscribe the
    # receiver to every agent event.
    "webhook_url", "webhook_event_filters",
    "primary_model_id", "answer_llm_config_id",
    "judge_llm_config_id", "judge_rubric_id",
    "aggregate_llm_config_id", "glossary_llm_config_id",
)


async def export_project(
    project_id: UUID,
    tenant_db: AsyncSession,
    *,
    tenant_slug: str,
    sections: set[str],
    include_credentials: bool = False,
    system_fernet: Fernet | None = None,
    passphrase_fernet: Fernet | None = None,
) -> dict[str, Any]:
    """Build a ProjectBundle dict for the given project.

    ``sections`` controls which optional parts are included. Models are
    always included.
    """
    project = await tenant_db.get(Project, project_id)
    if project is None:
        raise ValueError(f"Project {project_id} not found")

    bundle: dict[str, Any] = {
        "schema_version": PROJECT_BUNDLE_VERSION,
        "export_format": PROJECT_EXPORT_FORMAT,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "exported_from": {
            "tenant_slug": tenant_slug,
            "project_id": str(project_id),
        },
        "credentials_included": include_credentials,
        "credentials_envelope": None,
        "included_sections": sorted(sections),
        "project": {
            "slug": project.slug,
            "display_name": project.display_name,
            "is_active": project.is_active,
        },
    }

    if "connections" in sections:
        conns_q = await tenant_db.execute(
            select(ProjectConnection)
            .where(ProjectConnection.project_id == project_id)
            .order_by(ProjectConnection.created_at)
        )
        conns: list[dict[str, Any]] = []
        for c in conns_q.scalars().all():
            entry: dict[str, Any] = {
                "id": _j(c.id),
                "display_name": c.display_name,
                "connection_type": c.connection_type,
                # F-020-10 (Bug-9080): the plaintext JSONB config bag is echoed
                # to lower-privileged readers of the bundle; secrets belong in
                # the Fernet-encrypted column, never here. Import already strips
                # secret-like keys (sanitise_imported_config) — apply the SAME
                # strip on export so a bundle exported WITHOUT credentials cannot
                # still carry a plaintext "password"/"token" in config.
                "config": sanitise_imported_config(
                    c.config, label="connection", name=c.display_name,
                ),
            }
            if (
                include_credentials
                and system_fernet
                and passphrase_fernet
                and c.encrypted_credentials
            ):
                # Bug-6293: deferred-credential connections persist empty
                # ciphertext (b""). Fernet.decrypt(b"") raises InvalidToken,
                # 500-ing a credential-including export. Mirror the llm_configs
                # guard just below: skip the credentials field entirely so the
                # connection round-trips as a placeholder (re-enter creds on
                # import) instead of crashing the export.
                plaintext = system_fernet.decrypt(c.encrypted_credentials)
                entry["credentials"] = base64.b64encode(
                    passphrase_fernet.encrypt(plaintext)
                ).decode("ascii")
            conns.append(entry)
        bundle["connections"] = conns

    if "llm_configs" in sections:
        llm_q = await tenant_db.execute(
            select(LLMProviderConfig)
            .where(LLMProviderConfig.project_id == project_id)
            .order_by(LLMProviderConfig.created_at)
        )
        llm_list: list[dict[str, Any]] = []
        for lc in llm_q.scalars().all():
            entry = {
                "id": _j(lc.id),
                "provider": lc.provider,
                "display_name": lc.display_name,
                "base_url": lc.base_url,
                "model_name": lc.model_name,
                "max_tokens": lc.max_tokens,
                "temperature": lc.temperature,
                "timeout_seconds": lc.timeout_seconds,
                # F-020-10 (Bug-9080): same strip on the LLM provider config bag.
                "config": sanitise_imported_config(
                    lc.config, label="llm_config", name=lc.display_name,
                ),
            }
            if (
                include_credentials
                and system_fernet
                and passphrase_fernet
                and lc.encrypted_api_key
            ):
                plaintext = system_fernet.decrypt(lc.encrypted_api_key)
                entry["api_key"] = base64.b64encode(
                    passphrase_fernet.encrypt(plaintext)
                ).decode("ascii")
            llm_list.append(entry)
        bundle["llm_configs"] = llm_list

    if "agent_config" in sections:
        cfg_q = await tenant_db.execute(
            select(ProjectAgentConfig)
            .where(ProjectAgentConfig.project_id == project_id)
        )
        cfg = cfg_q.scalar_one_or_none()
        if cfg:
            config_dict: dict[str, Any] = {}
            for field in _AGENT_CONFIG_FIELDS:
                config_dict[field] = _j(getattr(cfg, field, None))
            if (
                include_credentials
                and system_fernet
                and passphrase_fernet
                and cfg.webhook_signing_secret
            ):
                plaintext = system_fernet.decrypt(cfg.webhook_signing_secret)
                config_dict["webhook_signing_secret"] = base64.b64encode(
                    passphrase_fernet.encrypt(plaintext)
                ).decode("ascii")

            am_q = await tenant_db.execute(
                select(ProjectAgentModel)
                .where(ProjectAgentModel.project_id == project_id)
            )
            agent_models = [
                {"model_id": _j(am.model_id)} for am in am_q.scalars().all()
            ]

            amc_q = await tenant_db.execute(
                select(ProjectAgentModelContext)
                .where(ProjectAgentModelContext.project_id == project_id)
            )
            agent_contexts = [
                _row_to_dict(
                    amc, exclude=("derived_at", "published_at", "updated_at")
                )
                for amc in amc_q.scalars().all()
            ]

            rub_q = await tenant_db.execute(
                select(AgentJudgeRubric)
                .where(AgentJudgeRubric.project_id == project_id)
                .order_by(AgentJudgeRubric.created_at)
            )
            rubrics = [
                {"id": _j(r.id), "name": r.name, "sections": r.sections}
                for r in rub_q.scalars().all()
            ]

            bundle["agent_config"] = {
                "config": config_dict,
                "models": agent_models,
                "model_contexts": agent_contexts,
                "judge_rubrics": rubrics,
            }
        else:
            # Bug-6290: emit a valid empty shape so the importer does not
            # reject the bundle.  Most projects never configure the agent,
            # so the export must produce a round-trippable section even
            # when no ProjectAgentConfig row exists.
            bundle["agent_config"] = {
                "config": {},
                "models": [],
                "model_contexts": [],
                "judge_rubrics": [],
            }

    if "cross_model_recipes" in sections:
        rec_q = await tenant_db.execute(
            select(ProjectCrossModelRecipe)
            .where(ProjectCrossModelRecipe.project_id == project_id)
            .order_by(ProjectCrossModelRecipe.created_at)
        )
        bundle["cross_model_recipes"] = [
            {
                "id": _j(r.id),
                "name": r.name,
                "description": r.description,
                "parameters": r.parameters,
                "steps": r.steps,
                "combine": r.combine,
                "notes": r.notes,
            }
            for r in rec_q.scalars().all()
        ]

    if "project_settings" in sections:
        ps_q = await tenant_db.execute(
            select(ProjectSetting)
            .where(ProjectSetting.project_id == project_id)
        )
        bundle["project_settings"] = [
            {"key": ps.key, "value": ps.value_json}
            for ps in ps_q.scalars().all()
        ]

    if "access_bindings" in sections:
        models_q = await tenant_db.execute(
            select(Model.id, Model.slug)
            .where(Model.project_id == project_id)
        )
        model_slug_by_id = {row[0]: row[1] for row in models_q.all()}

        ab_q = await tenant_db.execute(
            select(UserAccessBinding)
            .where(UserAccessBinding.project_id == project_id)
        )
        bindings: list[dict[str, Any]] = []
        for ab in ab_q.scalars().all():
            bindings.append({
                "user_identity": ab.user_identity,
                "role": ab.role,
                "model_slug": (
                    model_slug_by_id.get(ab.model_id) if ab.model_id else None
                ),
                # Bug-6599: carry provenance so an exported sso_group binding
                # does not silently re-import as "manual" (which would make an
                # SSO-managed grant permanent and unrevocable post-import).
                "source": ab.source,
            })
        bundle["access_bindings"] = bindings

    # Models are always included
    models_q = await tenant_db.execute(
        select(Model)
        .where(Model.project_id == project_id)
        .order_by(Model.created_at, Model.id)
    )
    model_rows = list(models_q.scalars().all())
    model_snapshots: list[dict[str, Any]] = []
    # Bug-8380: the endpoint supplies one REPEATABLE READ session for the
    # complete project bundle. Keep model snapshots on that same session so
    # project sections and every model share one committed observation point.
    for m in model_rows:
        snap = await snapshot_model(m.id, tenant_db, include_versions=True)
        model_snapshots.append(snap)
    bundle["models"] = model_snapshots

    bundle["test_metadata"] = None

    return bundle
