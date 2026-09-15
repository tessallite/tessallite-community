"""Bug-9744 item 2 — an exhausted pool is not an invalid token.

Live-reproduced twice on the investor-demo-rc stack. A deploy calls the
optimizer's predictive cold-start as a side effect; the optimizer also runs its
own in-process sweep; both compete with live user traffic for the same
2-connection tenant pool. A normal request then died inside
``_validate_regular_session``'s account-state lookup with
``sqlalchemy.exc.TimeoutError: QueuePool limit of size 2 overflow 0 reached``.

The blanket ``except Exception`` turned that into ``401 Invalid or expired
token``. So a transient resource shortage was reported as a credential failure:
the user is sent to the login screen, the operator to the identity provider, and
neither remedy touches the actual cause. The 401 also carries
``WWW-Authenticate``, prompting for credentials that were never wrong.

The refusal itself is correct and is NOT relaxed here — a session whose account
state cannot be read must still be denied. Only the story changes, from "your
token is bad" to "this could not be verified, retry".

The precedent was already in the same file: the embed-revocation guard fails
closed with a 403 that names the unverifiable check. These tests hold the two
lookups that lacked it to the same standard, and hold the line in the other
direction — a genuinely invalid session is still 401.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import exc as sa_exc

from shared.auth import middleware


class TestUnavailableIsDistinguishedFromInvalid:
    @pytest.mark.parametrize("exc", [
        sa_exc.TimeoutError("QueuePool limit of size 2 overflow 0 reached"),
        asyncio.TimeoutError(),
        ConnectionError("connection refused"),
    ])
    def test_unambiguously_transient_faults_are_recognised(self, exc):
        assert middleware._is_backend_unavailable(exc) is True

    def test_a_dropped_connection_is_transient(self):
        """SQLAlchemy sets ``connection_invalidated`` when it discarded the
        connection. That is the driver's own verdict and is trusted."""
        exc = sa_exc.OperationalError("SELECT 1", {}, Exception("server closed"))
        exc.connection_invalidated = True
        assert middleware._is_backend_unavailable(exc) is True

    @pytest.mark.parametrize("sqlstate,expected", [
        ("08006", True),   # connection_failure
        ("53300", True),   # too_many_connections
        ("57P01", True),   # admin_shutdown
        ("58030", True),   # io_error
        ("42601", False),  # syntax_error — permanent
        ("23505", False),  # unique_violation — permanent
        ("22001", False),  # string_data_right_truncation — permanent
    ])
    def test_sqlstate_decides_the_ambiguous_families(self, sqlstate, expected):
        """OperationalError straddles the line: a dropped connection is
        transient, a bad DSN or a constraint problem is not."""
        orig = Exception("boom")
        orig.sqlstate = sqlstate
        exc = sa_exc.OperationalError("SELECT 1", {}, orig)
        assert middleware._is_backend_unavailable(exc) is expected

    @pytest.mark.parametrize("exc", [
        ValueError("bad role claim"),
        KeyError("token_version"),
        TypeError("not an int"),
        RuntimeError("coding error"),
    ])
    def test_ordinary_faults_are_not_mistaken_for_an_outage(self, exc):
        """The over-correction direction. A coding error must not start
        answering 503 and inviting clients to retry it forever."""
        assert middleware._is_backend_unavailable(exc) is False

    @pytest.mark.parametrize("exc", [
        sa_exc.ProgrammingError("SELECT bad", {}, Exception("syntax error")),
        sa_exc.IntegrityError("INSERT", {}, Exception("unique violation")),
        sa_exc.DataError("SELECT", {}, Exception("value too long")),
        FileNotFoundError("missing.pem"),
        PermissionError("cannot read socket"),
    ])
    def test_permanent_faults_are_not_advertised_as_retryable(self, exc):
        """The defect the first version of this fix carried.

        ``DBAPIError`` and ``OSError`` are BASE classes: the first covers
        Programming/Integrity/Data errors, the second covers FileNotFound and
        Permission errors. Naming those bases meant a malformed statement, a
        missing migration, a constraint violation or an unreadable file each
        answered "503, retry shortly" — inviting clients to retry a permanent
        defect forever while hiding the real fault behind a transient status.
        """
        assert middleware._is_backend_unavailable(exc) is False


class TestTheRefusalStaysClosed:
    def test_unverifiable_sessions_are_refused_not_admitted(self):
        """The security property. This must never become a way in."""
        refusal = middleware._session_unverifiable()
        assert isinstance(refusal, HTTPException)
        assert refusal.status_code == 503
        assert 200 > refusal.status_code or refusal.status_code >= 400

    def test_the_refusal_does_not_prompt_for_credentials(self):
        """``WWW-Authenticate`` is what sends the user to the login screen for a
        password that was never the problem."""
        refusal = middleware._session_unverifiable()
        assert "WWW-Authenticate" not in (refusal.headers or {})
        assert (refusal.headers or {}).get("Retry-After")
        assert int(refusal.headers["Retry-After"]) > 0

    def test_the_message_says_it_is_not_a_credential_problem(self):
        """An operator reading only the message must not go to the identity
        provider — that is the whole cost of the old behaviour."""
        detail = middleware._session_unverifiable().detail.lower()
        assert "not a credential problem" in detail
        assert "retry" in detail


@pytest.mark.asyncio
class TestBothLookupsBehaveTheSameWay:
    """Both blanket handlers in this file carried the defect. Fixing only the
    one the live trace named would have left the other masking outages."""

    async def _regular(self, monkeypatch, raise_exc):
        def _boom(_tenant_id):
            raise raise_exc

        monkeypatch.setattr(middleware, "get_tenant_db", _boom)
        user = middleware.CurrentUser(
            user_id="u@t.com", tenant_id="acme", email="u@t.com", role="viewer",
        )
        auth_exc = HTTPException(status_code=401, detail="Invalid or expired token")
        with pytest.raises(HTTPException) as info:
            await middleware._validate_regular_session(
                user, {"token_version": 0}, auth_exc,
            )
        return info.value

    async def test_regular_session_pool_timeout_is_503(self, monkeypatch):
        """THE reported defect."""
        got = await self._regular(
            monkeypatch,
            sa_exc.TimeoutError("QueuePool limit of size 2 overflow 0 reached"),
        )
        assert got.status_code == 503

    async def test_regular_session_ordinary_error_is_still_401(self, monkeypatch):
        """Non-regression: an unexpected fault still fails closed as before."""
        got = await self._regular(monkeypatch, RuntimeError("unexpected"))
        assert got.status_code == 401

    async def test_system_admin_version_lookup_pool_timeout_is_503(self, monkeypatch):
        """The second site, found by enumeration rather than by the trace."""
        async def _boom():
            raise sa_exc.TimeoutError("QueuePool limit reached")

        monkeypatch.setattr(middleware, "get_system_admin_token_version", _boom)
        auth_exc = HTTPException(status_code=401, detail="Invalid or expired token")
        with pytest.raises(HTTPException) as info:
            await middleware._validate_system_admin_token_version(
                {"token_version": 0}, auth_exc,
            )
        assert info.value.status_code == 503

    async def test_system_admin_version_lookup_ordinary_error_is_still_401(
        self, monkeypatch,
    ):
        async def _boom():
            raise RuntimeError("unexpected")

        monkeypatch.setattr(middleware, "get_system_admin_token_version", _boom)
        auth_exc = HTTPException(status_code=401, detail="Invalid or expired token")
        with pytest.raises(HTTPException) as info:
            await middleware._validate_system_admin_token_version(
                {"token_version": 0}, auth_exc,
            )
        assert info.value.status_code == 401


class TestCorruptRevocationStateFailsClosed:
    """Bug-5534 review, adjacent security correction.

    ``get_system_admin_token_version`` degraded a MALFORMED stored value to 0.
    Absent is legitimately 0 — a missing row, or a pre-migration table. Corrupt
    is not: returning 0 silently undoes every revocation recorded since the
    first bump, so a token minted at version 0 compares equal again and a
    retired system-admin session is honoured.
    """

    def test_a_negative_version_is_rejected(self):
        with pytest.raises(ValueError):
            middleware._require_non_negative_version(-1)

    @pytest.mark.parametrize("value", ["", "abc", None, {}, []])
    def test_an_unreadable_version_is_rejected(self, value):
        with pytest.raises((TypeError, ValueError)):
            middleware._require_non_negative_version(value)

    @pytest.mark.parametrize("value,expected", [(0, 0), (7, 7), ("3", 3)])
    def test_a_readable_version_is_returned(self, value, expected):
        assert middleware._require_non_negative_version(value) == expected

    @pytest.mark.asyncio
    async def test_corrupt_state_refuses_as_a_server_fault_not_a_bad_token(
        self, monkeypatch,
    ):
        """Fail closed, and say the true reason.

        401 would blame the caller's credentials; 503 would invite a retry loop
        for something retrying cannot fix. Neither is honest, so this is a 500
        that names the server-side cause.
        """
        async def _corrupt():
            raise middleware.SystemAdminTokenVersionCorrupt("malformed")

        monkeypatch.setattr(
            middleware, "get_system_admin_token_version", _corrupt,
        )
        auth_exc = HTTPException(status_code=401, detail="Invalid or expired token")
        with pytest.raises(HTTPException) as info:
            await middleware._validate_system_admin_token_version(
                {"token_version": 0}, auth_exc,
            )
        assert info.value.status_code == 500
        assert "not a credential problem" in info.value.detail.lower()


class TestTheLookupItselfRefusesCorruptState:
    """The integration the helper tests do not reach.

    Testing ``_require_non_negative_version`` proves the parser rejects rubbish;
    it does NOT prove ``get_system_admin_token_version`` calls it rather than
    swallowing the failure. A mutation restoring the old ``return 0`` survived
    every other test in this file, which is exactly what that gap looks like.
    """

    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _DB:
        def __init__(self, value):
            self._value = value

        async def execute(self, *_a, **_kw):
            return TestTheLookupItselfRefusesCorruptState._Result(self._value)

        async def rollback(self):
            return None

    def _patch_db(self, monkeypatch, value):
        db = self._DB(value)

        async def _get_system_db():
            yield db

        monkeypatch.setattr(middleware, "get_system_db", _get_system_db,
                            raising=False)
        import shared.db.session as session_mod
        monkeypatch.setattr(session_mod, "get_system_db", _get_system_db)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stored", ["not-a-number", "", {"value": "oops"}])
    async def test_a_malformed_stored_version_raises(self, monkeypatch, stored):
        self._patch_db(monkeypatch, stored)
        with pytest.raises(middleware.SystemAdminTokenVersionCorrupt):
            await middleware.get_system_admin_token_version()

    @pytest.mark.asyncio
    async def test_an_absent_version_is_still_zero(self, monkeypatch):
        """Absent is legitimately 0 — a fresh system, or a pre-migration table.
        Only CORRUPT must refuse, or every new deployment would fail to log in."""
        self._patch_db(monkeypatch, None)
        assert await middleware.get_system_admin_token_version() == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stored,expected", [(0, 0), (5, 5), ("7", 7),
                                                 ({"value": 9}, 9)])
    async def test_a_readable_version_is_returned(self, monkeypatch, stored, expected):
        self._patch_db(monkeypatch, stored)
        assert await middleware.get_system_admin_token_version() == expected
