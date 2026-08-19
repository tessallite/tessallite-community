"""Best-effort predictive cold-start trigger (Bug-8029).

A fresh deployment never used to initiate the predictive cold-start pipeline:
predictive candidates were only generated on the next scheduled
``predictive_build_sweep`` tick (up to a day later). The optimizer already
exposes a durable, idempotent kickoff endpoint (F-010-01, built in Fix-Lane
E-optz, commit 4aac5bb1):

    POST {OPTIMIZER_URL}/api/v1/models/{model_id}/predictive/cold-start

This helper calls it right after a deploy, a deployed REVERT (Bug-8395), or an
import has durably committed, so a freshly-deployed model generates predictive
candidates now instead of waiting for the sweep.

Contract:
- Best-effort: a missing/unreachable/slow optimizer never fails or blocks the
  deploy/import. Any error is logged at WARNING and swallowed — the scheduled
  predictive sweep is the fallback.
- Idempotent: the optimizer stamps ``(predictive_built_for_version_id,
  predictive_built_for_epoch)`` per DEPLOYMENT (Bug-8395 — the epoch is what
  makes a revert-to-same-version a distinct deployment) and runs under a
  per-model advisory lock, so a duplicate call (e.g. deploy immediately followed
  by the sweep) is harmless. This helper adds no dedup/retry/queue of its own.
- Config-driven: the optimizer base URL comes from ``settings.OPTIMIZER_URL``;
  no hardcoded host.
"""
from __future__ import annotations

import logging
from uuid import UUID

import httpx

from shared.auth.service_principal import (
    SCOPE_PREDICTIVE_COLD_START,
    create_service_access_token,
)
from shared.config.settings import get_settings

logger = logging.getLogger(__name__)


async def trigger_predictive_cold_start(
    tenant_id: str,
    model_id: UUID | str,
    *,
    timeout: float = 10.0,
) -> bool:
    """POST to the optimizer's predictive cold-start kickoff for a model.

    Returns True if the trigger was accepted (HTTP 2xx), False on any failure.
    Never raises: every failure path (auth mint, optimizer down, timeout, 4xx/5xx)
    is logged and swallowed so the caller's deploy/import always succeeds.
    """
    if not tenant_id or model_id is None:
        return False
    settings = get_settings()
    url = (
        f"{settings.OPTIMIZER_URL}"
        f"/api/v1/models/{model_id}/predictive/cold-start"
    )
    try:
        # Short-lived internal service token (mirrors the deploy path's
        # agent-refresh / cache-evict token mint). Never forward the caller's
        # bearer: deploy/import are authorised at modeler, but the optimizer
        # route requires the predictive-cold-start scope or tenant_admin, so a
        # forwarded modeler token would silently 403 (Bug-6204 class).
        token = create_service_access_token(
            principal="model-service-deploy",
            tenant_id=tenant_id,
            role="tenant_admin",
            ttl_minutes=1,
            scopes=[SCOPE_PREDICTIVE_COLD_START],
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code >= 400:
                logger.warning(
                    "predictive cold-start trigger returned %s for model=%s "
                    "(tenant=%s) — the scheduled predictive sweep will pick it "
                    "up.",
                    resp.status_code, model_id, tenant_id,
                )
                return False
            return True
    except Exception as exc:
        logger.warning(
            "predictive cold-start trigger failed for model=%s (tenant=%s): %s "
            "— the scheduled predictive sweep will pick it up.",
            model_id, tenant_id, exc,
        )
        return False
