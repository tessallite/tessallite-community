"""Trigger an immediate rebuild of a model's unhealthy aggregates.

After an import/reseed forces aggregates to ``pending`` (F-013-05 / Bug-5346),
this kicks the scheduler to rebuild them now instead of waiting for the next
scheduled refresh sweep. Best-effort: a missing/unreachable scheduler never
fails the import — the sweep is the fallback.

Shared by the model-service import endpoint and the acme-demo seed script so
the trigger logic (service-token mint + endpoint URL) lives in one place.
"""
from __future__ import annotations

import logging

import httpx

from shared.auth.service_principal import (
    SCOPE_AGGREGATE_REBUILD,
    create_service_access_token,
)
from shared.config.settings import get_settings

logger = logging.getLogger(__name__)


def mint_service_token(tenant_id: str, *, subject: str, ttl_minutes: int = 30) -> str:
    """Mint a short-lived ``tenant_admin`` JWT for an internal service call."""
    principal = subject.removeprefix("service:")
    return create_service_access_token(
        principal=principal,
        tenant_id=tenant_id,
        role="tenant_admin",
        ttl_minutes=ttl_minutes,
        scopes=[SCOPE_AGGREGATE_REBUILD],
    )


async def trigger_model_refresh(
    tenant_id: str,
    model_id: object,
    *,
    timeout: float = 120.0,
) -> dict | None:
    """POST to the scheduler to rebuild a model's pending/invalid aggregates.

    Returns the scheduler's ``{rebuilt, failed, skipped}`` summary, or None if
    the call could not be made (scheduler unreachable, auth, etc.) — the caller
    treats None as "the scheduled sweep will pick them up".
    """
    settings = get_settings()
    url = f"{settings.SCHEDULER_URL}/api/v1/scheduler/trigger/refresh-model"
    token = mint_service_token(tenant_id, subject="aggregate-rebuild-service")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                json={"model_id": str(model_id)},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.warning(
            "Could not trigger import-rebuild for model %s (tenant %s): %s — "
            "the scheduled refresh sweep will rebuild it instead.",
            model_id, tenant_id, exc,
        )
        return None
