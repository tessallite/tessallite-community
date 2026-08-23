"""Notification route CRUD and test-send endpoints."""
from __future__ import annotations

import logging
from uuid import UUID

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import or_

from shared.alerting.dispatcher import EVENT_TYPES
from shared.audit.logger import audit, audit_required
from shared.config.bootstrap import system_snapshot_get
from shared.db.models import NotificationDelivery, NotificationRoute
from shared.db.session import get_tenant_db
from shared.middleware.action_throttle import consume_action_quota
from shared.schemas.pydantic_models import (
    NotificationRouteCreate,
    NotificationRouteResponse,
    NotificationRouteUpdate,
)
from shared.security.credential_crypto import encrypt_str, decrypt_str
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

router = APIRouter(
    prefix="/projects/{project_id}/notifications",
    tags=["notifications"],
)

_VALID_CHANNELS = {"email", "slack"}


# ---------------------------------------------------------------------------
# Bug-5945: Slack webhook URL encryption helpers
# ---------------------------------------------------------------------------

def _encrypt_channel_config(channel_type: str | None, config: dict | None) -> dict | None:
    """Encrypt secrets inside channel_config before persisting to DB.

    For Slack routes, the ``webhook_url`` is a bearer secret. We store it
    Fernet-encrypted under ``webhook_url_encrypted`` and set a
    ``has_webhook_url`` flag. The plaintext ``webhook_url`` key is removed
    from the stored dict so the secret is never at rest in plain JSON.
    """
    if config is None or channel_type != "slack":
        return config
    config = dict(config)  # shallow copy to avoid mutating caller's dict
    webhook_url = config.pop("webhook_url", None)
    if webhook_url:
        config["webhook_url_encrypted"] = encrypt_str(webhook_url).decode("utf-8")
        config["has_webhook_url"] = True
    return config


def _decrypt_webhook_url(config: dict) -> str | None:
    """Recover the plaintext Slack webhook URL from an encrypted config.

    Returns None if no encrypted value is stored.
    """
    encrypted = config.get("webhook_url_encrypted")
    if not encrypted:
        # Legacy: if the route was saved before Bug-5945, the URL may still
        # be in plaintext. Return it directly so existing routes keep working.
        return config.get("webhook_url")
    try:
        return decrypt_str(encrypted.encode("utf-8") if isinstance(encrypted, str) else encrypted)
    except Exception:
        logger.warning("Failed to decrypt Slack webhook URL — returning None")
        return None


def _redact_channel_config(channel_type: str | None, config: dict | None) -> dict:
    """Return a safe-for-API copy of channel_config with secrets redacted.

    For Slack routes: removes ``webhook_url`` and ``webhook_url_encrypted``,
    exposes only ``has_webhook_url: true`` so the frontend knows a URL is set.
    """
    if config is None:
        return {}
    config = dict(config)
    if channel_type == "slack":
        # Bug-6001: check before popping so legacy plaintext routes correctly
        # report has_webhook_url=true even when the persisted config still
        # stores the secret under ``webhook_url`` or ``webhook_url_encrypted``.
        has_url = bool(
            config.get("has_webhook_url")
            or config.get("webhook_url_encrypted")
            or config.get("webhook_url")
        )
        config.pop("webhook_url", None)
        config.pop("webhook_url_encrypted", None)
        config["has_webhook_url"] = has_url
    return config


def _route_to_response(route: NotificationRoute) -> NotificationRouteResponse:
    """Build a NotificationRouteResponse with secrets redacted (Bug-5945)."""
    data = {
        "id": route.id,
        "project_id": route.project_id,
        "event_type": route.event_type,
        "channel_type": route.channel_type,
        "channel_config": _redact_channel_config(route.channel_type, route.channel_config),
        "enabled": route.enabled,
        "created_at": route.created_at,
        "updated_at": route.updated_at,
    }
    return NotificationRouteResponse.model_validate(data)


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
    # Bug-8114: pocket refresh failures are a distinct event type from
    # aggregate "Refresh Failure" (separate acceleration-asset lifecycle).
    "pocket_refresh_failure": "Pocket Refresh Failure",
}


def _validate_channel_config(
    channel_type: str | None,
    channel_config: dict | None,
    *,
    existing_channel_config: dict | None = None,
) -> None:
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
            # Bug-5999: a blank webhook_url on an update means "keep the
            # already-configured secret" -- the API never returns the
            # plaintext URL for editing (Bug-5945), so the frontend cannot
            # resupply it. Only require a fresh URL when nothing is stored.
            existing = existing_channel_config or {}
            if existing.get("webhook_url_encrypted") or existing.get("webhook_url"):
                return
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
    existing_channel_config: dict | None = None,
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
    #
    # Bug-6016 (found on Bug-5999 re-review): the mirror case was still
    # bypassed -- a PUT that changes channel_type WITHOUT resending
    # channel_config validated nothing at all, because the check below only
    # ran when channel_config was provided. But the update loop leaves
    # route.channel_config untouched when it's omitted, so the route would
    # end up with the OLD channel_type's config paired with the NEW
    # channel_type (e.g. a slack-typed route still carrying
    # {"recipients": [...]}) -- silently dropped by the dispatcher at send
    # time instead of rejected at save time. Validate whenever EITHER field
    # is present in the request, using the config that will actually govern
    # once this update applies (the new one if supplied, else the persisted
    # one).
    channel_config = getattr(body, "channel_config", None)
    effective_channel_type = body.channel_type if body.channel_type is not None else persisted_channel_type
    effective_channel_config = channel_config if channel_config is not None else existing_channel_config
    if effective_channel_type is not None and (
        body.channel_type is not None or channel_config is not None
    ):
        _validate_channel_config(
            effective_channel_type,
            effective_channel_config,
            existing_channel_config=existing_channel_config,
        )


def _prepare_channel_config_for_update(
    channel_type: str | None,
    new_config: dict,
    existing_config: dict | None,
) -> dict | None:
    """Bug-5999: preserve an already-configured Slack secret on edit.

    ``_validate_route`` already allowed a blank ``webhook_url`` through when
    a secret is already stored. Mirror that here: if the caller supplied a
    fresh URL, encrypt and store it as usual; otherwise carry the existing
    encrypted value forward instead of overwriting ``channel_config`` with a
    dict that has no secret in it at all.
    """
    if channel_type != "slack":
        return _encrypt_channel_config(channel_type, new_config)

    webhook_url = (new_config.get("webhook_url") or "").strip()
    if webhook_url:
        return _encrypt_channel_config(channel_type, new_config)

    existing = existing_config or {}
    existing_encrypted = existing.get("webhook_url_encrypted")
    existing_plain = existing.get("webhook_url")
    merged = dict(new_config)
    merged.pop("webhook_url", None)
    if existing_encrypted:
        merged["webhook_url_encrypted"] = existing_encrypted
        merged["has_webhook_url"] = True
    elif existing_plain:
        merged["webhook_url_encrypted"] = encrypt_str(existing_plain).decode("utf-8")
        merged["has_webhook_url"] = True
    # If neither is present, _validate_route already rejected this request
    # with a 422 before we get here.
    return merged


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

    Bug-8862 — deliberate absence of a resource-ownership guard. Every other
    handler flagged by that scan reads or mutates a nested resource, so it must
    prove the project -> model -> resource chain. This one does not: the body
    touches no session and no project-scoped row, and returns the process-wide
    ``EVENT_TYPES`` / ``_EVENT_LABELS`` constants, byte-identical for every
    project and every tenant.

    On the unreferenced ``project_id`` parameter, stated precisely so this is
    not re-litigated on a false premise: the path segment exists because of the
    router prefix, and ``require_role`` reads it from the path through its OWN
    ``project_id`` parameter (``src/auth/rbac.py`` ``_dependency``), which
    FastAPI resolves independently of this handler's signature. Removing the
    parameter here would therefore NOT weaken the role gate; it is kept only
    for signature symmetry with the sibling handlers in this router. The reason
    no ownership guard applies is simply that the path names no second
    resource to bind the project to — no ``model_id``, no ``route_id``. Adding
    an existence query here would be a guard that can never fire; do not
    "complete" the enumeration with one.
    """
    return [
        {"value": name, "label": _EVENT_LABELS.get(name, name)}
        for name in sorted(EVENT_TYPES)
    ]


class NotificationDeliveryResponse(BaseModel):
    """Operator-visible record of one email/Slack delivery attempt (Bug-8053).

    ``target`` is a non-secret destination hint (joined recipients for email, a
    hashed webhook URL for Slack) — a plaintext Slack secret never appears here.
    ``status`` is ``sent`` for a genuine successful send or ``failed`` for a
    failed send or misconfiguration skip.
    """

    id: UUID
    route_id: UUID | None = None
    project_id: UUID | None = None
    event_type: str
    channel_type: str
    target: str | None = None
    status: str
    error_message: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


@router.get("/deliveries", response_model=list[NotificationDeliveryResponse])
async def list_notification_deliveries(
    project_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("modeler"),
) -> list[NotificationDeliveryResponse]:
    """Durable, operator-visible email/Slack delivery outcomes (Bug-8053).

    Before this endpoint a failed email/Slack notification left only an
    application-log line, invisible in the product. This surfaces the persisted
    delivery records — including failures — so an operator can see, triage, and
    prove them. Project-scoped rows and tenant-global rows (``project_id`` NULL,
    which cover this project via the dispatcher's tenant-global fallback) are
    both returned, newest first.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(
            select(NotificationDelivery)
            .where(
                or_(
                    NotificationDelivery.project_id == project_id,
                    NotificationDelivery.project_id.is_(None),
                )
            )
            .order_by(NotificationDelivery.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [
            NotificationDeliveryResponse.model_validate(d)
            for d in result.scalars().all()
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
        # Bug-5945: redact Slack webhook URLs from API responses.
        return [_route_to_response(r) for r in result.scalars().all()]


@router.post("", response_model=NotificationRouteResponse, status_code=201)
async def create_notification_route(
    project_id: UUID,
    body: NotificationRouteCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("modeler"),
) -> NotificationRouteResponse:
    _validate_route(body)
    async for db in get_tenant_db(current_user.tenant_id):
        # Bug-5945: encrypt Slack webhook URL before persisting.
        stored_config = _encrypt_channel_config(body.channel_type, body.channel_config)
        route = NotificationRoute(
            project_id=project_id,
            event_type=body.event_type,
            channel_type=body.channel_type,
            channel_config=stored_config,
            enabled=body.enabled,
        )
        db.add(route)
        await db.flush()
        await audit_required(
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
        return _route_to_response(route)


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
        # Bug-5999: also pass the persisted channel_config so a blank
        # webhook_url is recognised as "keep the existing secret" rather than
        # rejected outright.
        _validate_route(
            body,
            persisted_channel_type=route.channel_type,
            existing_channel_config=route.channel_config,
        )

        changed_fields: list[str] = []
        for field in ("event_type", "channel_type", "channel_config", "enabled"):
            val = getattr(body, field, None)
            if val is not None:
                if field == "channel_config":
                    # Bug-5945/5999: encrypt Slack secrets on update, or carry
                    # the existing encrypted secret forward if the caller left
                    # the URL blank.
                    effective_channel = body.channel_type if body.channel_type is not None else route.channel_type
                    val = _prepare_channel_config_for_update(effective_channel, val, route.channel_config)
                setattr(route, field, val)
                changed_fields.append(field)

        await audit_required(
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
        return _route_to_response(route)


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
        await audit_required(
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


def _int_setting(key: str, fallback: int) -> int:
    try:
        return int(system_snapshot_get(key))
    except (TypeError, ValueError):
        return fallback


def _recipient_domain_allowlist() -> list[str]:
    raw = system_snapshot_get("notifications.recipient_domain_allowlist")
    if not isinstance(raw, str):
        return []
    return [d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()]


def _check_recipients(recipients: list) -> None:
    """Bound who a test alert may be addressed to (Bug-6324).

    A test-send puts arbitrary addresses behind the PLATFORM's verified SMTP
    identity, so the recipient list is attacker-chosen input, not a display
    field: bound its size, and honour an operator domain allowlist when one is
    configured.
    """
    max_recipients = _int_setting("notifications.test_max_recipients", 5)
    if len(recipients) > max_recipients:
        raise HTTPException(
            status_code=422,
            detail=(
                f"A test alert may be sent to at most {max_recipients} "
                f"recipients at a time ({len(recipients)} supplied)."
            ),
        )

    allowlist = _recipient_domain_allowlist()
    if not allowlist:
        return
    for address in recipients:
        domain = str(address).rsplit("@", 1)[-1].strip().lower()
        # A subdomain of an allowed domain is allowed; a lookalike is not
        # (``evil-example.com`` must not match ``example.com``).
        if not any(
            domain == allowed or domain.endswith("." + allowed)
            for allowed in allowlist
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Recipient domain '{domain}' is not in this platform's "
                    "allowed notification domains."
                ),
            )


def _enforce_test_send_quota(project_id: UUID, tenant_id: str) -> None:
    """Bound how often test alerts may be sent (Bug-6324).

    Without this, any project modeler can drive unlimited mail through the
    platform's shared SMTP sender to addresses of their choosing — an open
    relay whose reputational cost lands on every tenant, not just the abusing
    one.

    Three ceilings, because the harm is shared and the smallest bucket does not
    bound it (R2 finding 11): a per-PROJECT ceiling alone is multiplied by
    however many projects an admin chooses to create, and a per-TENANT ceiling
    is multiplied by however many tenants exist. Sender reputation is
    platform-wide, so the outermost bucket has to be too. Every ceiling is
    config-driven; 0 disables that level, and 0 on the project ceiling switches
    test-sends off entirely.
    """
    per_hour = _int_setting("notifications.test_send_per_hour", 10)
    if per_hour <= 0:
        raise HTTPException(
            status_code=403,
            detail="Notification test-sends are disabled on this platform.",
        )

    buckets: list[tuple[str, tuple[str, ...], int, str]] = [
        ("project", (str(project_id),), per_hour, "This project"),
        (
            "tenant", (str(tenant_id),),
            _int_setting("notifications.test_send_per_hour_tenant", 30),
            "This tenant",
        ),
        (
            "platform", ("all",),
            _int_setting("notifications.test_send_per_hour_platform", 200),
            "The platform",
        ),
    ]
    for scope, key, ceiling, subject in buckets:
        if ceiling <= 0:
            continue  # this level is switched off
        if consume_action_quota(
            f"notification.test_send.{scope}", *key, limit=f"{ceiling}/hour",
        ):
            continue
        raise HTTPException(
            status_code=429,
            detail=(
                f"{subject} has reached its limit of {ceiling} notification "
                "test-sends per hour. Try again later."
            ),
            headers={"Retry-After": "3600"},
        )


async def _audit_test_send(
    project_id: UUID, current_user: CurrentUser, channel_type: str,
    recipients: list, route_id: UUID | None = None, db=None,
) -> None:
    """Attribute every test-send (Bug-6324).

    The throttle bounds the damage; the audit record makes an abusing account
    identifiable. Recipients are recorded because "who did this account mail"
    is the whole question during an SMTP-reputation incident.

    Pass ``db`` when the caller already holds a tenant session so the record
    is written on that session rather than opening a nested one.
    """
    detail = {
        "project_id": str(project_id),
        "channel_type": channel_type,
        "recipients": [str(r) for r in recipients],
        "recipient_count": len(recipients),
    }

    async def _write(session) -> None:
        await audit(
            session,
            action="notification_route.test_send",
            severity="warn",
            actor_email=current_user.email,
            target_type="notification_route",
            target_id=route_id,
            detail=detail,
        )
        await session.commit()

    if db is not None:
        await _write(db)
        return
    async for session in get_tenant_db(current_user.tenant_id):
        await _write(session)


async def _send_test_notification(channel_type: str, channel_config: dict) -> dict:
    """Send a one-off test alert for the given channel and config.

    Shared by ``POST /test`` (draft config, not yet saved) and
    ``POST /{route_id}/test`` (Bug-5999: a persisted route, whose secret is
    decrypted by the caller before this is invoked).

    Callers MUST first pass ``_enforce_test_send_quota`` and (for email)
    ``_check_recipients`` — this function performs delivery only.
    """
    subject = "Test Alert"
    html = (
        "<h2>Tessallite Test Alert</h2>"
        "<p>This is a test notification. If you received this, "
        "your alert channel is configured correctly.</p>"
    )
    text = "Tessallite Test Alert - your alert channel is configured correctly."

    from shared.alerting.smtp_sender import SmtpNotConfiguredError

    try:
        if channel_type == "email":
            from shared.alerting.smtp_sender import send_email
            recipients = channel_config.get("recipients", [])
            if not recipients:
                raise HTTPException(status_code=422, detail="No recipients configured")
            await send_email(to=recipients, subject=f"[Tessallite] {subject}", body_html=html, body_text=text)

        elif channel_type == "slack":
            from shared.alerting.slack_sender import send_slack
            webhook_url = channel_config.get("webhook_url", "")
            if not webhook_url:
                raise HTTPException(status_code=422, detail="No webhook URL configured")
            await send_slack(webhook_url=webhook_url, text=text)

        return {"status": "sent"}
    except HTTPException:
        raise
    except SmtpNotConfiguredError:
        # Bug-7340: surface a clear 503 so the admin knows SMTP is not set up.
        # Never report "sent" when no email was actually delivered.
        raise HTTPException(
            status_code=503,
            detail="SMTP is not configured. Set SMTP_HOST in the server "
                   "environment to enable email notifications.",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Test notification failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Notification delivery failed. Check server logs for details.",
        ) from exc


@router.post("/test", status_code=200)
async def test_notification(
    project_id: UUID,
    body: NotificationRouteCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("modeler"),
) -> dict:
    """Test-send a draft channel config that has not been saved as a route yet.

    The caller supplies the plaintext ``webhook_url``/``recipients`` directly
    in the request body (nothing has been persisted or encrypted).
    """
    _validate_route(body)
    recipients = list((body.channel_config or {}).get("recipients") or [])
    if body.channel_type == "email":
        _check_recipients(recipients)
    # Bug-6324: quota BEFORE delivery, and count the attempt whether or not
    # SMTP ultimately accepts it — otherwise a failing-but-still-dispatched
    # send is free and the ceiling means nothing.
    _enforce_test_send_quota(project_id, current_user.tenant_id)
    await _audit_test_send(project_id, current_user, body.channel_type, recipients)
    return await _send_test_notification(body.channel_type, body.channel_config)


@router.post("/{route_id}/test", status_code=200)
async def test_notification_route(
    project_id: UUID,
    route_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("modeler"),
) -> dict:
    """Test-send an already-saved route (Bug-5999).

    ``GET``/``PUT`` responses never expose a Slack route's plaintext webhook
    URL (Bug-5945), so the frontend cannot resupply it for a test-send. This
    endpoint decrypts the stored secret server-side instead.
    """
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

        channel_config = dict(route.channel_config or {})
        recipients = list(channel_config.get("recipients") or [])
        if route.channel_type == "slack":
            webhook_url = _decrypt_webhook_url(channel_config)
            if not webhook_url:
                raise HTTPException(status_code=422, detail="No webhook URL configured")
            channel_config = {"webhook_url": webhook_url}
        else:
            # Bug-6324: a saved route's recipients are just as operator-editable
            # as a draft's, so the same bounds apply on this path.
            _check_recipients(recipients)

        # Bug-6324: same project quota as the draft path — a modeler must not
        # be able to sidestep the ceiling by saving the route first.
        _enforce_test_send_quota(project_id, current_user.tenant_id)
        await _audit_test_send(
            project_id, current_user, route.channel_type, recipients,
            route_id=route_id, db=db,
        )
        return await _send_test_notification(route.channel_type, channel_config)
