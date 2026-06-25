"""Tests for notification route API endpoints."""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from src.main import app
from tests.conftest import (
    TEST_PROJECT_ID,
    NOW,
    async_gen_from,
    make_mock_db,
)


def _make_route(
    *,
    project_id: uuid.UUID = TEST_PROJECT_ID,
    event_type: str = "schema_drift",
    channel_type: str = "email",
    channel_config: dict | None = None,
    enabled: bool = True,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        project_id=project_id,
        event_type=event_type,
        channel_type=channel_type,
        channel_config=channel_config or {"recipients": ["admin@test.com"]},
        enabled=enabled,
        created_at=NOW,
        updated_at=NOW,
    )


def _mock_db_with_routes(routes: list):
    db = make_mock_db()
    result = MagicMock()
    result.scalars.return_value.all.return_value = routes
    result.scalar_one_or_none.return_value = routes[0] if routes else None
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.fixture
def url():
    return f"/api/v1/projects/{TEST_PROJECT_ID}/notifications"


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_notification_routes(client, url):
    routes = [_make_route(), _make_route(event_type="sla_breach")]
    db = _mock_db_with_routes(routes)
    with patch("src.api.notifications.get_tenant_db", async_gen_from(db)):
        resp = await client.get(url)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2


@pytest.mark.asyncio
async def test_list_notification_routes_empty(client, url):
    db = _mock_db_with_routes([])
    with patch("src.api.notifications.get_tenant_db", async_gen_from(db)):
        resp = await client.get(url)
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_notification_route(client, url):
    route = _make_route()
    db = make_mock_db()

    async def fake_refresh(obj):
        obj.id = route.id
        obj.created_at = route.created_at
        obj.updated_at = route.updated_at

    db.refresh = AsyncMock(side_effect=fake_refresh)

    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("src.api.notifications.audit", new_callable=AsyncMock) as mock_audit,
    ):
        resp = await client.post(url, json={
            "event_type": "schema_drift",
            "channel_type": "email",
            "channel_config": {"recipients": ["admin@test.com"]},
        })
    assert resp.status_code == 201
    mock_audit.assert_awaited_once()
    call_kwargs = mock_audit.call_args.kwargs
    assert call_kwargs["action"] == "notification_route.create"
    assert call_kwargs["severity"] == "warn"
    assert call_kwargs["actor_email"] == "user@example.com"
    assert call_kwargs["target_type"] == "notification_route"
    assert call_kwargs["detail"] == {
        "project_id": str(TEST_PROJECT_ID),
        "event_type": "schema_drift",
        "channel_type": "email",
        "enabled": True,
    }


@pytest.mark.asyncio
async def test_create_invalid_event_type(client, url):
    db = make_mock_db()
    with patch("src.api.notifications.get_tenant_db", async_gen_from(db)):
        resp = await client.post(url, json={
            "event_type": "invalid_type",
            "channel_type": "email",
            "channel_config": {"recipients": ["a@b.com"]},
        })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_invalid_channel_type(client, url):
    db = make_mock_db()
    with patch("src.api.notifications.get_tenant_db", async_gen_from(db)):
        resp = await client.post(url, json={
            "event_type": "schema_drift",
            "channel_type": "telegram",
            "channel_config": {},
        })
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Event catalogue parity (F-022-02): the UI offers exactly what the API
# accepts — no KPI events the backend would 422 on.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_event_types_catalogue_matches_backend(client, url):
    from shared.alerting.dispatcher import EVENT_TYPES
    resp = await client.get(f"{url}/event-types")
    assert resp.status_code == 200
    values = {e["value"] for e in resp.json()}
    assert values == set(EVENT_TYPES)


@pytest.mark.asyncio
async def test_catalogue_offers_no_kpi_events(client, url):
    resp = await client.get(f"{url}/event-types")
    values = {e["value"] for e in resp.json()}
    for kpi in ("kpi_threshold_breach", "kpi_status_change", "kpi_trend_alert"):
        assert kpi not in values


@pytest.mark.parametrize(
    "kpi_event",
    ["kpi_threshold_breach", "kpi_status_change", "kpi_trend_alert"],
)
@pytest.mark.asyncio
async def test_kpi_events_are_rejected(client, url, kpi_event):
    """A KPI event the UI used to offer must 422 — and is no longer offered,
    so the picker can never produce this request."""
    db = make_mock_db()
    with patch("src.api.notifications.get_tenant_db", async_gen_from(db)):
        resp = await client.post(url, json={
            "event_type": kpi_event,
            "channel_type": "email",
            "channel_config": {"recipients": ["a@b.com"]},
        })
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_notification_route_emits_audit(client, url):
    route = _make_route()
    db = _mock_db_with_routes([route])

    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("src.api.notifications.audit", new_callable=AsyncMock) as mock_audit,
    ):
        resp = await client.put(f"{url}/{route.id}", json={
            "channel_type": "slack",
            "channel_config": {"webhook_url": "https://hooks.slack.com/test"},
            "enabled": False,
        })
    assert resp.status_code == 200
    mock_audit.assert_awaited_once()
    call_kwargs = mock_audit.call_args.kwargs
    assert call_kwargs["action"] == "notification_route.update"
    assert call_kwargs["severity"] == "warn"
    assert call_kwargs["target_id"] == route.id
    assert call_kwargs["detail"]["project_id"] == str(TEST_PROJECT_ID)
    assert call_kwargs["detail"]["channel_type"] == "slack"
    assert call_kwargs["detail"]["enabled"] is False
    assert call_kwargs["detail"]["fields"] == [
        "channel_type",
        "channel_config",
        "enabled",
    ]


# ---------------------------------------------------------------------------
# Secret hygiene: channel_config (which can hold a Slack webhook secret) must
# never appear in the emitted audit detail — on create, update, or delete.
# ---------------------------------------------------------------------------

_SECRET_WEBHOOK = "https://hooks.slack.com/services/T000/B000/sUpErSeCrEtToKeN"


def _assert_no_secret_in_detail(detail: dict) -> None:
    """The whole audit detail blob must not carry channel_config or its value."""
    assert "channel_config" not in detail
    blob = repr(detail)
    assert _SECRET_WEBHOOK not in blob
    assert "sUpErSeCrEtToKeN" not in blob


@pytest.mark.asyncio
async def test_create_audit_excludes_channel_config_secret(client, url):
    route = _make_route(
        channel_type="slack",
        channel_config={"webhook_url": _SECRET_WEBHOOK},
    )
    db = make_mock_db()

    async def fake_refresh(obj):
        obj.id = route.id
        obj.created_at = route.created_at
        obj.updated_at = route.updated_at

    db.refresh = AsyncMock(side_effect=fake_refresh)

    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("src.api.notifications.audit", new_callable=AsyncMock) as mock_audit,
    ):
        resp = await client.post(url, json={
            "event_type": "schema_drift",
            "channel_type": "slack",
            "channel_config": {"webhook_url": _SECRET_WEBHOOK},
        })
    assert resp.status_code == 201
    mock_audit.assert_awaited_once()
    _assert_no_secret_in_detail(mock_audit.call_args.kwargs["detail"])


@pytest.mark.asyncio
async def test_update_audit_excludes_channel_config_secret(client, url):
    route = _make_route()
    db = _mock_db_with_routes([route])

    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("src.api.notifications.audit", new_callable=AsyncMock) as mock_audit,
    ):
        resp = await client.put(f"{url}/{route.id}", json={
            "channel_type": "slack",
            "channel_config": {"webhook_url": _SECRET_WEBHOOK},
            "enabled": True,
        })
    assert resp.status_code == 200
    mock_audit.assert_awaited_once()
    detail = mock_audit.call_args.kwargs["detail"]
    # ``channel_config`` is named in the changed-fields list (the fact that it
    # changed is auditable) but its VALUE must never be recorded.
    assert "channel_config" in detail.get("fields", [])
    _assert_no_secret_in_detail(detail)


@pytest.mark.asyncio
async def test_delete_audit_excludes_channel_config_secret(client, url):
    route = _make_route(
        channel_type="slack",
        channel_config={"webhook_url": _SECRET_WEBHOOK},
    )
    db = _mock_db_with_routes([route])
    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("src.api.notifications.audit", new_callable=AsyncMock) as mock_audit,
    ):
        resp = await client.delete(f"{url}/{route.id}")
    assert resp.status_code == 204
    mock_audit.assert_awaited_once()
    _assert_no_secret_in_detail(mock_audit.call_args.kwargs["detail"])


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_notification_route(client, url):
    route = _make_route()
    db = _mock_db_with_routes([route])
    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("src.api.notifications.audit", new_callable=AsyncMock) as mock_audit,
    ):
        resp = await client.delete(f"{url}/{route.id}")
    assert resp.status_code == 204
    mock_audit.assert_awaited_once()
    call_kwargs = mock_audit.call_args.kwargs
    assert call_kwargs["action"] == "notification_route.delete"
    assert call_kwargs["severity"] == "warn"
    assert call_kwargs["target_id"] == route.id
    assert call_kwargs["detail"]["project_id"] == str(TEST_PROJECT_ID)
    assert call_kwargs["detail"]["event_type"] == route.event_type


@pytest.mark.asyncio
async def test_delete_not_found(client, url):
    db = make_mock_db()
    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("src.api.notifications.audit", new_callable=AsyncMock) as mock_audit,
    ):
        resp = await client.delete(f"{url}/{uuid.uuid4()}")
    assert resp.status_code == 404
    mock_audit.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test send
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_test_send_email(client, url):
    db = make_mock_db()
    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("shared.alerting.smtp_sender.send_email", new_callable=AsyncMock) as mock_send,
    ):
        resp = await client.post(f"{url}/test", json={
            "event_type": "schema_drift",
            "channel_type": "email",
            "channel_config": {"recipients": ["admin@test.com"]},
        })
    assert resp.status_code == 200
    assert resp.json()["status"] == "sent"
    mock_send.assert_awaited_once()


@pytest.mark.asyncio
async def test_test_send_slack(client, url):
    db = make_mock_db()
    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("shared.alerting.slack_sender.send_slack", new_callable=AsyncMock) as mock_send,
    ):
        resp = await client.post(f"{url}/test", json={
            "event_type": "refresh_failure",
            "channel_type": "slack",
            "channel_config": {"webhook_url": "https://hooks.slack.com/test"},
        })
    assert resp.status_code == 200
    assert resp.json()["status"] == "sent"
    mock_send.assert_awaited_once()


@pytest.mark.asyncio
async def test_test_send_email_no_recipients(client, url):
    db = make_mock_db()
    with patch("src.api.notifications.get_tenant_db", async_gen_from(db)):
        resp = await client.post(f"{url}/test", json={
            "event_type": "schema_drift",
            "channel_type": "email",
            "channel_config": {"recipients": []},
        })
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Bug-5277: partial update sends channel_config without channel_type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_update_channel_config_without_channel_type_is_validated(
    client, url,
):
    """Bug-5277: a PUT that sends only channel_config (no channel_type) must
    still be validated against the persisted channel_type. Before the fix
    the validation was skipped because effective_channel_type was None.

    Scenario: the persisted route has channel_type='email'. The partial
    update sends an empty recipients list in channel_config but omits
    channel_type. The API must reject with 422 (email requires non-empty
    recipients), not silently accept.
    """
    route = _make_route(
        channel_type="email",
        channel_config={"recipients": ["admin@test.com"]},
    )
    db = _mock_db_with_routes([route])

    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("src.api.notifications.audit", new_callable=AsyncMock),
    ):
        resp = await client.put(f"{url}/{route.id}", json={
            # No channel_type -- partial update
            "channel_config": {"recipients": []},
        })

    # Must be rejected: empty recipients for an email route.
    assert resp.status_code == 422
