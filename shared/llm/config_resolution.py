"""LLM config resolution functions.

Each function has an isolated scope — they are intentionally separate.

PROJECT SCOPE IS PART OF EVERY LOOKUP HERE
------------------------------------------
An ``LLMProviderConfig`` row carries a Fernet-encrypted provider API key and a
base_url, and ``llm_provider_configs.id`` is tenant-schema-wide. The write path
now proves that a config id arriving in a REQUEST BODY belongs to the path
project (``agent-service/src/api/_body_scope.py``,
``model-service/src/api/scheduler_config.py``). That guard says nothing about
ids ALREADY STORED: a ``project_agent_configs`` or
``model_ai_scheduler_configs`` row bound to another project's config before the
guard existed kept resolving through a bare ``db.get``, so this project's
prompts were sent to, and billed to, that project's provider account — silently,
with no refusal anywhere.

Every dereference of a stored config id therefore carries
``project_id == <owning project>`` INSIDE the query. A foreign id is then
indistinguishable from an unknown one, and each site keeps the behaviour it
already had for "this id resolves to nothing":

* ``resolve_agent_llm_config`` raises — it has no further fallback, so the
  admin is told to re-pick a config rather than being silently moved onto a
  different provider.
* ``_first_existing_config`` skips to the next candidate — that chain is a
  documented model-override -> project-default -> agent-default fallback and
  already skipped a candidate that did not resolve.
* ``resolve_agent_llm_failover_configs`` needs no change: it already selects
  the project's configs and matches the primary within them, so a foreign
  primary already fails to match.
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


async def _llm_config_in_project(
    db: AsyncSession,
    config_id: Optional[UUID],
    project_id: UUID,
) -> Optional[LLMProviderConfig]:
    """Load one ``LLMProviderConfig`` if ``project_id`` owns it, else ``None``.

    The ownership predicate is in the SELECT rather than in a comparison after
    a ``db.get``: a foreign row must not be read at all, because reading it is
    what put another project's encrypted API key and base_url in reach of this
    project's request in the first place.
    """
    if config_id is None:
        return None
    result = await db.execute(
        select(LLMProviderConfig)
        .where(LLMProviderConfig.project_id == project_id)
        .where(LLMProviderConfig.id == config_id)
    )
    return result.scalars().one_or_none()


async def resolve_agent_llm_config(
    project_id: UUID,
    role: str,
    db: AsyncSession,
) -> LLMConfig:
    """Resolve answer-LLM or judge-LLM config for a project.

    Reads ProjectAgentConfig.{answer,judge}_llm_config_id directly, scoped to
    ``project_id``. A stored id belonging to another project resolves to
    nothing and raises the same "no LLM configuration available" error an
    unset field raises — the agent stops instead of running on, and billing
    to, the other project's provider account.
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
        record = await _llm_config_in_project(db, ref, project_id)

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

    # The primary is matched WITHIN this project's own configs, so a stored
    # primary_id belonging to another project simply does not match — the same
    # outcome as an id that resolves to nothing. No unscoped load happens here.
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


async def _project_scope_for_model(
    model_id: UUID,
    db: AsyncSession,
) -> tuple[Optional[UUID], Optional[ProjectAgentConfig]]:
    """Return ``(owning project_id, ProjectAgentConfig)`` for ``model_id``.

    The project_id is returned alongside the config because it is the scope
    every subsequent LLM-config lookup must be proven against. When the model
    row is gone there is no project to prove ownership against, so both are
    ``None`` and the caller fails closed rather than resolving a stored config
    id that nothing can vouch for.
    """
    model_row = await db.get(Model, model_id)
    if model_row is None:
        return None, None
    cfg_result = await db.execute(
        select(ProjectAgentConfig).where(
            ProjectAgentConfig.project_id == model_row.project_id
        )
    )
    return model_row.project_id, cfg_result.scalar_one_or_none()


async def _first_existing_config(
    db: AsyncSession,
    candidate_ids: list[Optional[UUID]],
    *,
    project_id: Optional[UUID],
) -> Optional[LLMProviderConfig]:
    """First candidate id that resolves to a config THIS PROJECT owns, in order.

    ``project_id`` is keyword-only so a transposed argument — which would be a
    silently-passing ownership check — is unexpressible. ``None`` means the
    owning project could not be established, and nothing resolves.

    An id owned by another project is skipped exactly as an id that resolves to
    nothing is skipped: this is an ordered fallback chain (model override ->
    project default -> agent default), so the next candidate is tried and the
    caller lands on a config the project actually owns.
    """
    if project_id is None:
        return None
    for cid in candidate_ids:
        record = await _llm_config_in_project(db, cid, project_id)
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
    project_id, cfg = await _project_scope_for_model(model_id, db)

    config_record = await _first_existing_config(
        db,
        [
            sched.llm_config_id if sched else None,
            cfg.aggregate_llm_config_id if cfg else None,
            cfg.answer_llm_config_id if cfg else None,
        ],
        project_id=project_id,
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
    project_id, cfg = await _project_scope_for_model(model_id, db)

    config_record = await _first_existing_config(
        db,
        [
            sched.glossary_llm_config_id if sched else None,
            cfg.glossary_llm_config_id if cfg else None,
            cfg.answer_llm_config_id if cfg else None,
        ],
        project_id=project_id,
    )
    if config_record is None:
        raise ValueError(
            f"No LLM config found for the glossary creator on model "
            f"{await _model_label(model_id, db)}. "
            "Assign one on the project LLM screen or the model's LLM tab."
        )
    return to_llm_config(config_record)
