"""Tests for the audit logging system (Phase 3, Block A)."""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from src.main import app
from src.auth.middleware import CurrentUser, get_current_user
from .conftest import (
    TEST_TENANT,
    TEST_USER_ID,
    NOW,
    async_gen_from,
    make_mock_db,
)


# ---------------------------------------------------------------------------
# Audit logger unit tests
# ---------------------------------------------------------------------------

class TestAuditLogger:
    """Unit tests for shared.audit.logger.audit()."""

    @pytest.mark.asyncio
    async def test_audit_creates_event_at_info_level(self):
        from shared.audit.logger import audit

        db = make_mock_db()
        setting_result = MagicMock()
        setting_result.scalar_one_or_none.return_value = {"value": "info"}
        db.execute = AsyncMock(return_value=setting_result)

        event = await audit(
            db,
            action="model.create",
            severity="info",
            actor_email="user@test.com",
            target_type="model",
            target_name="TestModel",
        )
        assert event is not None
        assert event.action == "model.create"
        assert event.severity == "info"
        assert event.actor_email == "user@test.com"
        db.add.assert_called_once()
        db.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_audit_suppressed_when_level_off(self):
        from shared.audit.logger import audit

        db = make_mock_db()
        setting_result = MagicMock()
        setting_result.scalar_one_or_none.return_value = {"value": "off"}
        db.execute = AsyncMock(return_value=setting_result)

        event = await audit(
            db,
            action="model.create",
            severity="info",
            actor_email="user@test.com",
        )
        assert event is None
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_audit_info_suppressed_at_critical_level(self):
        from shared.audit.logger import audit

        db = make_mock_db()
        setting_result = MagicMock()
        setting_result.scalar_one_or_none.return_value = {"value": "critical"}
        db.execute = AsyncMock(return_value=setting_result)

        event = await audit(
            db,
            action="model.create",
            severity="info",
            actor_email="user@test.com",
        )
        assert event is None

    @pytest.mark.asyncio
    async def test_audit_critical_passes_at_critical_level(self):
        from shared.audit.logger import audit

        db = make_mock_db()
        setting_result = MagicMock()
        setting_result.scalar_one_or_none.return_value = {"value": "critical"}
        db.execute = AsyncMock(return_value=setting_result)

        event = await audit(
            db,
            action="auth.login_failure",
            severity="critical",
            actor_email="attacker@test.com",
        )
        assert event is not None
        assert event.severity == "critical"

    @pytest.mark.asyncio
    async def test_audit_warn_passes_at_warn_level(self):
        from shared.audit.logger import audit

        db = make_mock_db()
        setting_result = MagicMock()
        setting_result.scalar_one_or_none.return_value = {"value": "warn"}
        db.execute = AsyncMock(return_value=setting_result)

        event = await audit(
            db,
            action="model.deploy",
            severity="warn",
            actor_email="user@test.com",
        )
        assert event is not None

    @pytest.mark.asyncio
    async def test_audit_info_suppressed_at_warn_level(self):
        from shared.audit.logger import audit

        db = make_mock_db()
        setting_result = MagicMock()
        setting_result.scalar_one_or_none.return_value = {"value": "warn"}
        db.execute = AsyncMock(return_value=setting_result)

        event = await audit(
            db,
            action="model.create",
            severity="info",
            actor_email="user@test.com",
        )
        assert event is None

    @pytest.mark.asyncio
    async def test_audit_defaults_to_info_when_no_setting(self):
        from shared.audit.logger import audit

        db = make_mock_db()
        setting_result = MagicMock()
        setting_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=setting_result)

        event = await audit(
            db,
            action="model.create",
            severity="info",
            actor_email="user@test.com",
        )
        assert event is not None

    @pytest.mark.asyncio
    async def test_audit_captures_ip_and_detail(self):
        from shared.audit.logger import audit

        db = make_mock_db()
        setting_result = MagicMock()
        setting_result.scalar_one_or_none.return_value = {"value": "info"}
        db.execute = AsyncMock(return_value=setting_result)

        target_id = uuid.uuid4()
        event = await audit(
            db,
            action="user.delete",
            severity="critical",
            actor_email="admin@test.com",
            target_type="user",
            target_id=target_id,
            target_name="victim@test.com",
            detail={"reason": "offboarding"},
            ip_address="192.168.1.1",
        )
        assert event is not None
        assert event.ip_address == "192.168.1.1"
        assert event.detail == {"reason": "offboarding"}
        assert event.target_id == target_id

    @pytest.mark.asyncio
    async def test_audit_swallows_db_errors(self):
        from shared.audit.logger import audit

        db = make_mock_db()
        db.execute = AsyncMock(side_effect=Exception("DB down"))

        event = await audit(
            db, action="model.create", severity="info",
            actor_email="user@test.com",
        )
        assert event is None


# ---------------------------------------------------------------------------
# Audit API tests
# ---------------------------------------------------------------------------

@pytest.fixture
def admin_user():
    user = CurrentUser(
        user_id=TEST_USER_ID,
        tenant_id=TEST_TENANT,
        email=TEST_USER_ID,
        role="tenant_admin",
    )
    app.dependency_overrides[get_current_user] = lambda: user
    yield user
    app.dependency_overrides.pop(get_current_user, None)


class TestAuditApi:

    @pytest.mark.asyncio
    async def test_list_audit_events_empty(self, admin_user):
        db = make_mock_db()

        count_result = MagicMock()
        count_result.scalar.return_value = 0
        rows_result = MagicMock()
        rows_result.scalars.return_value.all.return_value = []

        call_count = 0
        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return count_result
            return rows_result

        db.execute = AsyncMock(side_effect=side_effect)

        with patch("src.api.audit.get_tenant_db", async_gen_from(db)):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                resp = await client.get("/api/v1/admin/audit-events")

        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_list_audit_events_with_results(self, admin_user):
        db = make_mock_db()

        event_id = uuid.uuid4()
        mock_event = types.SimpleNamespace(
            id=event_id,
            timestamp=NOW,
            actor_id=None,
            actor_email="user@test.com",
            action="model.create",
            target_type="model",
            target_id=uuid.uuid4(),
            target_name="Sales Model",
            severity="info",
            detail={"slug": "sales"},
            ip_address="10.0.0.1",
        )

        count_result = MagicMock()
        count_result.scalar.return_value = 1
        rows_result = MagicMock()
        rows_result.scalars.return_value.all.return_value = [mock_event]

        call_count = 0
        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return count_result
            return rows_result

        db.execute = AsyncMock(side_effect=side_effect)

        with patch("src.api.audit.get_tenant_db", async_gen_from(db)):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                resp = await client.get("/api/v1/admin/audit-events")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["action"] == "model.create"

    @pytest.mark.asyncio
    async def test_export_csv(self, admin_user):
        db = make_mock_db()

        mock_event = types.SimpleNamespace(
            id=uuid.uuid4(),
            timestamp=NOW,
            actor_id=None,
            actor_email="user@test.com",
            action="model.deploy",
            target_type="model",
            target_id=uuid.uuid4(),
            target_name="Sales",
            severity="warn",
            detail=None,
            ip_address=None,
        )

        rows_result = MagicMock()
        rows_result.scalars.return_value.all.return_value = [mock_event]
        db.execute = AsyncMock(return_value=rows_result)

        with patch("src.api.audit.get_tenant_db", async_gen_from(db)):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                resp = await client.get("/api/v1/admin/audit-events/export")

        assert resp.status_code == 200
        assert "text/csv" in resp.headers.get("content-type", "")
        lines = resp.text.strip().split("\n")
        assert len(lines) == 2
        assert "model.deploy" in lines[1]


# ---------------------------------------------------------------------------
# Audit purge tests
# ---------------------------------------------------------------------------

class TestAuditPurge:

    @pytest.mark.asyncio
    async def test_purge_deletes_old_events(self):
        # The retention value is resolved through the settings registry
        # (get_setting), not a raw TenantSetting read — value_json stores a
        # bare scalar int, and the old direct read mis-assumed a dict shape
        # (F-012-05). Patch the resolver to return the configured value.
        from unittest.mock import patch
        from shared.audit import purge as purge_mod

        db = make_mock_db()
        delete_result = MagicMock()
        delete_result.rowcount = 5
        db.execute = AsyncMock(return_value=delete_result)

        with patch.object(purge_mod, "get_setting", AsyncMock(return_value=90)):
            purged = await purge_mod.purge_audit_events(db)
        assert purged == 5
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_purge_skips_indefinite_retention(self):
        # 0 = indefinite must skip the purge entirely — the bug allowed an
        # explicit 0 to fall through to the 365-day default (F-012-05).
        from unittest.mock import patch
        from shared.audit import purge as purge_mod

        db = make_mock_db()
        db.execute = AsyncMock()

        with patch.object(purge_mod, "get_setting", AsyncMock(return_value=0)):
            purged = await purge_mod.purge_audit_events(db)
        assert purged == 0
        db.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# Registry validation tests
# ---------------------------------------------------------------------------

class TestAuditSettingsRegistry:

    def test_audit_level_validation(self):
        from shared.config.registry import _validate_audit_level

        _validate_audit_level("off")
        _validate_audit_level("critical")
        _validate_audit_level("warn")
        _validate_audit_level("info")

        with pytest.raises(ValueError):
            _validate_audit_level("debug")
        with pytest.raises(ValueError):
            _validate_audit_level("")

    def test_retention_days_validation(self):
        from shared.config.registry import _validate_retention_days

        _validate_retention_days(0)
        _validate_retention_days(30)
        _validate_retention_days(365)

        with pytest.raises(ValueError):
            _validate_retention_days(15)
        with pytest.raises(ValueError):
            _validate_retention_days(-1)


# ---------------------------------------------------------------------------
# F-022-10: unknown severity must NOT be silently dropped (audit completeness)
# ---------------------------------------------------------------------------

class TestUnknownSeverityFailsOpen:
    """A misspelled/foreign severity (e.g. 'warning' instead of 'warn') must be
    recorded, not vanish. The writer treats an unrecognised severity as the most
    severe level so an audit-worthy action always produces an event."""

    @pytest.mark.asyncio
    async def test_unknown_severity_is_recorded_at_info_level(self):
        from shared.audit.logger import audit

        db = make_mock_db()
        setting_result = MagicMock()
        setting_result.scalar_one_or_none.return_value = {"value": "info"}
        db.execute = AsyncMock(return_value=setting_result)

        event = await audit(
            db, action="security.change", severity="warning",  # typo for "warn"
            actor_email="user@test.com",
        )
        assert event is not None, "unknown severity was silently dropped"
        db.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_severity_recorded_even_at_critical_level(self):
        """Treated as critical, so it is written even when the tenant only keeps
        critical events."""
        from shared.audit.logger import audit

        db = make_mock_db()
        setting_result = MagicMock()
        setting_result.scalar_one_or_none.return_value = {"value": "critical"}
        db.execute = AsyncMock(return_value=setting_result)

        event = await audit(
            db, action="security.change", severity="totally-bogus",
            actor_email="user@test.com",
        )
        assert event is not None
        db.add.assert_called_once()

    def test_should_log_unknown_severity_logic(self):
        from shared.audit.logger import _should_log
        # Unknown severity is always logged (treated as critical), at every
        # non-off level.
        assert _should_log("warning", "info") is True
        assert _should_log("warning", "warn") is True
        assert _should_log("warning", "critical") is True
        # ...but a tenant that disabled audit still suppresses everything.
        assert _should_log("warning", "off") is False
