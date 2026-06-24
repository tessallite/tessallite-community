"""Notification route CRUD and test-send endpoints."""
from __future__ import annotations

import logging
from uuid import UUID

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from shared.alerting.dispatcher import EVENT_TYPES
from shared.audit.logger import audit
from shared.db.models import NotificationRoute
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    NotificationRouteCreate,
    NotificationRouteResponse,
    NotificationRouteUpdate,
)
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

router = APIRouter(
    prefix="/projects/{project_id}/notifications",
    tags=["notifications"],
)

_VALID_CHANNELS = {"email", "slack"}


def _audit_detail(
    project_id: UUID,
    route: NotificationRoute,
    *,
    fields: list[str] | None = None,
) -> dict:
    """Build the audit detail payload for a notification-route mutation.

    Records the project, event/channel identity and enabled state — never the
    channel_config (it can hold a Slack webhook secret), so the audit log
    stays free of credentials.
    """
    detail = {
        "project_id": str(project_id),
        "event_type": route.event_type,
        "channel_type": route.channel_type,
        "enabled": route.enabled,
    }
    if fields is not None:
        detail["fields"] = fields
    return detail


# Human labels for the notification event vocabulary. Kept here next to the
# backend EVENT_TYPES frozenset so the picker the frontend renders is exactly
# the set the dispatcher recognises (no KPI events the API would 422 on).
_EVENT_LABELS: dict[str, str] = {
    "refresh_failure": "Refresh Failure",
    "schema_drift": "Schema Drift",
    "sla_breach": "SLA Breach",
    "query_failure_spike": "Query Failure Spike",
    "aggregate_retired": "Aggregate Retired",
    "refresh_upstream_failed": "Upstream Refresh Failed",
}


def _validate_channel_config(channel_type: str | None, channel_config: dict | None) -> None:
    """F-022-12: validate the channel payload at write time, not only at send.

    A route saved with no recipients or a non-Slack webhook URL would otherwise
    appear "enabled" but be silently skipped by the dispatcher when an event
    fires. Failing here surfaces the error at save time.
    """
    if channel_type is None:
        return
    config = channel_config or {}
    if channel_type == "email":
        recipients = config.get("recipients") or []
        if not isinstance(recipients, list) or not recipients:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Email route requires a non-empty 'recipients' list.",
            )
    elif channel_type == "slack":
        from shared.alerting.slack_sender import validate_webhook_url
        webhook_url = config.get("webhook_url") or ""
        if not webhook_url:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Slack route requires a 'webhook_url'.",
            )
        try:
            validate_webhook_url(webhook_url)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid Slack webhook URL: {exc}",
            )


def _validate_route(
    body: NotificationRouteCreate | NotificationRouteUpdate,
    *,
    persisted_channel_type: str | None = None,
) -> None:
    if body.event_type is not None and body.event_type not in EVENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid event_type. Must be one of: {sorted(EVENT_TYPES)}",
        )
    if body.channel_type is not None and body.channel_type not in _VALID_CHANNELS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid channel_type. Must be one of: {sorted(_VALID_CHANNELS)}",
        )
    # Bug-5277: validate channel_config against the effective channel_type.
    # On a partial update the body may carry a new channel_config without
    # channel_type; fall back to the persisted channel_type so validation
    # is never bypassed.
    channel_config = getattr(body, "channel_config", None)
    effective_channel_type = body.channel_type if body.channel_type is not None else persisted_channel_type
    if effective_channel_type is not None and channel_config is not None:
        _validate_channel_config(effective_channel_type, channel_config)


@router.get("/event-types")
async def list_notification_event_types(
    project_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("modeler"),
) -> list[dict[str, str]]:
    """Catalogue of event types the dispatcher recognises.

    The frontend Alerts panel fetches this instead of hard-coding a list,
    so it can never offer an event the API would reject with 422. Keys are
    exactly the backend ``EVENT_TYPES`` frozenset.
    """
    return [
        {"value": name, "label": _EVENT_LABELS.get(name, name)}
        for name in sorted(EVENT_TYPES)
    ]


@router.get("", response_model=list[NotificationRouteResponse])
async def list_notification_routes(
    project_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("modeler"),
) -> list[NotificationRouteResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(
            select(NotificationRoute)
            .where(NotificationRoute.project_id == project_id)
            .order_by(NotificationRoute.created_at.desc())
        )
        return [NotificationRouteResponse.model_validate(r) for r in result.scalars().all()]


@router.post("", response_model=NotificationRouteResponse, status_code=201)
async def create_notification_route(
    project_id: UUID,
    body: NotificationRouteCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("modeler"),
) -> NotificationRouteResponse:
    _validate_route(body)
    async for db in get_tenant_db(current_user.tenant_id):
        route = NotificationRoute(
            project_id=project_id,
            event_type=body.event_type,
            channel_type=body.channel_type,
            channel_config=body.channel_config,
            enabled=body.enabled,
        )
        db.add(route)
        await db.flush()
        await audit(
            db,
            action="notification_route.create",
            severity="warn",
            actor_email=current_user.email,
            target_type="notification_route",
            target_id=route.id,
            detail=_audit_detail(project_id, route),
        )
        await db.commit()
        await db.refresh(route)
        return NotificationRouteResponse.model_validate(route)


@router.put("/{route_id}", response_model=NotificationRouteResponse)
async def update_notification_route(
    project_id: UUID,
    route_id: UUID,
    body: NotificationRouteUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("modeler"),
) -> NotificationRouteResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(
            select(NotificationRoute).where(
                NotificationRoute.id == route_id,
                NotificationRoute.project_id == project_id,
            )
        )
        route = result.scalar_one_or_none()
        if not route:
            raise HTTPException(status_code=404, detail="Notification route not found")

        # Bug-5277: pass the persisted channel_type so a partial update that
        # sends channel_config without channel_type still validates the config.
        _validate_route(body, persisted_channel_type=route.channel_type)

        changed_fields: list[str] = []
        for field in ("event_type", "channel_type", "channel_config", "enabled"):
            val = getattr(body, field, None)
            if val is not None:
                setattr(route, field, val)
                changed_fields.append(field)

        await audit(
            db,
            action="notification_route.update",
            severity="warn",
            actor_email=current_user.email,
            target_type="notification_route",
            target_id=route.id,
            detail=_audit_detail(project_id, route, fields=changed_fields),
        )
        await db.commit()
        await db.refresh(route)
        return NotificationRouteResponse.model_validate(route)


@router.delete("/{route_id}", status_code=204)
async def delete_notification_route(
    project_id: UUID,
    route_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("modeler"),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(
            select(NotificationRoute).where(
                NotificationRoute.id == route_id,
                NotificationRoute.project_id == project_id,
            )
        )
        route = result.scalar_one_or_none()
        if not route:
            raise HTTPException(status_code=404, detail="Notification route not found")
        await audit(
            db,
            action="notification_route.delete",
            severity="warn",
            actor_email=current_user.email,
            target_type="notification_route",
            target_id=route.id,
            detail=_audit_detail(project_id, route),
        )
        await db.delete(route)
        await db.commit()


@router.post("/test", status_code=200)
async def test_notification(
    project_id: UUID,
    body: NotificationRouteCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("modeler"),
) -> dict:
    _validate_route(body)

    subject = "Test Alert"
    html = (
        "<h2>Tessallite Test Alert</h2>"
        "<p>This is a test notification. If you received this, "
        "your alert channel is configured correctly.</p>"
    )
    text = "Tessallite Test Alert - your alert channel is configured correctly."

    try:
        if body.channel_type == "email":
            from shared.alerting.smtp_sender import send_email
            recipients = body.channel_config.get("recipients", [])
            if not recipients:
                raise HTTPException(status_code=422, detail="No recipients configured")
            await send_email(to=recipients, subject=f"[Tessallite] {subject}", body_html=html, body_text=text)

        elif body.channel_type == "slack":
            from shared.alerting.slack_sender import send_slack
            webhook_url = body.channel_config.get("webhook_url", "")
            if not webhook_url:
                raise HTTPException(status_code=422, detail="No webhook URL configured")
            await send_slack(webhook_url=webhook_url, text=text)

        return {"status": "sent"}
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Test notification failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Notification delivery failed. Check server logs for details.",
        ) from exc
