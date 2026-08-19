"""G2 / grok F-001-01 — JDBC session revalidation on long-lived connections.

Bug-7322 wired ``validate_session_upstream`` into the gateway auth handshake, but
its scope was connect-time only, and only for the JDBC direct-JWT branch. Two
residual gaps remained in live code:

1. The password-exchange auth branch issued a JWT via ``login_for_token`` and
   only ran ``verify_jwt_token`` (signature/expiry) — it never validated the
   session upstream, unlike the direct-JWT branch and both XMLA endpoints.
2. The two JDBC query paths (``_execute_for_extended`` for the extended
   protocol, ``_handle_user_query`` for the simple protocol) reused
   ``self._jwt_token`` for every ``execute_query`` with no gateway-side
   revocation check. A deactivated / demoted / token_version-bumped user kept
   querying on an already-open pooled connection until disconnect or JWT expiry.

These tests assert the fix: both query paths revalidate before dispatch (fail
CLOSED with SQLSTATE 28000 + connection close on revocation), the password-login
branch validates the session upstream, and a still-valid session (served from
the TTL cache) is not blocked. Revalidation must not fire for constant/metadata
SELECTs or driver housekeeping.
"""
from __future__ import annotations

import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.auth.base import TokenPayload
from src.jdbc import protocol as proto
from src.jdbc.server import PGWireServer, SessionRevokedError

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Wire / IO helpers (mirrors test_bug_6056_describe_single_execution.py)
# ---------------------------------------------------------------------------


def _fe(type_char: str, payload: bytes = b"") -> bytes:
    return type_char.encode() + struct.pack("!I", len(payload) + 4) + payload


def _query_msg(sql: str) -> bytes:
    return _fe("Q", sql.encode() + b"\x00")


def _parse_msg(sql: str, stmt: str = "") -> bytes:
    payload = stmt.encode() + b"\x00" + sql.encode() + b"\x00" + struct.pack("!H", 0)
    return _fe("P", payload)


def _bind_zero_param_msg() -> bytes:
    payload = (
        b"\x00" + b"\x00"
        + struct.pack("!H", 0)
        + struct.pack("!H", 0)
        + struct.pack("!H", 0)
    )
    return _fe("B", payload)


def _execute_msg() -> bytes:
    return _fe("E", b"\x00" + struct.pack("!I", 0))


def _sync_msg() -> bytes:
    return _fe("S")


def _terminate_msg() -> bytes:
    return _fe("X")


class _CollectWriter:
    """Minimal asyncio.StreamWriter stand-in that records emitted bytes."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    async def drain(self) -> None:
        return None


def _reader_for(*messages: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for msg in messages:
        reader.feed_data(msg)
    reader.feed_eof()
    return reader


def _make_authed_server() -> PGWireServer:
    """A server already past the handshake with a live token and no catalogue,
    so user queries reach the router dispatch path (and thus revalidation)."""
    server = PGWireServer()
    server._jwt_token = "ey.fake.jwt"
    server._tenant_slug = "acme"
    server._catalogue = None
    return server


def _stub_router(server: PGWireServer) -> list[int]:
    """Stub execute_query so a routed query returns a fixed result without a
    live source. Returns a mutable counter of router dispatches."""
    calls = [0]

    async def _fake_execute_query(**kwargs):
        calls[0] += 1
        return {"columns": [("id", "int4")], "rows": [["1"]], "route_type": "source"}

    # execute_query is called inside _run_cancellable; both paths import it into
    # the server module namespace.
    patch("src.jdbc.server.execute_query", _fake_execute_query).start()
    server._resolve_model_id_and_variant = lambda sql: ("model-1", False, None)  # type: ignore
    return calls


# ---------------------------------------------------------------------------
# Extended-protocol query path (_execute_for_extended)
# ---------------------------------------------------------------------------


class TestExtendedPathRevalidation:
    @pytest.mark.asyncio
    async def test_revoked_session_fails_closed_and_closes(self):
        """A revoked session mid-connection: the extended path emits SQLSTATE
        28000 and raises SessionRevokedError so handle_client closes the socket."""
        server = _make_authed_server()

        async def _revoked(token):
            raise ValueError("Session has been revoked")

        with patch("src.jdbc.server.execute_query", new=AsyncMock()) as exec_q, \
                patch("src.jdbc.server.validate_session_upstream", new=_revoked):
            server._resolve_model_id_and_variant = lambda sql: ("model-1", False, None)  # type: ignore
            reader = _reader_for(
                _parse_msg("SELECT amount FROM sales"),
                _bind_zero_param_msg(),
                _execute_msg(),
                _sync_msg(),
                _terminate_msg(),
            )
            writer = _CollectWriter()

            with pytest.raises(SessionRevokedError):
                await server._query_loop(reader, writer)

            # Fail CLOSED: 28000 reported to the client, router never invoked.
            assert b"28000" in writer.buf
            exec_q.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_session_dispatches_query(self):
        """A still-valid session (cache hit / 200) passes revalidation and the
        query is dispatched to the router exactly once."""
        server = _make_authed_server()
        calls = _stub_router(server)

        async def _ok(token):
            return None

        with patch("src.jdbc.server.validate_session_upstream", new=_ok):
            reader = _reader_for(
                _parse_msg("SELECT amount FROM sales"),
                _bind_zero_param_msg(),
                _execute_msg(),
                _sync_msg(),
                _terminate_msg(),
            )
            writer = _CollectWriter()
            await server._query_loop(reader, writer)

        assert calls[0] == 1, f"router dispatched {calls[0]} times, expected 1"
        patch.stopall()

    @pytest.mark.asyncio
    async def test_constant_select_does_not_revalidate(self):
        """A FROM-less literal SELECT is answered locally and must not trigger
        an upstream revalidation round-trip."""
        server = _make_authed_server()
        checked = [0]

        async def _count(token):
            checked[0] += 1
            return None

        with patch("src.jdbc.server.validate_session_upstream", new=_count):
            reader = _reader_for(
                _parse_msg("SELECT 1"),
                _bind_zero_param_msg(),
                _execute_msg(),
                _sync_msg(),
                _terminate_msg(),
            )
            writer = _CollectWriter()
            await server._query_loop(reader, writer)

        assert checked[0] == 0, "constant SELECT should not revalidate upstream"


# ---------------------------------------------------------------------------
# Simple-protocol query path (_handle_user_query)
# ---------------------------------------------------------------------------


class TestSimplePathRevalidation:
    @pytest.mark.asyncio
    async def test_revoked_session_fails_closed_and_closes(self):
        server = _make_authed_server()

        async def _revoked(token):
            raise ValueError("Session has been revoked")

        with patch("src.jdbc.server.execute_query", new=AsyncMock()) as exec_q, \
                patch("src.jdbc.server.validate_session_upstream", new=_revoked):
            server._resolve_model_id_and_variant = lambda sql: ("model-1", False, None)  # type: ignore
            writer = _CollectWriter()

            with pytest.raises(SessionRevokedError):
                await server._handle_user_query("SELECT amount FROM sales", writer)

            assert b"28000" in writer.buf
            exec_q.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_session_dispatches_query(self):
        server = _make_authed_server()
        calls = _stub_router(server)

        async def _ok(token):
            return None

        with patch("src.jdbc.server.validate_session_upstream", new=_ok):
            writer = _CollectWriter()
            await server._handle_user_query("SELECT amount FROM sales", writer)

        assert calls[0] == 1, f"router dispatched {calls[0]} times, expected 1"
        patch.stopall()


# ---------------------------------------------------------------------------
# _revalidate_session guard behavior
# ---------------------------------------------------------------------------


class TestRevalidateSessionGuard:
    @pytest.mark.asyncio
    async def test_no_token_is_noop(self):
        """With no token, the guard is a no-op (the caller's own 28000 guard
        handles the unauthenticated case)."""
        server = PGWireServer()
        server._jwt_token = ""
        called = [0]

        async def _count(token):
            called[0] += 1

        with patch("src.jdbc.server.validate_session_upstream", new=_count):
            await server._revalidate_session()  # must not raise, must not call

        assert called[0] == 0

    @pytest.mark.asyncio
    async def test_valueerror_becomes_session_revoked(self):
        server = _make_authed_server()

        async def _revoked(token):
            raise ValueError("token_version stale")

        with patch("src.jdbc.server.validate_session_upstream", new=_revoked):
            with pytest.raises(SessionRevokedError):
                await server._revalidate_session()


# ---------------------------------------------------------------------------
# Password-login auth branch (Gap 1)
# ---------------------------------------------------------------------------


class TestPasswordLoginBranchValidatesUpstream:
    def _governor(self):
        gov = MagicMock()
        gov.record_auth_success = MagicMock()
        gov.record_auth_failure = MagicMock()
        return gov

    @pytest.mark.asyncio
    async def test_password_login_revoked_session_denied(self):
        """A password login whose session is revoked upstream must be denied
        (returns False) even though model-service issued the JWT."""
        server = PGWireServer()
        gov = self._governor()

        async def _revoked(token):
            raise ValueError("Session has been revoked")

        with patch("src.jdbc.server.get_governor", return_value=gov), \
                patch("src.jdbc.server.proto.read_password_message",
                      new=AsyncMock(return_value="s3cret")), \
                patch("src.jdbc.server.login_for_token",
                      new=AsyncMock(return_value="issued.jwt.token")), \
                patch("src.jdbc.server.verify_jwt_token",
                      return_value=TokenPayload(sub="u@acme", tenant_id="acme", exp=0, extra={})), \
                patch("src.jdbc.server.validate_session_upstream", new=_revoked):
            writer = _CollectWriter()
            ok = await server._authenticate(
                {"database": "acme", "user": "u@acme"},
                _reader_for(),
                writer,
            )

        assert ok is False, "revoked session must not authenticate"
        gov.record_auth_success.assert_not_called()
        gov.record_auth_failure.assert_called_once()
        assert server._jwt_token == "", "revoked JWT must be cleared"

    @pytest.mark.asyncio
    async def test_password_login_valid_session_accepted(self):
        """A password login whose session is valid upstream authenticates."""
        server = PGWireServer()
        gov = self._governor()

        async def _ok(token):
            return None

        with patch("src.jdbc.server.get_governor", return_value=gov), \
                patch("src.jdbc.server.proto.read_password_message",
                      new=AsyncMock(return_value="s3cret")), \
                patch("src.jdbc.server.login_for_token",
                      new=AsyncMock(return_value="issued.jwt.token")), \
                patch("src.jdbc.server.verify_jwt_token",
                      return_value=TokenPayload(sub="u@acme", tenant_id="acme", exp=0, extra={})), \
                patch("src.jdbc.server.validate_session_upstream", new=_ok):
            writer = _CollectWriter()
            ok = await server._authenticate(
                {"database": "acme", "user": "u@acme"},
                _reader_for(),
                writer,
            )

        assert ok is True, "valid session must authenticate"
        gov.record_auth_success.assert_called_once()
        assert server._jwt_token == "issued.jwt.token"
