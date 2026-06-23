"""Webhook endpoint management API.

CRUD for webhook endpoints, delivery history, DLQ management, test events.
All endpoints require tenant_admin role.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select

from shared.audit.logger import audit
from shared.db.models import WebhookDelivery, WebhookEndpoint
from shared.db.session import get_tenant_db
from shared.webhooks.dispatcher import (
    attempt_delivery,
    deliver_test_event,
    generate_signing_secret,
    rebuild_signed_body,
)
from shared.webhooks.event_types import event_catalogue, is_valid_filter
from shared.webhooks.ssrf import validate_webhook_url
from src.auth.middleware import CurrentUser, require_tenant_admin

router = APIRouter(prefix="/admin/webhooks", tags=["webhooks"])


def _validate_event_filters(filters: list[str]) -> None:
    """Reject any filter that is not the wildcard or a known event name."""
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
        return [WebhookResponse.model_validate(e) for e in result.scalars().all()]


@router.post("", response_model=WebhookResponse, status_code=status.HTTP_201_CREATED)
async def create_webhook(
    body: WebhookCreate,
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> WebhookResponse:
    _validate_event_filters(body.event_filters)
    _validate_url(body.url)
    async for db in get_tenant_db(current_user.tenant_id):
        _, encrypted = generate_signing_secret()
        ep = WebhookEndpoint(
            name=body.name,
            url=body.url,
            event_filters=body.event_filters,
            signing_secret=encrypted,
        )
        db.add(ep)
        await audit(
            db, action="webhook.create", severity="warn",
            actor_email=current_user.email,
            target_type="webhook", target_name=body.name,
            detail={"url": body.url, "event_filters": body.event_filters},
        )
        await db.commit()
        await db.refresh(ep)
        return WebhookResponse.model_validate(ep)


@router.put("/{webhook_id}", response_model=WebhookResponse)
async def update_webhook(
    webhook_id: uuid.UUID,
    body: WebhookUpdate,
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> WebhookResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        ep = await db.get(WebhookEndpoint, webhook_id)
        if ep is None:
            raise HTTPException(status_code=404, detail="Webhook not found")
        if body.event_filters is not None:
            _validate_event_filters(body.event_filters)
        if body.url is not None:
            _validate_url(body.url)
        updates = body.model_dump(exclude_unset=True)
        for key, value in updates.items():
            setattr(ep, key, value)
        await audit(
            db, action="webhook.update", severity="warn",
            actor_email=current_user.email,
            target_type="webhook", target_id=ep.id, target_name=ep.name,
            detail={"fields": list(updates.keys())},
        )
        await db.commit()
        await db.refresh(ep)
        return WebhookResponse.model_validate(ep)


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
        await audit(
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
        return DeliveryResponse.model_validate(delivery)


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
        await audit(
            db, action="webhook.secret_rotated", severity="critical",
            actor_email=current_user.email,
            target_type="webhook", target_id=ep.id, target_name=ep.name,
        )
        await db.commit()
        return {"signing_secret": plaintext}


@router.get("/{webhook_id}/deliveries", response_model=list[DeliveryResponse])
async def list_deliveries(
    webhook_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0,
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> list[DeliveryResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(
            select(WebhookDelivery)
            .where(WebhookDelivery.endpoint_id == webhook_id)
            .order_by(WebhookDelivery.created_at.desc())
            .limit(limit).offset(offset)
        )
        return [DeliveryResponse.model_validate(d) for d in result.scalars().all()]


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
    limit: int = 50,
    offset: int = 0,
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> list[DeliveryResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(
            select(WebhookDelivery)
            .where(WebhookDelivery.status == "dlq")
            .order_by(WebhookDelivery.created_at.desc())
            .limit(limit).offset(offset)
        )
        return [DeliveryResponse.model_validate(d) for d in result.scalars().all()]


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

        # F-022-13: a manual DLQ retry is a single re-delivery of the *same*
        # row — one attempt, no inline backoff that would hang the admin
        # request for minutes. The attempt counter is reset so the row gets a
        # fresh backoff schedule; on failure the scheduler drain job continues
        # the retries asynchronously (it never sleeps in this request).
        delivery.status = "pending"
        delivery.attempts = 0
        delivery.error_message = None
        delivery.next_attempt_at = None

        url, body_bytes, sig_header = rebuild_signed_body(ep, delivery)
        await attempt_delivery(delivery, url, body_bytes, sig_header)
        await db.commit()
        await db.refresh(delivery)
        return DeliveryResponse.model_validate(delivery)


@router.delete("/dlq/{delivery_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dlq(
    delivery_id: uuid.UUID,
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        delivery = await db.get(WebhookDelivery, delivery_id)
        if delivery is None or delivery.status != "dlq":
            raise HTTPException(status_code=404, detail="DLQ entry not found")
        await db.delete(delivery)
        await db.commit()
