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
        patch("src.api.notifications.audit_required", new_callable=AsyncMock) as mock_audit,
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
async def test_every_event_type_has_a_human_catalogue_label(client, url):
    """Bug-8114 F-2: ``_EVENT_LABELS.get(name, name)`` falls back to the raw
    event-type string, so a dispatcher EVENT_TYPES member with no entry in
    ``_EVENT_LABELS`` is silently invisible in THIS test (values still match)
    but renders the raw machine name (e.g. "pocket_refresh_failure" instead
    of "Pocket Refresh Failure") in the Alerts panel dropdown. Assert every
    label is an actual human label, not the value echoed back."""
    from shared.alerting.dispatcher import EVENT_TYPES
    resp = await client.get(f"{url}/event-types")
    assert resp.status_code == 200
    by_value = {e["value"]: e["label"] for e in resp.json()}
    unlabelled = [v for v in EVENT_TYPES if by_value.get(v) == v]
    assert not unlabelled, (
        f"EVENT_TYPES member(s) with no human label in _EVENT_LABELS "
        f"(notifications.py): {sorted(unlabelled)}"
    )


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
        patch("src.api.notifications.audit_required", new_callable=AsyncMock) as mock_audit,
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
        patch("src.api.notifications.audit_required", new_callable=AsyncMock) as mock_audit,
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
        patch("src.api.notifications.audit_required", new_callable=AsyncMock) as mock_audit,
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
        patch("src.api.notifications.audit_required", new_callable=AsyncMock) as mock_audit,
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
        patch("src.api.notifications.audit_required", new_callable=AsyncMock) as mock_audit,
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
        patch("src.api.notifications.audit_required", new_callable=AsyncMock) as mock_audit,
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
        patch("src.api.notifications.audit_required", new_callable=AsyncMock),
    ):
        resp = await client.put(f"{url}/{route.id}", json={
            # No channel_type -- partial update
            "channel_config": {"recipients": []},
        })

    # Must be rejected: empty recipients for an email route.
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_partial_update_channel_type_without_channel_config_is_validated(
    client, url,
):
    """Bug-6016 (found on Bug-5999 re-review): the mirror of Bug-5277 -- a PUT
    that changes channel_type WITHOUT resending channel_config must still be
    validated. Before the fix this was skipped entirely, because validation
    only ran when channel_config was provided. The route's config never
    changes when channel_config is omitted, so switching channel_type alone
    would leave a slack-typed route carrying its old email config
    ({"recipients": [...]}) -- silently dropped by the dispatcher at send
    time instead of rejected at save time.
    """
    route = _make_route(
        channel_type="email",
        channel_config={"recipients": ["admin@test.com"]},
    )
    db = _mock_db_with_routes([route])

    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("src.api.notifications.audit_required", new_callable=AsyncMock),
    ):
        resp = await client.put(f"{url}/{route.id}", json={
            # No channel_config -- the persisted email config has no
            # webhook_url, so switching to slack must be rejected.
            "channel_type": "slack",
        })

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_partial_update_channel_type_resent_unchanged_without_config_still_valid(
    client, url,
):
    """Redundantly resending the SAME channel_type without channel_config
    must not be rejected -- the persisted config is already valid for that
    channel_type, so nothing about the effective config actually changed."""
    route = _make_route(
        channel_type="email",
        channel_config={"recipients": ["admin@test.com"]},
    )
    db = _mock_db_with_routes([route])

    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("src.api.notifications.audit_required", new_callable=AsyncMock),
    ):
        resp = await client.put(f"{url}/{route.id}", json={
            "channel_type": "email",
            "enabled": False,
        })

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Bug-6007 (partial): encrypt/redact cycle coverage for Bug-5945/5999/6000/6001.
# ---------------------------------------------------------------------------

from cryptography.fernet import Fernet

from shared.config import settings as settings_module
from shared.security import credential_crypto as cc

_TEST_WEBHOOK_URL = "https://hooks.slack.com/services/T000/B000/original-secret"


def _set_encryption_key(monkeypatch) -> None:
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY_PREVIOUS", "")
    settings_module.get_settings.cache_clear()
    cc._multifernet_cached.cache_clear()


@pytest.mark.asyncio
async def test_create_slack_route_encrypts_webhook_url_and_redacts_response(
    client, url, monkeypatch,
):
    """Bug-5945: the plaintext URL must never reach the DB or the response;
    the response must expose only ``has_webhook_url: true``."""
    _set_encryption_key(monkeypatch)
    db = make_mock_db()

    async def fake_refresh(obj):
        obj.id = uuid.uuid4()
        obj.created_at = NOW
        obj.updated_at = NOW

    db.refresh = AsyncMock(side_effect=fake_refresh)

    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("src.api.notifications.audit_required", new_callable=AsyncMock),
    ):
        resp = await client.post(url, json={
            "event_type": "refresh_failure",
            "channel_type": "slack",
            "channel_config": {"webhook_url": _TEST_WEBHOOK_URL},
        })
    assert resp.status_code == 201
    body = resp.json()
    assert body["channel_config"] == {"has_webhook_url": True}

    # The row handed to db.add() is the persistence-layer truth: plaintext
    # must be gone, only the encrypted blob + flag remain.
    stored = db.add.call_args_list[-1][0][0]
    assert "webhook_url" not in stored.channel_config
    assert stored.channel_config["has_webhook_url"] is True
    encrypted = stored.channel_config["webhook_url_encrypted"]
    assert encrypted != _TEST_WEBHOOK_URL
    assert cc.decrypt_str(encrypted.encode()) == _TEST_WEBHOOK_URL


@pytest.mark.asyncio
async def test_list_legacy_plaintext_slack_route_reports_has_webhook_url_true(
    client, url,
):
    """Bug-6001: a route saved before Bug-5945 (plaintext ``webhook_url``,
    no ``has_webhook_url`` flag) must still report ``has_webhook_url: true``
    -- the redaction check has to run before the plaintext key is popped."""
    route = _make_route(
        channel_type="slack",
        channel_config={"webhook_url": _TEST_WEBHOOK_URL},
    )
    db = _mock_db_with_routes([route])
    with patch("src.api.notifications.get_tenant_db", async_gen_from(db)):
        resp = await client.get(url)
    assert resp.status_code == 200
    config = resp.json()[0]["channel_config"]
    assert config == {"has_webhook_url": True}
    assert "webhook_url" not in config
    assert "webhook_url_encrypted" not in config


@pytest.mark.asyncio
async def test_update_blank_webhook_url_preserves_existing_secret(
    client, url, monkeypatch,
):
    """Bug-5999: editing a Slack route without resupplying the URL must keep
    the previously-configured secret, not wipe it or 422."""
    _set_encryption_key(monkeypatch)
    encrypted = cc.encrypt_str(_TEST_WEBHOOK_URL).decode("utf-8")
    route = _make_route(
        channel_type="slack",
        channel_config={"webhook_url_encrypted": encrypted, "has_webhook_url": True},
    )
    db = _mock_db_with_routes([route])

    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("src.api.notifications.audit_required", new_callable=AsyncMock),
    ):
        resp = await client.put(f"{url}/{route.id}", json={
            "channel_config": {"webhook_url": ""},
        })
    assert resp.status_code == 200
    body = resp.json()
    assert body["channel_config"] == {"has_webhook_url": True}
    # The persisted secret must still decrypt to the original URL.
    assert cc.decrypt_str(route.channel_config["webhook_url_encrypted"].encode()) == (
        _TEST_WEBHOOK_URL
    )


@pytest.mark.asyncio
async def test_update_new_webhook_url_replaces_existing_secret(
    client, url, monkeypatch,
):
    """Bug-5999: supplying a fresh URL on edit still replaces the secret."""
    _set_encryption_key(monkeypatch)
    old_encrypted = cc.encrypt_str(_TEST_WEBHOOK_URL).decode("utf-8")
    route = _make_route(
        channel_type="slack",
        channel_config={"webhook_url_encrypted": old_encrypted, "has_webhook_url": True},
    )
    db = _mock_db_with_routes([route])
    new_url = "https://hooks.slack.com/services/T111/B111/new-secret"

    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("src.api.notifications.audit_required", new_callable=AsyncMock),
    ):
        resp = await client.put(f"{url}/{route.id}", json={
            "channel_config": {"webhook_url": new_url},
        })
    assert resp.status_code == 200
    assert cc.decrypt_str(route.channel_config["webhook_url_encrypted"].encode()) == new_url


@pytest.mark.asyncio
async def test_update_blank_webhook_url_without_existing_secret_is_rejected(
    client, url,
):
    """A blank webhook_url is only a no-op when a secret already exists.
    A slack route with nothing configured yet must still 422."""
    route = _make_route(channel_type="slack", channel_config={})
    db = _mock_db_with_routes([route])

    with patch("src.api.notifications.get_tenant_db", async_gen_from(db)):
        resp = await client.put(f"{url}/{route.id}", json={
            "channel_config": {"webhook_url": ""},
        })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_test_notification_route_decrypts_saved_secret(
    client, url, monkeypatch,
):
    """Bug-5999: POST /{route_id}/test must decrypt and use the persisted
    secret -- the client never resends the plaintext URL for a saved route."""
    _set_encryption_key(monkeypatch)
    encrypted = cc.encrypt_str(_TEST_WEBHOOK_URL).decode("utf-8")
    route = _make_route(
        channel_type="slack",
        channel_config={"webhook_url_encrypted": encrypted, "has_webhook_url": True},
    )
    db = _mock_db_with_routes([route])

    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("shared.alerting.slack_sender.send_slack", new_callable=AsyncMock) as mock_send,
    ):
        resp = await client.post(f"{url}/{route.id}/test")
    assert resp.status_code == 200
    assert resp.json()["status"] == "sent"
    mock_send.assert_awaited_once()
    assert mock_send.call_args.kwargs["webhook_url"] == _TEST_WEBHOOK_URL


@pytest.mark.asyncio
async def test_test_notification_route_not_found(client, url):
    db = make_mock_db()
    with patch("src.api.notifications.get_tenant_db", async_gen_from(db)):
        resp = await client.post(f"{url}/{uuid.uuid4()}/test")
    assert resp.status_code == 404
