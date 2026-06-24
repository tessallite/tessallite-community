"""Direct unit tests for sso_state.consume_state (Bug-5270/5271/5272/5274).

These tests exercise the consume_state function directly rather than via
the SSO callback endpoints, verifying the atomic DELETE ... RETURNING logic,
expiry checks, flow-type matching, browser-nonce enforcement (OIDC) vs
intentional nonce-skip (SAML), and single-use consumption.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db_yielding_row(row):
    """Build a mock get_system_db that yields a session whose execute()
    returns a result with .first() == *row*.  The mock tracks whether
    commit was called (needed for the atomic delete path)."""
    result = MagicMock()
    result.first.return_value = row
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()

    async def _gen():
        yield db

    return _gen, db


def _future(seconds: int = 300) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _past(seconds: int = 60) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestConsumeStateExpired:
    """(a) A row with expires_at in the past returns None."""

    @pytest.mark.asyncio
    async def test_expired_state_returns_none(self):
        row = ("tenant-x", "stored-nonce", "oidc-nonce-val", "oidc", _past(60))
        gen, db = _make_db_yielding_row(row)

        with patch("src.auth.sso_state.get_system_db", gen):
            from src.auth.sso_state import consume_state
            result = await consume_state("some-state", "oidc", "stored-nonce")

        assert result is None
        db.commit.assert_awaited_once()


class TestConsumeStateWrongFlowType:
    """(b) A row whose flow_type != the requested flow_type returns None."""

    @pytest.mark.asyncio
    async def test_wrong_flow_type_returns_none(self):
        row = ("tenant-x", "stored-nonce", "oidc-nonce-val", "saml", _future())
        gen, db = _make_db_yielding_row(row)

        with patch("src.auth.sso_state.get_system_db", gen):
            from src.auth.sso_state import consume_state
            result = await consume_state("some-state", "oidc", "stored-nonce")

        assert result is None


class TestConsumeStateOidcNonceMismatch:
    """(c) OIDC flow: browser_nonce mismatch returns None."""

    @pytest.mark.asyncio
    async def test_oidc_mismatched_nonce_returns_none(self):
        row = ("tenant-x", "correct-nonce", "oidc-nonce-val", "oidc", _future())
        gen, _ = _make_db_yielding_row(row)

        with patch("src.auth.sso_state.get_system_db", gen):
            from src.auth.sso_state import consume_state
            result = await consume_state("some-state", "oidc", "wrong-nonce")

        assert result is None


class TestConsumeStateOidcNonceMatch:
    """(d) OIDC flow: matching browser_nonce succeeds."""

    @pytest.mark.asyncio
    async def test_oidc_matching_nonce_succeeds(self):
        row = ("tenant-x", "correct-nonce", "oidc-nonce-val", "oidc", _future())
        gen, _ = _make_db_yielding_row(row)

        with patch("src.auth.sso_state.get_system_db", gen):
            from src.auth.sso_state import consume_state
            result = await consume_state("some-state", "oidc", "correct-nonce")

        assert result == ("tenant-x", "oidc-nonce-val")


class TestConsumeStateSamlNonceSkip:
    """(e) SAML flow: absent or mismatched browser_nonce still succeeds.

    Bug-5270: the SAML POST binding is a cross-site POST from the IdP.
    Browsers conforming to RFC 6265bis do not send SameSite=Lax cookies on
    cross-site POST requests, so the nonce cookie is intentionally not
    checked for SAML flows.
    """

    @pytest.mark.asyncio
    async def test_saml_absent_nonce_succeeds(self):
        row = ("tenant-y", "stored-nonce", None, "saml", _future())
        gen, _ = _make_db_yielding_row(row)

        with patch("src.auth.sso_state.get_system_db", gen):
            from src.auth.sso_state import consume_state
            result = await consume_state("some-state", "saml", None)

        assert result == ("tenant-y", None)

    @pytest.mark.asyncio
    async def test_saml_mismatched_nonce_succeeds(self):
        row = ("tenant-y", "stored-nonce", None, "saml", _future())
        gen, _ = _make_db_yielding_row(row)

        with patch("src.auth.sso_state.get_system_db", gen):
            from src.auth.sso_state import consume_state
            result = await consume_state("some-state", "saml", "totally-wrong")

        assert result == ("tenant-y", None)


class TestConsumeStateDoubleConsume:
    """(f) Double consume: the second call returns None (single-use).

    The atomic DELETE ... RETURNING ensures only the first caller gets the
    row. The second caller's DELETE matches zero rows and .first() is None.
    """

    @pytest.mark.asyncio
    async def test_second_consume_returns_none(self):
        # First call returns the row
        row = ("tenant-z", "nonce", "oidc-nonce", "oidc", _future())
        first_result = MagicMock()
        first_result.first.return_value = row
        # Second call returns None (row already deleted)
        second_result = MagicMock()
        second_result.first.return_value = None

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[first_result, second_result])
        db.commit = AsyncMock()

        call_count = 0

        async def _gen():
            nonlocal call_count
            call_count += 1
            yield db

        with patch("src.auth.sso_state.get_system_db", _gen):
            from src.auth.sso_state import consume_state

            first = await consume_state("the-state", "oidc", "nonce")
            second = await consume_state("the-state", "oidc", "nonce")

        assert first == ("tenant-z", "oidc-nonce")
        assert second is None
        assert db.execute.await_count == 2
