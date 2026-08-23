"""Best-effort post-deploy KPI re-evaluation trigger (Bug-7982 completion round).

A deploy/revert bumps ``deploy_epoch`` (Bug-7140). The ``$KPIs`` serve predicate
(query-router ``api/routes.py``) then withholds every ``kpi_latest`` row still
stamped with the OLD epoch (Bug-7982 residual 2, fail-closed by design) until
the next scheduled hourly KPI snapshot sweep re-evaluates and re-stamps them —
up to an hour where a deployed model's scorecard/`$KPIs` serves nothing, with no
operator-visible signal that anything is wrong (it looks identical to "no KPIs
configured").

This helper calls the model's own ``evaluate-batch`` endpoint — the SAME
service-context publish path the scheduler sweep already uses (internal bearer
+ the internal-bypass header) — immediately after a deploy/revert commits, so
``$KPIs`` is repopulated in seconds instead of waiting for the sweep. It is a
plain HTTP self-call (mirrors ``cold_start_trigger.py``'s call to the
optimizer) rather than an in-process function call: ``evaluate_batch`` is a
FastAPI request handler whose behaviour (persona resolution, served-KPI
snapshot binding, the internal-service publish gate) is coupled to being
invoked as a real HTTP request, not something safely re-entered as a bare
Python call from within the same commit.

Contract mirrors ``cold_start_trigger.py``:
- Best-effort: a missing/unreachable/slow model-service never fails or blocks
  the deploy/revert response (it already returned before this runs, since the
  caller fires it as a background task after commit). Any error is logged at
  WARNING and swallowed — the scheduled hourly sweep is the fallback within its
  normal cadence.
- Config-driven: the model-service base URL comes from
  ``settings.MODEL_SERVICE_URL``; no hardcoded host.
- No-op when there are no deployed KPI ids to evaluate.
"""
from __future__ import annotations

import logging
from uuid import UUID

import httpx

from shared.auth.service_principal import (
    KPI_EVALUATOR_ROLE,
    SCOPE_KPI_EVALUATE,
    SCOPE_KPI_QUERY_EXECUTE,
    create_service_access_token,
)
from shared.config.settings import get_settings
from shared.middleware.internal_bypass import internal_request_headers

logger = logging.getLogger(__name__)


async def trigger_post_deploy_kpi_reeval(
    tenant_id: str,
    project_id: UUID | str,
    model_id: UUID | str,
    kpi_ids: list[UUID] | list[str],
    *,
    deploy_epoch: int | None = None,
    timeout: float = 30.0,
) -> bool:
    """POST evaluate-batch for every deployed KPI right after a deploy/revert.

    Returns True if the trigger was accepted (HTTP 200), False on any failure
    or when there is nothing to evaluate. Never raises: every failure path
    (auth mint, model-service down, timeout, 4xx/5xx) is logged and swallowed
    so the caller's deploy/revert is never affected.

    Bug-7982 finding 6: on success this ALSO clears the durable
    ``pending_kpi_reeval`` outbox row (written in the deploy/revert transaction)
    for this model up to ``deploy_epoch``, so the scheduler sweep does not later
    re-fire an already-satisfied re-eval. If this trigger never runs (process
    death) the row survives and the sweep drains it — that is the durability the
    outbox provides.
    """
    if not tenant_id or model_id is None or not kpi_ids:
        return False
    settings = get_settings()
    url = (
        f"{settings.MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
        f"/models/{model_id}/kpis/evaluate-batch"
    )
    try:
        # Short-lived internal service token reusing the existing
        # ``kpi-snapshot-sweep`` principal (shared.auth.service_principal's
        # ``_PRINCIPAL_POLICY`` is a closed allow-list of principal ->
        # max_role/scopes pairs; this call is functionally identical to the
        # scheduler's snapshot-sweep evaluate-batch call — a KPI evaluation
        # kickoff — just triggered post-deploy instead of on the hourly timer,
        # so it reuses the SAME narrow kpi_evaluator role + kpi-evaluate scope
        # rather than adding a new principal to that registry). A leaked token
        # cannot mutate tenant data. Never forward the caller's bearer: the
        # deploy/revert actor may be a modeler, but evaluate-batch's kpi_latest
        # PUBLISH gate requires the verified internal-service marker below,
        # which only a service-minted token + header pair can carry.
        token = create_service_access_token(
            principal="kpi-snapshot-sweep",
            tenant_id=tenant_id,
            role=KPI_EVALUATOR_ROLE,
            ttl_minutes=2,
            scopes=[SCOPE_KPI_EVALUATE, SCOPE_KPI_QUERY_EXECUTE],
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                json={"kpi_ids": [str(k) for k in kpi_ids]},
                headers={
                    "Authorization": f"Bearer {token}",
                    **internal_request_headers(),
                },
            )
            if resp.status_code >= 400:
                logger.warning(
                    "Bug-7982: post-deploy KPI re-eval trigger returned %s for "
                    "model=%s (tenant=%s) — $KPIs stays withheld for KPIs "
                    "evaluated under the prior epoch until the next hourly KPI "
                    "snapshot sweep repopulates it.",
                    resp.status_code, model_id, tenant_id,
                )
                return False
            # R7 finding 5: HTTP 200 is NOT proof of publication. evaluate-batch
            # isolates a per-row kpi_latest upsert failure so one bad KPI cannot
            # starve its siblings, and it still returns 200 with the evaluated
            # values. Deleting the DURABLE outbox row on that 200 destroyed the
            # very safety net that exists for a failed publish (a reachable
            # trigger: an over-long formatted_value used to overflow String(128)).
            # Clear the row ONLY when the response explicitly confirms every
            # considered KPI reached a published state. Fail-closed on a
            # missing/False flag — an older model-service that does not report the
            # field leaves the row for the sweep to drain, which costs one extra
            # re-evaluation instead of silent $KPIs unavailability.
            if not _publish_confirmed(resp, model_id, tenant_id):
                return False
            await _clear_pending_outbox(tenant_id, model_id, deploy_epoch)
            return True
    except Exception as exc:
        logger.warning(
            "Bug-7982: post-deploy KPI re-eval trigger failed for model=%s "
            "(tenant=%s): %s — $KPIs stays withheld for KPIs evaluated under "
            "the prior epoch until the next hourly KPI snapshot sweep "
            "repopulates it.",
            model_id, tenant_id, exc,
        )
        return False


def _publish_confirmed(resp, model_id, tenant_id) -> bool:
    """Whether an evaluate-batch 200 actually PUBLISHED every kpi_latest row.

    Bug-7982 R7 finding 5. Shared shape with the scheduler sweep's drain
    (``sweep.py::kpi_publish_confirmed``); both read the same explicit response
    contract instead of inferring publication from the status code.
    """
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — a 200 with an unreadable body is not proof
        body = None
    published = body.get("kpi_latest_published") if isinstance(body, dict) else None
    if published is True:
        return True
    logger.warning(
        "Bug-7982: post-deploy KPI re-eval for model=%s (tenant=%s) returned 200 "
        "but did NOT confirm the kpi_latest publish (kpi_latest_published=%r, "
        "failed=%r) — RETAINING the durable pending_kpi_reeval outbox row so the "
        "scheduler sweep retries. $KPIs may stay withheld until it does.",
        model_id, tenant_id, published,
        body.get("kpi_latest_failed") if isinstance(body, dict) else None,
    )
    return False


async def _clear_pending_outbox(
    tenant_id: str, model_id: UUID | str, deploy_epoch: int | None
) -> None:
    """Best-effort delete of the pending_kpi_reeval outbox row for this model.

    Deletes rows whose ``requested_for_epoch`` is <= the epoch we just
    re-evaluated for, so a NEWER deploy's outbox row (higher epoch) enqueued in
    the meantime is preserved. When ``deploy_epoch`` is None we cannot bound the
    delete safely, so we leave the row for the sweep to drain.
    """
    if deploy_epoch is None:
        return
    try:
        from sqlalchemy import delete as _delete

        from shared.db.models import PendingKpiReeval
        from shared.db.session import get_tenant_db

        _mid = model_id if isinstance(model_id, UUID) else UUID(str(model_id))
        async for db in get_tenant_db(tenant_id):
            await db.execute(
                _delete(PendingKpiReeval).where(
                    PendingKpiReeval.model_id == _mid,
                    PendingKpiReeval.requested_for_epoch <= deploy_epoch,
                )
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — outbox cleanup is best-effort
        logger.warning(
            "Bug-7982: could not clear pending_kpi_reeval outbox for model=%s "
            "(tenant=%s): %s — the sweep will re-check and clear it.",
            model_id, tenant_id, exc,
        )
