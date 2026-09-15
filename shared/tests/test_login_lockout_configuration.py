"""Coverage for the configured account-lockout policy.

Bug-9833 pinned a default of five with zero REJECTED, so the control could not
be switched off. Bug-10060 reversed that: the account lock is keyed on the
ACCOUNT rather than the caller, so an unauthenticated caller could hold any
known address locked. Owner decision 2026-09-15 - Tessallite does not lock
accounts; zero is the default and means never lock.

These tests still pin the configured policy exactly as before. Only the policy
they pin has changed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from shared.auth import lockout
from shared.config.settings import Settings


def test_bug10060_account_lockout_is_disabled_by_default():
    """Zero means never lock, and it is the default."""
    assert Settings().TESSALLITE_LOGIN_LOCKOUT_MAX_FAILURES == 0


def test_bug10060_zero_is_accepted_so_the_lock_can_be_switched_off():
    """The previous ge=1 made the account lock impossible to disable."""
    assert Settings(TESSALLITE_LOGIN_LOCKOUT_MAX_FAILURES=0).TESSALLITE_LOGIN_LOCKOUT_MAX_FAILURES == 0


@pytest.mark.parametrize("value", [-1, "five", 1.5])
def test_bug10060_malformed_thresholds_are_still_rejected(value):
    """Disabling is deliberate (0); a negative or non-integer value is not."""
    with pytest.raises(ValidationError):
        Settings(TESSALLITE_LOGIN_LOCKOUT_MAX_FAILURES=value)


@pytest.mark.asyncio
async def test_bug10060_disabled_lockout_never_queries_the_database(monkeypatch):
    """Disabled must cost nothing: no row read, so no row lock either."""
    monkeypatch.setattr(lockout, "_MAX_FAILURES", 0)
    db = AsyncMock()
    await lockout.assert_not_locked(db, "acme-demo", "viewer@example.com")
    db.execute.assert_not_called()


def test_bug9833_demo_threshold_accepts_one_thousand():
    settings = Settings(TESSALLITE_LOGIN_LOCKOUT_MAX_FAILURES=1000)
    assert settings.TESSALLITE_LOGIN_LOCKOUT_MAX_FAILURES == 1000


@pytest.mark.asyncio
async def test_bug9833_demo_threshold_releases_row_locked_under_default(monkeypatch):
    monkeypatch.setattr(lockout, "_MAX_FAILURES", 1000)
    row = SimpleNamespace(
        failed_count=5,
        locked_until=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    monkeypatch.setattr(lockout, "_get", AsyncMock(return_value=row))

    await lockout.assert_not_locked(AsyncMock(), "acme-demo", "viewer@example.com")


@pytest.mark.asyncio
async def test_bug9833_demo_threshold_blocks_at_one_thousand(monkeypatch):
    monkeypatch.setattr(lockout, "_MAX_FAILURES", 1000)
    row = SimpleNamespace(
        failed_count=1000,
        locked_until=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    monkeypatch.setattr(lockout, "_get", AsyncMock(return_value=row))

    with pytest.raises(HTTPException) as exc_info:
        await lockout.assert_not_locked(
            AsyncMock(), "acme-demo", "viewer@example.com"
        )

    assert exc_info.value.status_code == 429
