"""Bug-6324 — notification test-sends are bounded, attributable, and may be
confined to operator-approved recipient domains.

"Send a test alert" is a modeler-level action that puts arbitrary recipient
addresses behind the PLATFORM's verified SMTP identity. Unbounded, one
compromised modeler account can use it as an open relay and burn the sending
reputation every tenant depends on.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.middleware import action_throttle
from src.api import notifications
from tests.conftest import TEST_PROJECT_ID, async_gen_from, make_mock_db

pytestmark = pytest.mark.unit


@pytest.fixture
def url():
    return f"/api/v1/projects/{TEST_PROJECT_ID}/notifications"


def _mock_db_with_route(route):
    db = make_mock_db()
    result = MagicMock()
    result.scalar_one_or_none.return_value = route
    result.scalars.return_value.all.return_value = [route] if route else []
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.fixture(autouse=True)
def _fresh_quota_counters(monkeypatch):
    """Each test gets its own in-memory bucket store."""
    from limits.storage import storage_from_string
    from limits.strategies import FixedWindowRateLimiter

    limiter = FixedWindowRateLimiter(storage_from_string("memory://"))
    monkeypatch.setattr(action_throttle, "_limiter", lambda: limiter)
    yield


def _settings(monkeypatch, **overrides):
    real = notifications.system_snapshot_get

    def _get(key):
        if key in overrides:
            return overrides[key]
        return real(key)

    monkeypatch.setattr(notifications, "system_snapshot_get", _get)


async def _post_test(client, url, recipients):
    return await client.post(f"{url}/test", json={
        "event_type": "schema_drift",
        "channel_type": "email",
        "channel_config": {"recipients": recipients},
    })


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_test_sends_are_capped_per_project_per_hour(client, url, monkeypatch):
    _settings(monkeypatch, **{"notifications.test_send_per_hour": 3})
    db = make_mock_db()
    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("src.api.notifications.audit", new_callable=AsyncMock),
        patch("shared.alerting.smtp_sender.send_email", new_callable=AsyncMock) as mock_send,
    ):
        for _ in range(3):
            resp = await _post_test(client, url, ["ops@test.com"])
            assert resp.status_code == 200, resp.text
        blocked = await _post_test(client, url, ["ops@test.com"])

    assert blocked.status_code == 429
    assert blocked.headers.get("Retry-After") == "3600"
    assert mock_send.await_count == 3, "a throttled send still reached SMTP"


@pytest.mark.asyncio
async def test_saved_route_test_send_shares_the_same_project_quota(
    client, url, monkeypatch,
):
    """A modeler must not sidestep the ceiling by saving the route first."""
    _settings(monkeypatch, **{"notifications.test_send_per_hour": 1})
    route_id = uuid.uuid4()
    route = type("R", (), {
        "id": route_id,
        "channel_type": "email",
        "channel_config": {"recipients": ["ops@test.com"]},
    })()
    db = _mock_db_with_route(route)
    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("src.api.notifications.audit", new_callable=AsyncMock),
        patch("shared.alerting.smtp_sender.send_email", new_callable=AsyncMock) as mock_send,
    ):
        first = await _post_test(client, url, ["ops@test.com"])
        second = await client.post(f"{url}/{route_id}/test")

    assert first.status_code == 200
    assert second.status_code == 429
    assert mock_send.await_count == 1


@pytest.mark.asyncio
async def test_zero_quota_disables_test_sends(client, url, monkeypatch):
    _settings(monkeypatch, **{"notifications.test_send_per_hour": 0})
    db = make_mock_db()
    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("shared.alerting.smtp_sender.send_email", new_callable=AsyncMock) as mock_send,
    ):
        resp = await _post_test(client, url, ["ops@test.com"])
    assert resp.status_code == 403
    mock_send.assert_not_awaited()


# ---------------------------------------------------------------------------
# Recipient bounds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recipient_count_is_capped(client, url, monkeypatch):
    _settings(monkeypatch, **{"notifications.test_max_recipients": 2})
    db = make_mock_db()
    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("shared.alerting.smtp_sender.send_email", new_callable=AsyncMock) as mock_send,
    ):
        resp = await _post_test(
            client, url, ["a@test.com", "b@test.com", "c@test.com"],
        )
    assert resp.status_code == 422
    assert "at most 2" in resp.json()["detail"]
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_domain_allowlist_blocks_foreign_recipients(client, url, monkeypatch):
    _settings(
        monkeypatch,
        **{"notifications.recipient_domain_allowlist": "corp.example"},
    )
    db = make_mock_db()
    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("shared.alerting.smtp_sender.send_email", new_callable=AsyncMock) as mock_send,
    ):
        resp = await _post_test(client, url, ["victim@elsewhere.test"])
    assert resp.status_code == 422
    assert "not in this platform's allowed notification domains" in resp.json()["detail"]
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_domain_allowlist_admits_subdomains_but_not_lookalikes(
    client, url, monkeypatch,
):
    _settings(
        monkeypatch,
        **{"notifications.recipient_domain_allowlist": "corp.example"},
    )
    db = make_mock_db()
    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("src.api.notifications.audit", new_callable=AsyncMock),
        patch("shared.alerting.smtp_sender.send_email", new_callable=AsyncMock),
    ):
        ok = await _post_test(client, url, ["ops@eu.corp.example"])
        lookalike = await _post_test(client, url, ["ops@evil-corp.example"])
    assert ok.status_code == 200
    assert lookalike.status_code == 422


@pytest.mark.asyncio
async def test_empty_allowlist_keeps_any_domain_working(client, url, monkeypatch):
    _settings(monkeypatch, **{"notifications.recipient_domain_allowlist": ""})
    db = make_mock_db()
    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("src.api.notifications.audit", new_callable=AsyncMock),
        patch("shared.alerting.smtp_sender.send_email", new_callable=AsyncMock),
    ):
        resp = await _post_test(client, url, ["anyone@anywhere.test"])
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_every_test_send_is_audited_with_its_recipients(client, url, monkeypatch):
    db = make_mock_db()
    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("src.api.notifications.audit", new_callable=AsyncMock) as mock_audit,
        patch("shared.alerting.smtp_sender.send_email", new_callable=AsyncMock),
    ):
        resp = await _post_test(client, url, ["ops@test.com"])
    assert resp.status_code == 200
    mock_audit.assert_awaited_once()
    kwargs = mock_audit.call_args.kwargs
    assert kwargs["action"] == "notification_route.test_send"
    assert kwargs["severity"] == "warn"
    assert kwargs["detail"]["recipients"] == ["ops@test.com"]
    assert kwargs["detail"]["recipient_count"] == 1


# ---------------------------------------------------------------------------
# The throttle primitive itself
# ---------------------------------------------------------------------------

def test_action_quota_scopes_buckets_independently():
    from limits.storage import storage_from_string
    from limits.strategies import FixedWindowRateLimiter

    limiter = FixedWindowRateLimiter(storage_from_string("memory://"))
    original = action_throttle._limiter
    try:
        action_throttle._limiter = lambda: limiter
        assert action_throttle.consume_action_quota("act", "p1", limit="1/hour")
        assert not action_throttle.consume_action_quota("act", "p1", limit="1/hour")
        # A different project has its own bucket.
        assert action_throttle.consume_action_quota("act", "p2", limit="1/hour")
    finally:
        action_throttle._limiter = original


def test_action_quota_allows_when_storage_is_broken(caplog):
    """The quota is an abuse control, not an authorization decision — a broken
    counter must not take a working feature offline, but must be visible."""
    original = action_throttle._limiter

    class _Boom:
        def hit(self, *a, **k):
            raise RuntimeError("storage down")

    try:
        action_throttle._limiter = lambda: _Boom()
        assert action_throttle.consume_action_quota("act", "p1", limit="1/hour")
    finally:
        action_throttle._limiter = original
    assert any("Action quota storage unavailable" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# R2 finding 11 — a per-project ceiling does not bound a platform-wide harm
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tenant_ceiling_bounds_an_admin_who_creates_more_projects(
    client, monkeypatch,
):
    """The per-project bucket is multiplied by however many projects a tenant
    admin chooses to create, so it cannot bound SMTP sender reputation on its
    own."""
    _settings(monkeypatch, **{
        "notifications.test_send_per_hour": 1,
        "notifications.test_send_per_hour_tenant": 2,
        "notifications.test_send_per_hour_platform": 0,
    })
    db = make_mock_db()
    sent = []
    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("src.api.notifications.audit", new_callable=AsyncMock),
        patch("shared.alerting.smtp_sender.send_email", new_callable=AsyncMock,
              side_effect=lambda **kw: sent.append(kw)),
    ):
        codes = []
        for _ in range(4):
            # A fresh project each time defeats the per-project bucket.
            url = f"/api/v1/projects/{uuid.uuid4()}/notifications"
            codes.append((await _post_test(client, url, ["ops@test.com"])).status_code)

    assert codes[:2] == [200, 200], codes
    assert codes[2:] == [429, 429], (
        f"the tenant ceiling did not bound project fan-out: {codes}"
    )
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_platform_ceiling_is_the_outermost_bucket(client, url, monkeypatch):
    """Sender reputation is shared by every tenant, so the last ceiling has to
    be platform-wide."""
    _settings(monkeypatch, **{
        "notifications.test_send_per_hour": 100,
        "notifications.test_send_per_hour_tenant": 100,
        "notifications.test_send_per_hour_platform": 2,
    })
    db = make_mock_db()
    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("src.api.notifications.audit", new_callable=AsyncMock),
        patch("shared.alerting.smtp_sender.send_email", new_callable=AsyncMock),
    ):
        codes = [
            (await _post_test(client, url, ["ops@test.com"])).status_code
            for _ in range(3)
        ]
    assert codes == [200, 200, 429], codes


@pytest.mark.asyncio
async def test_outer_ceilings_can_be_switched_off_individually(client, url, monkeypatch):
    _settings(monkeypatch, **{
        "notifications.test_send_per_hour": 3,
        "notifications.test_send_per_hour_tenant": 0,
        "notifications.test_send_per_hour_platform": 0,
    })
    db = make_mock_db()
    with (
        patch("src.api.notifications.get_tenant_db", async_gen_from(db)),
        patch("src.api.notifications.audit", new_callable=AsyncMock),
        patch("shared.alerting.smtp_sender.send_email", new_callable=AsyncMock),
    ):
        codes = [
            (await _post_test(client, url, ["ops@test.com"])).status_code
            for _ in range(4)
        ]
    assert codes == [200, 200, 200, 429], codes
