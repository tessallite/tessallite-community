"""Webhook endpoint management API.

CRUD for webhook endpoints, delivery history, DLQ management, test events.
All endpoints require tenant_admin role.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select

from shared.audit.logger import audit_required
from shared.db.models import WebhookDelivery, WebhookEndpoint
from shared.db.session import get_tenant_db
from shared.webhooks.dispatcher import (
    attempt_delivery,
    deliver_test_event,
    generate_signing_secret,
    rebuild_signed_body,
    seal_destination_url,
)
from shared.webhooks.event_types import event_catalogue, is_valid_filter
# Bug-8350-sibling (R2 MED-4) — read-time redaction backstop, defense in
# depth on top of the write-time scrub the dispatcher now applies in
# attempt_delivery. Guards rows written before that fix shipped, and any
# future write-path regression, from leaking a URL through GET
# /admin/webhooks/dlq, GET /admin/webhooks/{id}/deliveries, or the
# webhook.dlq_retry / webhook.dlq_delete audit log entries below.
from shared.webhooks.redact import redact_url_for_display, scrub_url_from_text
from shared.webhooks.ssrf import validate_webhook_url
from src.auth.middleware import CurrentUser, require_tenant_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/webhooks", tags=["webhooks"])


def _validate_event_filters(filters: list[str]) -> None:
    """Reject any filter that is not the wildcard or a known event name.

    Bug-7330: an empty ``event_filters`` list is explicitly rejected. Under
    the Bug-6313 fail-closed semantic, ``[]`` means "receive nothing", which
    is never the caller's intent when creating or updating an endpoint.  Use
    ``["*"]`` for all events, or list specific event names. Deactivate the
    endpoint if no events should be delivered.
    """
    if not filters:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="event_filters must not be empty. Use ['*'] to receive "
                   "all events, or list specific event names. To stop "
                   "delivery, deactivate the endpoint instead.",
        )
    unknown = [f for f in filters if not is_valid_filter(f)]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown webhook event_filters: {unknown}. "
                   "Use GET /admin/webhooks/event-types for the valid set.",
        )


def _validate_url(url: str) -> None:
    """F-022-05: reject SSRF-unsafe webhook URLs at the admin surface."""
    try:
        validate_webhook_url(url)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid webhook URL: {exc}",
        )


def _redact_url_for_audit(url: str) -> str:
    """Bug-8429 — delegate to the platform's ONE URL-redaction rule.

    Bug-7341 introduced a private copy here whose docstring promised to
    "strip credentials and token-like query/path segments" and stated that
    "only the scheme + host + redacted path are safe to persist". The
    implementation stripped userinfo, query and fragment — and kept
    ``parsed.path`` completely verbatim, under a comment that said so. A
    webhook URL embedding a bearer token in the PATH
    (``https://receiver.example/hooks/<token>`` — the exact shape Bug-8350's
    repro used) therefore had that token written in plaintext into the tenant
    audit log by ``create_webhook``: a longer-lived, more widely-copied record
    (audit table, UI, CSV exports, backups) than the endpoint itself. The
    false docstring was the worst part: an operator reading it believed the
    material was redacted.

    ``shared.webhooks.redact.redact_url_for_display`` already drops the path
    for exactly this reason and is what both dispatchers persist. The private
    copy is deleted rather than corrected, so there is one rule and no second
    place for it to drift (see also Bug-8445 on what five copies of one rule
    cost this codebase).
    """
    return redact_url_for_display(url)


class WebhookCreate(BaseModel):
    name: str
    url: str
    event_filters: list[str] = ["*"]


class WebhookUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    event_filters: list[str] | None = None
    is_active: bool | None = None


class WebhookResponse(BaseModel):
    id: uuid.UUID
    name: str
    url: str
    event_filters: list[str]
    is_active: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class WebhookCreateResponse(WebhookResponse):
    """Bug-6312: returned only on create and rotate-secret so the admin
    receives the plaintext signing secret exactly once.  Ordinary GET/list
    responses use ``WebhookResponse`` which has no ``signing_secret`` field,
    preventing accidental exposure on subsequent reads."""

    signing_secret: str


class WebhookUpdateResponse(WebhookResponse):
    """Bug-8556: the update response, which carries the one-time plaintext
    signing secret WHEN — and only when — this update rotated it.

    ``update_webhook`` rotates the signing secret whenever the endpoint is
    repointed at a different receiver (Bug-8410 parity), because the secret is
    scoped to the receiver it was shared with. Until this field existed the
    rotation was invisible: the route returned ``WebhookResponse``, which has
    no ``signing_secret``, so the admin saved an edit and every subsequent
    delivery was signed with a value no human had ever seen. A
    signature-verifying receiver answers 401/403, ``_is_non_retryable`` treats
    that as terminal, and the endpoint was dead on its first event.

    ``signing_secret`` is ``None`` on every update that did NOT rotate (a name
    change, an event-filter change, an idempotent re-save of the same URL), so
    the SPA opens its existing secret dialog only when there is genuinely a new
    secret to hand over. This mirrors ``WebhookCreateResponse``: the plaintext
    is returned exactly once, at the moment it is generated, and is never
    readable again through any GET.
    """

    signing_secret: str | None = None


class DeliveryResponse(BaseModel):
    id: uuid.UUID
    endpoint_id: uuid.UUID
    event_type: str
    payload: dict
    status: str
    attempts: int
    response_code: int | None = None
    error_message: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


def _redact_delivery(
    delivery: WebhookDelivery, endpoint_url: str | None = None
) -> DeliveryResponse:
    """Bug-8350-sibling (R2 MED-4) — read-time redaction backstop for
    ``error_message``. The write path (``attempt_delivery``) scrubs URLs out
    of ``error_message`` before persisting, but a row written before that
    fix shipped predates the scrub; re-applying it here guarantees GET
    /admin/webhooks/{id}/deliveries and GET /admin/webhooks/dlq can never
    return a raw URL regardless of when the row was written.

    Bug-8357 — ``endpoint_url`` is passed wherever it is known. Without it,
    the scrub can only sweep for text carrying a scheme delimiter; a receiver
    that echoed only the credential-bearing PATH (``/hooks/<token>``) is
    indistinguishable from ordinary free text and survives. Every caller here
    has the endpoint in hand or can load it in one query, so there is no
    reason to run the weaker form.
    """
    resp = DeliveryResponse.model_validate(delivery)
    resp.error_message = scrub_url_from_text(resp.error_message, endpoint_url)
    return resp


@router.get("/event-types")
async def list_event_types(
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> list[dict[str, str]]:
    """The single source of truth for subscribable webhook event names.

    The frontend webhook picker fetches this so it can never drift from
    the events the backend actually emits.
    """
    return event_catalogue()


@router.get("", response_model=list[WebhookResponse])
async def list_webhooks(
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> list[WebhookResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(
            select(WebhookEndpoint).order_by(WebhookEndpoint.name)
        )
        return [
            WebhookResponse.model_validate(e).model_copy(
                update={"url": redact_url_for_display(e.url)}
            )
            for e in result.scalars().all()
        ]


@router.post("", response_model=WebhookCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_webhook(
    body: WebhookCreate,
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> WebhookCreateResponse:
    _validate_event_filters(body.event_filters)
    _validate_url(body.url)
    async for db in get_tenant_db(current_user.tenant_id):
        # Bug-6312: capture the plaintext so it can be returned to the admin
        # exactly once in the 201 response.  Only the encrypted form is stored.
        plaintext, encrypted = generate_signing_secret()
        ep = WebhookEndpoint(
            name=body.name,
            url=body.url,
            event_filters=body.event_filters,
            signing_secret=encrypted,
        )
        db.add(ep)
        # Bug-7341: redact the URL before persisting it in audit records.
        # Webhook URLs may carry bearer tokens in userinfo, query params, or
        # path segments. The audit table, UI, and CSV exports retain this data
        # independently and for longer than the endpoint itself.
        await audit_required(
            db, action="webhook.create", severity="warn",
            actor_email=current_user.email,
            target_type="webhook", target_name=body.name,
            detail={"url": _redact_url_for_audit(body.url), "event_filters": body.event_filters},
        )
        await db.commit()
        await db.refresh(ep)
        # Return the full endpoint data plus the one-time plaintext secret.
        base = WebhookResponse.model_validate(ep)
        return WebhookCreateResponse(
            **base.model_dump(), signing_secret=plaintext,
        )


@router.put("/{webhook_id}", response_model=WebhookUpdateResponse)
async def update_webhook(
    webhook_id: uuid.UUID,
    body: WebhookUpdate,
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> WebhookUpdateResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        ep = await db.get(WebhookEndpoint, webhook_id)
        if ep is None:
            raise HTTPException(status_code=404, detail="Webhook not found")
        if body.event_filters is not None:
            _validate_event_filters(body.event_filters)
        if body.url is not None:
            _validate_url(body.url)
        # Bug-8410 parity (R1 reviewer F6) — a signing secret is scoped to the
        # receiver it was shared with. agent-service rotates on a receiver
        # change; this dispatcher family must not silently hand receiver A's
        # secret to receiver B, or A's operator (who has it written down) can
        # forge events B accepts as authentic. `generate_signing_secret` has
        # four call sites and only agent-service's had the rule — shipping one
        # dispatcher that rotates and one that does not is worse than either
        # behaviour applied consistently.
        previous_url = ep.url
        updates = body.model_dump(exclude_unset=True)
        for key, value in updates.items():
            setattr(ep, key, value)
        rotated_for_new_receiver = False
        rotated_plaintext: str | None = None
        if (ep.url or "").strip() and (ep.url or "").strip() != (
            previous_url or ""
        ).strip():
            rotated_plaintext, encrypted = generate_signing_secret()
            ep.signing_secret = encrypted
            rotated_for_new_receiver = True
            # Bug-8556 — the rotation used to be INVISIBLE: this route returned
            # `WebhookResponse`, which carries no plaintext (only create and
            # rotate-secret did), so the admin saved an edit and every
            # subsequent delivery was signed with a secret no human had ever
            # seen. A signature-verifying receiver answers 401/403, which
            # `_is_non_retryable` treats as terminal, so the endpoint was dead
            # on the first event with no retry. The plaintext now comes back in
            # `WebhookUpdateResponse` and the SPA shows it in the same
            # one-time secret dialog it already uses for create and rotate; the
            # operator log line stays as the server-side record.
            logger.warning(
                "Webhook endpoint %s was repointed at a different receiver; "
                "the signing secret was rotated so the previous receiver can "
                "no longer sign or verify events for it. The new plaintext "
                "secret is returned once in this update response — share it "
                "with the new receiver. Until it is shared, this endpoint's "
                "deliveries will be rejected by it. If it was not captured, "
                "call POST /admin/webhooks/%s/rotate-secret for a new one.",
                ep.id, ep.id,
            )
        await audit_required(
            db, action="webhook.update", severity="warn",
            actor_email=current_user.email,
            target_type="webhook", target_id=ep.id, target_name=ep.name,
            detail={
                "fields": list(updates.keys()),
                # The rotation is a security-relevant side effect of an edit
                # the admin did not explicitly ask for, so it belongs in the
                # audit record rather than only in the code.
                "signing_secret_rotated": rotated_for_new_receiver,
            },
        )
        await db.commit()
        await db.refresh(ep)
        base = WebhookResponse.model_validate(ep)
        return WebhookUpdateResponse(
            **base.model_dump(), signing_secret=rotated_plaintext,
        )


@router.delete("/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    webhook_id: uuid.UUID,
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        ep = await db.get(WebhookEndpoint, webhook_id)
        if ep is None:
            raise HTTPException(status_code=404, detail="Webhook not found")
        name = ep.name
        await db.delete(ep)
        await audit_required(
            db, action="webhook.delete", severity="critical",
            actor_email=current_user.email,
            target_type="webhook", target_id=webhook_id, target_name=name,
        )
        await db.commit()


@router.post("/{webhook_id}/test", response_model=DeliveryResponse)
async def test_webhook(
    webhook_id: uuid.UUID,
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> DeliveryResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        ep = await db.get(WebhookEndpoint, webhook_id)
        if ep is None:
            raise HTTPException(status_code=404, detail="Webhook not found")

        # F-022-06: single attempt, no inline backoff — the request returns
        # promptly even against an unreachable endpoint (one 10s client
        # timeout at most). F-022-07: deliver_test_event returns the exact row
        # it created, so the caller never sees an unrelated "latest delivery".
        delivery = await deliver_test_event(
            db,
            ep,
            payload={
                "message": "Test delivery from Tessallite",
                "triggered_by": current_user.email,
            },
        )
        await db.commit()
        await db.refresh(delivery)
        return _redact_delivery(delivery, ep.url)


@router.post("/{webhook_id}/rotate-secret")
async def rotate_secret(
    webhook_id: uuid.UUID,
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> dict:
    async for db in get_tenant_db(current_user.tenant_id):
        ep = await db.get(WebhookEndpoint, webhook_id)
        if ep is None:
            raise HTTPException(status_code=404, detail="Webhook not found")
        plaintext, encrypted = generate_signing_secret()
        ep.signing_secret = encrypted
        # F-022-01/F-022-02: rotating a webhook signing secret is a protected
        # security mutation. Fail closed so the rotation cannot commit without a
        # durable audit record. In-flight deliveries keep their pinned old
        # secret (F-022-06), so rotation does not break queued signatures.
        await audit_required(
            db, action="webhook.secret_rotated", severity="critical",
            actor_email=current_user.email,
            target_type="webhook", target_id=ep.id, target_name=ep.name,
        )
        await db.commit()
        return {"signing_secret": plaintext}


@router.get("/{webhook_id}/deliveries", response_model=list[DeliveryResponse])
async def list_deliveries(
    webhook_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> list[DeliveryResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(
            select(WebhookDelivery)
            .where(WebhookDelivery.endpoint_id == webhook_id)
            .order_by(WebhookDelivery.created_at.desc())
            .limit(limit).offset(offset)
        )
        rows = result.scalars().all()
        # Bug-8357 — give the backstop the endpoint URL so it can also strip a
        # scheme-less echo of the credential-bearing path.
        endpoint = await db.get(WebhookEndpoint, webhook_id)
        endpoint_url = endpoint.url if endpoint is not None else None
        return [_redact_delivery(d, endpoint_url) for d in rows]


@router.get("/dlq/count")
async def dlq_count(
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> dict:
    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(
            select(func.count())
            .select_from(WebhookDelivery)
            .where(WebhookDelivery.status == "dlq")
        )
        return {"count": result.scalar_one()}


@router.get("/dlq", response_model=list[DeliveryResponse])
async def list_dlq(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> list[DeliveryResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(
            select(WebhookDelivery)
            .where(WebhookDelivery.status == "dlq")
            .order_by(WebhookDelivery.created_at.desc())
            .limit(limit).offset(offset)
        )
        rows = result.scalars().all()
        # Bug-8357 — DLQ rows span endpoints, so resolve each row's URL for
        # the backstop in ONE query rather than per row.
        endpoint_ids = {d.endpoint_id for d in rows}
        urls: dict = {}
        if endpoint_ids:
            eps = await db.execute(
                select(WebhookEndpoint).where(WebhookEndpoint.id.in_(endpoint_ids))
            )
            urls = {e.id: e.url for e in eps.scalars().all()}
        return [_redact_delivery(d, urls.get(d.endpoint_id)) for d in rows]


@router.post("/dlq/{delivery_id}/retry", response_model=DeliveryResponse)
async def retry_dlq(
    delivery_id: uuid.UUID,
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> DeliveryResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        delivery = await db.get(WebhookDelivery, delivery_id)
        if delivery is None or delivery.status != "dlq":
            raise HTTPException(status_code=404, detail="DLQ entry not found")

        ep = await db.get(WebhookEndpoint, delivery.endpoint_id)
        if ep is None or not ep.is_active:
            raise HTTPException(
                status_code=409,
                detail="Endpoint missing or inactive; cannot retry",
            )

        # Bug-7333: audit the DLQ retry before mutating — capture prior state.
        prior_status = delivery.status
        prior_attempts = delivery.attempts
        prior_error = delivery.error_message

        # F-022-13: a manual DLQ retry is a single re-delivery of the *same*
        # row — one attempt, no inline backoff that would hang the admin
        # request for minutes. The attempt counter is reset so the row gets a
        # fresh backoff schedule; on failure the scheduler drain job continues
        # the retries asynchronously (it never sleeps in this request).
        delivery.status = "pending"
        delivery.attempts = 0
        delivery.error_message = None
        delivery.next_attempt_at = None

        # Bug-8056: a manual DLQ retry is an explicit operator decision to
        # re-send using the endpoint's CURRENT signing secret. Re-pin the
        # delivery's snapshot to that secret so a retry performed AFTER the
        # operator rotated the secret (the exact recovery a missing-signature
        # failure instructs them to take) actually signs the payload. Without
        # this, rebuild_signed_body keeps reading the stale pinned snapshot, so
        # a delivery DLQ'd for an unsignable secret would refuse-and-DLQ forever
        # no matter how many times the secret is rotated and retried.
        delivery.signing_secret_snapshot = ep.signing_secret

        # Bug-8557: re-pin the DESTINATION for the same reason and with the
        # same authority. A manual retry is the operator explicitly asking to
        # send this row to wherever the endpoint points NOW, so re-targeting it
        # here is a decision, not a guess — which is exactly what the automatic
        # dispatch paths are forbidden from doing. This is also the recovery
        # path for a legacy row enqueued before migration 0202 added the
        # column: without it, rebuild_signed_body would return None and the
        # retry would immediately dead-letter again as an incoherent row.
        delivery.destination_url_snapshot = seal_destination_url(ep.url)

        url, body_bytes, sig_header = rebuild_signed_body(ep, delivery)
        # url cannot be None here: the destination was just re-pinned from a
        # live endpoint whose url column is NOT NULL.
        await attempt_delivery(delivery, url, body_bytes, sig_header)

        # Bug-7333: emit audit event for DLQ retry so compliance reviewers
        # can reconstruct who retried which failed delivery and what happened.
        await audit_required(
            db, action="webhook.dlq_retry", severity="warn",
            actor_email=current_user.email,
            target_type="webhook_delivery", target_id=delivery.id,
            detail={
                "endpoint_id": str(delivery.endpoint_id),
                "event_type": delivery.event_type,
                "prior_status": prior_status,
                "prior_attempts": prior_attempts,
                # Bug-8350-sibling (R2 MED-4) — scrub before it ever reaches
                # the audit log, defense in depth for a row written before
                # the write-time scrub in attempt_delivery shipped.
                "prior_error": (
                    scrub_url_from_text(prior_error, ep.url) or ""
                )[:200],
                "new_status": delivery.status,
            },
        )

        await db.commit()
        await db.refresh(delivery)
        return _redact_delivery(delivery, ep.url)


@router.delete("/dlq/{delivery_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dlq(
    delivery_id: uuid.UUID,
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        delivery = await db.get(WebhookDelivery, delivery_id)
        if delivery is None or delivery.status != "dlq":
            raise HTTPException(status_code=404, detail="DLQ entry not found")

        # Bug-8357 (R1 reviewer F5) — this call scrubs into the AUDIT LOG,
        # which outlives and is exported more widely than the DLQ row it is
        # recording the destruction of. It used to run the URL-less form, so a
        # receiver echo of only the credential-bearing path survived into it.
        # The endpoint id is in hand; one read gives the scrub its strong form.
        endpoint = await db.get(WebhookEndpoint, delivery.endpoint_id)
        endpoint_url = endpoint.url if endpoint is not None else None

        # Bug-7333: audit the DLQ deletion before destroying the evidence.
        # Deleting DLQ entries removes the only record of failed deliveries;
        # the audit event preserves who did it and what was deleted.
        await audit_required(
            db, action="webhook.dlq_delete", severity="warn",
            actor_email=current_user.email,
            target_type="webhook_delivery", target_id=delivery.id,
            detail={
                "endpoint_id": str(delivery.endpoint_id),
                "event_type": delivery.event_type,
                "attempts": delivery.attempts,
                # Bug-8350-sibling (R2 MED-4) — this is the exact leak the
                # gate found: error_message copied verbatim into the audit
                # log. Scrub before it ever reaches this detail blob.
                "error_message": (
                    scrub_url_from_text(delivery.error_message, endpoint_url) or ""
                )[:200],
            },
        )

        await db.delete(delivery)
        await db.commit()
