"""Bug-8053 (F-022-04): durable, operator-visible email/Slack delivery records.

Before this fix the alerting dispatcher persisted routes and dedup claims but no
record of whether an email/Slack notification actually reached its destination.
A failed send left only an application-log line — invisible in the product.

These tests assert that:
  - a FAILED email/Slack send writes a durable ``NotificationDelivery`` row with
    status='failed' that an operator can read;
  - a SUCCESSFUL send writes status='sent';
  - a broken channel (SMTP unset / no webhook URL) also writes a failed record;
  - the operator-facing ``GET /notifications/deliveries`` endpoint returns them.
"""
from __future__ import annotations

import types
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.db.models import NotificationDelivery
from tests.conftest import (
    TEST_PROJECT_ID,
    NOW,
    async_gen_from,
    make_mock_db,
)


@contextmanager
def _smtp_configured():
    fake = MagicMock()
    fake.SMTP_HOST = "mail.test"
    with patch("shared.alerting.dispatcher._get_settings", return_value=fake):
        yield


@contextmanager
def _smtp_unconfigured():
    fake = MagicMock()
    fake.SMTP_HOST = ""
    with patch("shared.alerting.dispatcher._get_settings", return_value=fake):
        yield


def _email_route():
    route = MagicMock()
    route.id = uuid.uuid4()
    route.channel_type = "email"
    route.event_type = "refresh_failure"
    route.enabled = True
    route.channel_config = {"recipients": ["ops@test.com"]}
    return route


def _slack_route():
    route = MagicMock()
    route.id = uuid.uuid4()
    route.channel_type = "slack"
    route.event_type = "refresh_failure"
    route.enabled = True
    route.channel_config = {"webhook_url": "https://hooks.slack.com/services/T/B/secret"}
    return route


def _capture_db(routes):
    """A mock session that records every object handed to ``db.add``."""
    db = AsyncMock()
    res = MagicMock()
    res.scalars.return_value.all.return_value = routes
    db.execute = AsyncMock(return_value=res)
    added: list = []
    db.add = MagicMock(side_effect=added.append)
    db.commit = AsyncMock()
    return db, added


def _delivery_records(added: list) -> list[NotificationDelivery]:
    return [o for o in added if isinstance(o, NotificationDelivery)]


class TestFailedSendRecorded:
    @pytest.mark.asyncio
    async def test_failed_email_send_writes_failed_record(self):
        from shared.alerting import dispatcher

        route = _email_route()
        db, added = _capture_db([route])

        with (
            patch(
                "shared.alerting.smtp_sender.send_email",
                new_callable=AsyncMock,
                side_effect=RuntimeError("SMTP timeout"),
            ),
            patch.object(dispatcher, "_claim_dispatch", new_callable=AsyncMock, return_value=True),
            patch.object(dispatcher, "_release_dispatch", new_callable=AsyncMock),
            _smtp_configured(),
        ):
            sent = await dispatcher.dispatch_alert(
                db, event_type="refresh_failure",
                project_id=TEST_PROJECT_ID,
                incident_key="refresh_failure:agg-A",
                subject="Refresh failed", body_html="<p>x</p>",
            )

        assert sent == 0
        records = _delivery_records(added)
        assert len(records) == 1
        rec = records[0]
        assert rec.status == "failed"
        assert rec.channel_type == "email"
        assert "SMTP timeout" in (rec.error_message or "")
        # The recipient (non-secret) is the target hint.
        assert rec.target == "ops@test.com"

    @pytest.mark.asyncio
    async def test_successful_email_send_writes_sent_record(self):
        from shared.alerting import dispatcher

        route = _email_route()
        db, added = _capture_db([route])

        with (
            patch("shared.alerting.smtp_sender.send_email", new_callable=AsyncMock),
            patch.object(dispatcher, "_claim_dispatch", new_callable=AsyncMock, return_value=True),
            _smtp_configured(),
        ):
            sent = await dispatcher.dispatch_alert(
                db, event_type="refresh_failure",
                project_id=TEST_PROJECT_ID,
                subject="Refresh failed", body_html="<p>x</p>",
            )

        assert sent == 1
        records = _delivery_records(added)
        assert len(records) == 1
        assert records[0].status == "sent"
        assert records[0].channel_type == "email"

    @pytest.mark.asyncio
    async def test_smtp_unconfigured_writes_failed_record(self):
        from shared.alerting import dispatcher

        route = _email_route()
        db, added = _capture_db([route])
        claim = AsyncMock(return_value=True)

        with (
            patch.object(dispatcher, "_claim_dispatch", claim),
            _smtp_unconfigured(),
        ):
            sent = await dispatcher.dispatch_alert(
                db, event_type="refresh_failure",
                project_id=TEST_PROJECT_ID,
                subject="Refresh failed", body_html="<p>x</p>",
            )

        assert sent == 0
        # Bug-7340: the dedup window is not consumed on an SMTP-unset skip...
        claim.assert_not_awaited()
        # ...but the failure is now durably recorded (Bug-8053).
        records = _delivery_records(added)
        assert len(records) == 1
        assert records[0].status == "failed"
        assert "SMTP" in (records[0].error_message or "")

    @pytest.mark.asyncio
    async def test_failed_slack_send_writes_failed_record_without_secret(self):
        from shared.alerting import dispatcher

        route = _slack_route()
        db, added = _capture_db([route])

        with (
            patch(
                "shared.alerting.slack_sender.send_slack",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Slack 500"),
            ),
            patch.object(dispatcher, "_claim_dispatch", new_callable=AsyncMock, return_value=True),
            patch.object(dispatcher, "_release_dispatch", new_callable=AsyncMock),
        ):
            sent = await dispatcher.dispatch_alert(
                db, event_type="refresh_failure",
                project_id=TEST_PROJECT_ID,
                subject="Refresh failed", body_html="<p>x</p>",
                slack_text="Refresh failed",
            )

        assert sent == 0
        records = _delivery_records(added)
        assert len(records) == 1
        rec = records[0]
        assert rec.status == "failed"
        assert rec.channel_type == "slack"
        # The plaintext Slack secret must NEVER appear in the delivery record.
        assert "secret" not in (rec.target or "")
        assert (rec.target or "").startswith("slack:")

    @pytest.mark.asyncio
    async def test_slack_error_message_scrubs_webhook_url(self, caplog):
        """Bug-8053/8056: the plaintext Slack webhook URL must never land in the
        durable, operator-readable ``error_message`` — NOR in the application
        log the failure re-raises into. Scrub once at the boundary so no
        consumer (DB field or centralised log) can reintroduce the secret."""
        import logging
        from shared.alerting import dispatcher

        secret_url = "https://hooks.slack.com/services/T/B/sUpErSeCrEt"
        route = _slack_route()
        route.channel_config = {"webhook_url": secret_url}
        db, added = _capture_db([route])

        async def _boom(*a, **k):
            raise RuntimeError(f"POST to {secret_url} failed with 500")

        with (
            patch("shared.alerting.slack_sender.send_slack", side_effect=_boom),
            patch.object(dispatcher, "_claim_dispatch", new_callable=AsyncMock, return_value=True),
            patch.object(dispatcher, "_release_dispatch", new_callable=AsyncMock),
            caplog.at_level(logging.ERROR, logger="shared.alerting.dispatcher"),
        ):
            await dispatcher.dispatch_alert(
                db, event_type="refresh_failure", project_id=TEST_PROJECT_ID,
                subject="x", body_html="<p>x</p>", slack_text="x",
            )

        records = _delivery_records(added)
        assert len(records) == 1
        assert "sUpErSeCrEt" not in (records[0].error_message or "")
        assert "<redacted-webhook-url>" in (records[0].error_message or "")
        # The secret must not leak through the log the outer handler emits...
        assert "sUpErSeCrEt" not in caplog.text
        # ...and the redacted line must actually be present (non-vacuous: proves
        # the outer handler still logged the failure, just scrubbed).
        assert "<redacted-webhook-url>" in caplog.text

    @pytest.mark.asyncio
    async def test_unknown_channel_type_writes_failed_record(self):
        """Bug-8053: a legacy/imported route with an unknown channel type is
        recorded as failed, not silently dropped with only a log line."""
        from shared.alerting import dispatcher

        route = MagicMock()
        route.id = uuid.uuid4()
        route.channel_type = "telegram"  # not in _VALID_CHANNELS
        route.event_type = "refresh_failure"
        route.enabled = True
        route.channel_config = {}
        db, added = _capture_db([route])

        sent = await dispatcher.dispatch_alert(
            db, event_type="refresh_failure", project_id=TEST_PROJECT_ID,
            subject="x", body_html="<p>x</p>",
        )

        assert sent == 0
        records = _delivery_records(added)
        assert len(records) == 1
        assert records[0].status == "failed"
        assert records[0].channel_type == "telegram"

    @pytest.mark.asyncio
    async def test_unexpected_branch_error_writes_single_failed_record(self):
        """Bug-8053: an unexpected error inside a route branch (here a NULL
        channel_config) still leaves durable evidence via the outer-except
        safety net — and exactly ONE record, never a duplicate."""
        from shared.alerting import dispatcher

        route = MagicMock()
        route.id = uuid.uuid4()
        route.channel_type = "email"
        route.event_type = "refresh_failure"
        route.enabled = True
        route.channel_config = None  # .get(...) raises AttributeError
        db, added = _capture_db([route])

        sent = await dispatcher.dispatch_alert(
            db, event_type="refresh_failure", project_id=TEST_PROJECT_ID,
            subject="x", body_html="<p>x</p>",
        )

        assert sent == 0
        records = _delivery_records(added)
        assert len(records) == 1
        assert records[0].status == "failed"

    @pytest.mark.asyncio
    async def test_slack_missing_url_writes_failed_record(self):
        from shared.alerting import dispatcher

        route = _slack_route()
        route.channel_config = {}  # no webhook URL
        db, added = _capture_db([route])

        with patch.object(dispatcher, "_claim_dispatch", new_callable=AsyncMock, return_value=True):
            sent = await dispatcher.dispatch_alert(
                db, event_type="refresh_failure",
                project_id=TEST_PROJECT_ID,
                subject="Refresh failed", body_html="<p>x</p>",
                slack_text="Refresh failed",
            )

        assert sent == 0
        records = _delivery_records(added)
        assert len(records) == 1
        assert records[0].status == "failed"
        assert "webhook URL" in (records[0].error_message or "")


# ---------------------------------------------------------------------------
# Operator-facing API
# ---------------------------------------------------------------------------

def _delivery_row(status: str = "failed", channel_type: str = "email"):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        route_id=uuid.uuid4(),
        project_id=TEST_PROJECT_ID,
        event_type="refresh_failure",
        channel_type=channel_type,
        target="ops@test.com",
        status=status,
        error_message="SMTP timeout" if status == "failed" else None,
        created_at=NOW,
    )


@pytest.fixture
def url():
    return f"/api/v1/projects/{TEST_PROJECT_ID}/notifications/deliveries"


@pytest.mark.asyncio
async def test_list_deliveries_returns_failed_records(client, url):
    rows = [_delivery_row(status="failed"), _delivery_row(status="sent", channel_type="slack")]
    db = make_mock_db()
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    db.execute = AsyncMock(return_value=result)

    with patch("src.api.notifications.get_tenant_db", async_gen_from(db)):
        resp = await client.get(url)

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    failed = [d for d in data if d["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["error_message"] == "SMTP timeout"
    assert failed[0]["channel_type"] == "email"


@pytest.mark.asyncio
async def test_list_deliveries_empty(client, url):
    db = make_mock_db()
    with patch("src.api.notifications.get_tenant_db", async_gen_from(db)):
        resp = await client.get(url)
    assert resp.status_code == 200
    assert resp.json() == []
