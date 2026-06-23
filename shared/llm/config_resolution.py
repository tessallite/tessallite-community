"""LLM config resolution functions.

Each function has an isolated scope — they are intentionally separate.
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import LLMProviderConfig, Model, ModelAISchedulerConfig, ProjectAgentConfig
from shared.llm.adapter import LLMConfig, to_llm_config

logger = logging.getLogger(__name__)


async def resolve_agent_llm_config(
    project_id: UUID,
    role: str,
    db: AsyncSession,
) -> LLMConfig:
    """Resolve answer-LLM or judge-LLM config for a project.

    Reads ProjectAgentConfig.{answer,judge}_llm_config_id directly.
    """
    if role not in ("answer", "judge"):
        raise ValueError(f"Unknown LLM role: {role!r}")

    cfg_row = await db.execute(
        select(ProjectAgentConfig).where(
            ProjectAgentConfig.project_id == project_id
        )
    )
    cfg = cfg_row.scalar_one_or_none()

    record: Optional[LLMProviderConfig] = None
    if cfg is not None:
        if role == "judge":
            ref = cfg.judge_llm_config_id or cfg.answer_llm_config_id
        else:
            ref = cfg.answer_llm_config_id
        if ref is not None:
            record = await db.get(LLMProviderConfig, ref)

    if record is None:
        raise ValueError(
            "No LLM configuration available for the agent. Add a provider "
            "in the Project drawer's LLM Configurations tab and pick it on "
            "the LLM & Judge tab."
        )

    return to_llm_config(record)


async def resolve_agent_llm_failover_configs(
    project_id: UUID,
    role: str,
    db: AsyncSession,
) -> list[LLMConfig]:
    """Return the failover-eligible LLM configs for a role, primary first.

    The primary is the config set in ProjectAgentConfig.

    F-023-15 — failover is restricted to configs of the **same provider
    family** as the primary. The previous behaviour fell over to every
    project-level LLMProviderConfig ordered by display name, so a config
    added only for the glossary bootstrap (a different provider, different
    data-processing jurisdiction, different cost profile) could silently
    receive the full grounded answer prompt with no admin opt-in. Keeping
    failover within one provider family means a retry stays in the same
    governance and cost envelope. When the primary is the only config of
    its provider, the list is just the primary (no failover) — that is the
    safe default, not a silent expansion of where data flows.
    """
    if role not in ("answer", "judge"):
        raise ValueError(f"Unknown LLM role: {role!r}")

    cfg_row = await db.execute(
        select(ProjectAgentConfig).where(
            ProjectAgentConfig.project_id == project_id
        )
    )
    cfg = cfg_row.scalar_one_or_none()

    primary_id = None
    if cfg is not None:
        if role == "judge":
            primary_id = cfg.judge_llm_config_id or cfg.answer_llm_config_id
        else:
            primary_id = cfg.answer_llm_config_id

    all_rows = await db.execute(
        select(LLMProviderConfig)
        .where(LLMProviderConfig.project_id == project_id)
        .order_by(LLMProviderConfig.display_name)
    )
    records = list(all_rows.scalars().all())

    primary_record: Optional[LLMProviderConfig] = None
    if primary_id:
        for r in records:
            if r.id == primary_id:
                primary_record = r
                break

    ordered: list[LLMProviderConfig] = []
    if primary_record is not None:
        ordered.append(primary_record)
        primary_provider = (primary_record.provider or "").strip().lower()
        # Same-provider-family failover only.
        for r in records:
            if r.id == primary_record.id:
                continue
            if (r.provider or "").strip().lower() == primary_provider:
                ordered.append(r)
    else:
        # No primary set — fall back to every config (legacy resolve path).
        ordered = list(records)

    if not ordered:
        raise ValueError(
            "No LLM configuration available for the agent. Add a provider "
            "in the Project drawer's LLM Configurations tab."
        )

    return [to_llm_config(r) for r in ordered]


async def _project_agent_config_for_model(
    model_id: UUID,
    db: AsyncSession,
) -> Optional[ProjectAgentConfig]:
    """Return the ProjectAgentConfig row for the project owning ``model_id``."""
    model_row = await db.get(Model, model_id)
    if model_row is None:
        return None
    cfg_result = await db.execute(
        select(ProjectAgentConfig).where(
            ProjectAgentConfig.project_id == model_row.project_id
        )
    )
    return cfg_result.scalar_one_or_none()


async def _first_existing_config(
    db: AsyncSession,
    candidate_ids: list[Optional[UUID]],
) -> Optional[LLMProviderConfig]:
    """Return the first LLMProviderConfig that exists for the candidate ids, in order."""
    for cid in candidate_ids:
        if cid is None:
            continue
        record = await db.get(LLMProviderConfig, cid)
        if record is not None:
            return record
    return None


async def _model_label(model_id: UUID, db: AsyncSession) -> str:
    """Resolve a human-friendly label for a model.

    Bug-3614/Bug-1059: the optimiser/glossary creator failure alerts compose a
    user-facing ``detail`` string that the validation tray renders verbatim to
    the modeller. A raw model UUID is meaningless to end users, so prefer the
    model's ``display_name``, then its ``slug``; fall back to the UUID only when
    neither is available (e.g. the model row was deleted between scheduling and
    failure). The UUID may still appear in structured/log fields, but never as
    the sole identifier in the alert ``detail``.
    """
    model = await db.get(Model, model_id)
    if model is not None:
        label = (model.display_name or "").strip() or (model.slug or "").strip()
        if label:
            return label
    return str(model_id)


async def resolve_optimizer_llm_config(
    model_id: UUID,
    db: AsyncSession,
) -> LLMConfig:
    """Resolve the aggregate-creator (optimizer) LLM for a model.

    Resolution order:
      1. model_ai_scheduler_config.llm_config_id        (per-model override).
      2. ProjectAgentConfig.aggregate_llm_config_id     (project default).
      3. ProjectAgentConfig.answer_llm_config_id        (agent fallback).
      4. Raise if none exists.
    """
    sched_result = await db.execute(
        select(ModelAISchedulerConfig).where(ModelAISchedulerConfig.model_id == model_id)
    )
    sched = sched_result.scalar_one_or_none()
    cfg = await _project_agent_config_for_model(model_id, db)

    config_record = await _first_existing_config(
        db,
        [
            sched.llm_config_id if sched else None,
            cfg.aggregate_llm_config_id if cfg else None,
            cfg.answer_llm_config_id if cfg else None,
        ],
    )
    if config_record is None:
        raise ValueError(
            f"No LLM config found for the aggregate creator on model "
            f"{await _model_label(model_id, db)}. "
            "Assign one on the project LLM screen or the model's LLM tab."
        )
    return to_llm_config(config_record)


async def resolve_glossary_llm_config(
    model_id: UUID,
    db: AsyncSession,
) -> LLMConfig:
    """Resolve the glossary-creator LLM for a model.

    Resolution order:
      1. model_ai_scheduler_config.glossary_llm_config_id (per-model override).
      2. ProjectAgentConfig.glossary_llm_config_id        (project default).
      3. ProjectAgentConfig.answer_llm_config_id          (agent fallback).
      4. Raise if none exists.
    """
    sched_result = await db.execute(
        select(ModelAISchedulerConfig).where(ModelAISchedulerConfig.model_id == model_id)
    )
    sched = sched_result.scalar_one_or_none()
    cfg = await _project_agent_config_for_model(model_id, db)

    config_record = await _first_existing_config(
        db,
        [
            sched.glossary_llm_config_id if sched else None,
            cfg.glossary_llm_config_id if cfg else None,
            cfg.answer_llm_config_id if cfg else None,
        ],
    )
    if config_record is None:
        raise ValueError(
            f"No LLM config found for the glossary creator on model "
            f"{await _model_label(model_id, db)}. "
            "Assign one on the project LLM screen or the model's LLM tab."
        )
    return to_llm_config(config_record)
