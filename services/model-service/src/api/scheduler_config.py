"""Per-model AI Scheduler Config CRUD.

GET  /projects/{p}/models/{m}/scheduler-config
PUT  /projects/{p}/models/{m}/scheduler-config
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from shared.config.bootstrap import system_snapshot_get
from shared.config.resolver import get_setting
from shared.config.settings import get_settings
from shared.db.models import LLMProviderConfig, ModelAISchedulerConfig
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    ModelAISchedulerConfigResponse,
    ModelAISchedulerConfigUpdate,
)
from src.api._scope import ensure_model_in_project, ensure_ref_in_project
from src.api._model_lock import acquire_model_definition_lock
from src.auth.middleware import CurrentUser, forbid_embed_user, require_tenant_admin
from src.auth.rbac import require_role

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/scheduler-config",
    tags=["scheduler-config"],
)


async def _notify_optimizer_reload(
    model_id: UUID,
    bearer_token: str | None = None,
    expect_registered: bool = False,
) -> None:
    """
    Notify the optimizer service to reload a model's AI scheduler job.

    Fire-and-forget: if the optimizer is unreachable, the next startup sweep
    will pick up the config. Logs the error but does not fail the PUT.

    ``expect_registered`` is the saved ``ai_enabled`` value: when the
    optimizer answers 200 but reports ``job_registered: false`` for a
    config that should have a job (e.g. an invalid cron expression), the
    mismatch is logged instead of passing silently (review F-10). The
    optimizer derives the tenant from the bearer token, so no tenant_id
    is sent in the payload.
    """
    try:
        headers = {}
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        async with httpx.AsyncClient(
            timeout=float(system_snapshot_get("control.scheduler_config_timeout"))
        ) as client:
            resp = await client.post(
                f"{settings.OPTIMIZER_URL}/api/v1/optimize/ai/scheduler/reload",
                json={"model_id": str(model_id)},
                headers=headers,
            )
        if not resp.is_success:
            logger.warning(
                "Optimizer scheduler reload for model %s returned %s: %s — "
                "the saved config will take effect at the next optimizer "
                "startup sweep",
                model_id, resp.status_code, resp.text[:200],
            )
            return
        job_registered = bool(resp.json().get("job_registered"))
        if expect_registered and not job_registered:
            logger.warning(
                "Optimizer scheduler reload for model %s succeeded but no "
                "job was registered although ai_enabled=true — check the "
                "cron expression and the optimizer logs",
                model_id,
            )
    except Exception as exc:
        logger.warning(
            "Failed to notify optimizer about scheduler reload for model %s: %s",
            model_id, exc,
        )


@router.get("", response_model=ModelAISchedulerConfigResponse)
async def get_scheduler_config(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> ModelAISchedulerConfigResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        result = await db.execute(
            select(ModelAISchedulerConfig).where(ModelAISchedulerConfig.model_id == model_id)
        )
        config = result.scalar_one_or_none()
        if config is None:
            # Return defaults — don't create a row until first PUT.
            # Pull the per-model AI scheduler defaults from the resolver
            # so an admin override at any level is honoured.
            now = datetime.now(timezone.utc)
            return ModelAISchedulerConfigResponse(
                id=UUID("00000000-0000-0000-0000-000000000000"),
                model_id=model_id,
                ai_enabled=False,
                cron_expression=await get_setting(
                    "ai_scheduler.cron",
                    tenant_session=db, project_id=project_id, model_id=model_id,
                ),
                lookback_hours=int(await get_setting(
                    "ai_scheduler.lookback_hours",
                    tenant_session=db, project_id=project_id, model_id=model_id,
                )),
                max_creates_per_run=int(await get_setting(
                    "ai_scheduler.max_creates_per_run",
                    tenant_session=db, project_id=project_id, model_id=model_id,
                )),
                min_confidence=float(await get_setting(
                    "ai_scheduler.confidence_threshold",
                    tenant_session=db, project_id=project_id, model_id=model_id,
                )),
                dry_run=False,
                enable_ai_aggregation=True,
                llm_config_id=None,
                created_at=now,
                updated_at=now,
            )
        return ModelAISchedulerConfigResponse.model_validate(config)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.put("", response_model=ModelAISchedulerConfigResponse)
async def upsert_scheduler_config(
    project_id: UUID,
    model_id: UUID,
    body: ModelAISchedulerConfigUpdate,
    # F-011-11: the config write and the optimizer reload it triggers must
    # require the SAME authority. The optimizer's AI surface — run, reload,
    # and telemetry-snapshot — is uniformly ``require_tenant_admin`` and has
    # no project-scoped binding machinery. Aligning the config write to
    # ``tenant_admin`` makes both sides consistent and fail-closed: a modeler
    # token no longer writes a config it cannot apply (the reload formerly
    # 403'd, so the cron change only took effect at the next optimizer
    # restart). ``require_tenant_admin`` also rejects embed tokens (role
    # ``embed``), so the explicit ``forbid_embed_user`` guard is subsumed.
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> ModelAISchedulerConfigResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)

        updates = body.model_dump(exclude_unset=True)

        # Body-supplied foreign keys. ``llm_config_id`` (the aggregate-creator
        # override) and ``glossary_llm_config_id`` name rows in
        # ``llm_provider_configs``, which is PROJECT-owned and carries a
        # Fernet-encrypted provider API key and a base_url. The route proves
        # the PATH model belongs to the PATH project and then applied the whole
        # body with a blanket ``setattr`` loop, so nothing proved the submitted
        # config id belonged to that project: a foreign id bound this model's
        # AI scheduler to ANOTHER PROJECT'S provider credentials, and every
        # scheduled aggregate/glossary creation run would then bill and prompt
        # through them. ``ensure_ref_in_project`` proves project -> config in
        # one query.
        #
        # The guard keys on the field being PRESENT in the payload
        # (``exclude_unset``), not on it being truthy: an explicit ``null``
        # means "clear the override, inherit the project default" and stays
        # legal — the helper returns None for it.
        #
        # It runs BEFORE ``acquire_model_definition_lock`` and before the
        # ``db.add``/``flush`` that materialises a config row for a model that
        # has never had one, so a request that is about to be refused neither
        # takes the cross-family model-definition lock (serialising unrelated
        # writers behind a doomed request) nor inserts a row on its way out.
        # The optimizer-reload HTTP call already happens after commit, so a
        # refused request never reaches it.
        for _fk_field in ("llm_config_id", "glossary_llm_config_id"):
            if _fk_field in updates:
                await ensure_ref_in_project(
                    db,
                    LLMProviderConfig,
                    ref_id=updates[_fk_field],
                    project_id=project_id,
                    field_name=_fk_field,
                    noun="an LLM provider config",
                    error_code="LLM_CONFIG_NOT_IN_PROJECT",
                )

        # Bug-7982 finding 7 then 3: auth before lock; ModelAISchedulerConfig is
        # snapshot-owned (truncate-reinserted on revert). The optimizer-reload
        # HTTP call happens AFTER commit below, so it is never made under the lock.
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        result = await db.execute(
            select(ModelAISchedulerConfig).where(ModelAISchedulerConfig.model_id == model_id)
        )
        config = result.scalar_one_or_none()
        if config is None:
            config = ModelAISchedulerConfig(model_id=model_id)
            db.add(config)
            await db.flush()

        for k, v in updates.items():
            setattr(config, k, v)
        await db.commit()
        await db.refresh(config)

        # Notify optimizer to reload this model's AI scheduler job
        await _notify_optimizer_reload(
            model_id,
            current_user.raw_token,
            expect_registered=bool(config.ai_enabled),
        )

        return ModelAISchedulerConfigResponse.model_validate(config)
    raise HTTPException(status_code=500, detail="DB session exhausted")
