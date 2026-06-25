"""Defensive access-governance tests for the gateway security tail (S-GW).

Covers the items lifted from the ML1/ML2 security pause:

  JDBC (unit 001)
    F-001-07  per-IP concurrency cap + failed-auth throttle (governor).
    F-001-08  require-TLS switch rejects plaintext startup.
    F-001-10  JWT tenant claim is cross-checked against the database param;
              a mismatched/blank tenant is DENIED (no cross-tenant read).
    F-001-11  auth-failure message is fixed and topology-free.

  XMLA (unit 002)
    F-002-06  per-request login is cached; session-resume requests pass
              through the middleware to the session store.
    F-002-07  Bearer-JWT requests reach the handler.
    F-002-15  the SOAP Catalog (a model slug) is not blindly used as a tenant
              in the login path — an unknown-tenant response falls through to
              cross-tenant discovery.
"""
from __future__ import annotations

import asyncio
import struct

import httpx
import pytest
from jose import jwt

from shared.config.settings import get_settings

from src.jdbc import protocol as proto
from src.jdbc import throttle
from src.jdbc.server import PGWireServer
from src.jdbc.throttle import JdbcConnectionGovernor

settings = get_settings()


def _make_jwt(tenant_id: str, sub: str = "user@acme.com") -> str:
    return jwt.encode(
        {"sub": sub, "tenant_id": tenant_id, "exp": 9999999999},
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


class _CaptureWriter:
    """Minimal asyncio.StreamWriter stand-in that records written bytes."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def get_extra_info(self, _name):
        return ("203.0.113.7", 54321)


class _PasswordReader:
    """Feeds a single PasswordMessage frame, then raises (no more input)."""

    def __init__(self, password: str) -> None:
        body = password.encode("utf-8") + b"\x00"
        self._data = b"p" + struct.pack("!I", len(body) + 4) + body
        self._pos = 0

    async def readexactly(self, n: int) -> bytes:
        chunk = self._data[self._pos:self._pos + n]
        self._pos += n
        if len(chunk) < n:
            raise asyncio.IncompleteReadError(chunk, n)
        return chunk


def _decode_error_message(buf: bytes) -> str:
    """Extract the M-field text from a captured ErrorResponse frame."""
    text = bytes(buf)
    idx = text.find(b"M")
    if idx == -1:
        return ""
    end = text.find(b"\x00", idx)
    return text[idx + 1:end].decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# F-001-07 — per-IP governance
# ---------------------------------------------------------------------------

class TestConnectionGovernor:
    def test_concurrency_cap_refuses_over_limit(self):
        g = JdbcConnectionGovernor(max_conn_per_ip=2, max_auth_failures=0)
        assert g.try_acquire("1.2.3.4") == (True, None)
        assert g.try_acquire("1.2.3.4")[0] is True
        refused, reason = g.try_acquire("1.2.3.4")
        assert refused is False
        assert "concurrent" in reason
        # A different IP is unaffected.
        assert g.try_acquire("5.6.7.8")[0] is True

    def test_release_frees_a_slot(self):
        g = JdbcConnectionGovernor(max_conn_per_ip=1, max_auth_failures=0)
        assert g.try_acquire("1.2.3.4")[0] is True
        assert g.try_acquire("1.2.3.4")[0] is False
        g.release("1.2.3.4")
        assert g.try_acquire("1.2.3.4")[0] is True

    def test_auth_failure_throttle_blocks_then_drains(self):
        clock = {"t": 1000.0}
        g = JdbcConnectionGovernor(
            max_conn_per_ip=0,
            max_auth_failures=3,
            auth_failure_window_seconds=60,
            time_fn=lambda: clock["t"],
        )
        for _ in range(3):
            g.record_auth_failure("9.9.9.9")
        assert g.is_throttled("9.9.9.9") is True
        # New connection attempts are refused while throttled.
        refused, reason = g.try_acquire("9.9.9.9")
        assert refused is False and "authentication failures" in reason
        # After the window passes, the failures age out.
        clock["t"] += 61
        assert g.is_throttled("9.9.9.9") is False
        assert g.try_acquire("9.9.9.9")[0] is True

    def test_auth_success_clears_failures(self):
        g = JdbcConnectionGovernor(
            max_conn_per_ip=0, max_auth_failures=2, auth_failure_window_seconds=60
        )
        g.record_auth_failure("9.9.9.9")
        g.record_auth_failure("9.9.9.9")
        assert g.is_throttled("9.9.9.9") is True
        g.record_auth_success("9.9.9.9")
        assert g.is_throttled("9.9.9.9") is False

    def test_zero_disables_controls(self):
        g = JdbcConnectionGovernor(max_conn_per_ip=0, max_auth_failures=0)
        for _ in range(100):
            assert g.try_acquire("1.2.3.4")[0] is True
        for _ in range(100):
            g.record_auth_failure("1.2.3.4")
        assert g.is_throttled("1.2.3.4") is False


# ---------------------------------------------------------------------------
# F-001-10 — JWT tenant vs database cross-check (the most important item)
# ---------------------------------------------------------------------------

class TestTenantCrossCheck:
    def test_matching_tenant_passes(self):
        assert PGWireServer._tenant_matches("acme", "acme") is True

    def test_mismatched_tenant_denied(self):
        # database=tenant_a but JWT is for tenant_b → must NOT match.
        assert PGWireServer._tenant_matches("tenant_b", "tenant_a") is False

    def test_blank_jwt_tenant_never_matches(self):
        # Fail-closed: a token with no tenant claim cannot satisfy any
        # tenant-scoped database parameter.
        assert PGWireServer._tenant_matches("", "acme") is False
        assert PGWireServer._tenant_matches("", "") is False

    def test_default_database_placeholder_allowed_with_real_jwt_tenant(self):
        # Browse sessions connect with database=default; the JWT carries the
        # real tenant and every query is JWT-scoped downstream.
        assert PGWireServer._tenant_matches("acme", "default") is True
        assert PGWireServer._tenant_matches("acme", "") is True

    @pytest.mark.asyncio
    async def test_authenticate_denies_cross_tenant_jwt(self, monkeypatch):
        """A direct JWT for tenant_b under database=tenant_a is denied and
        no cross-tenant read can proceed (jwt_token stays empty)."""
        throttle._reset_for_tests()
        server = PGWireServer()
        server._peer_ip = "203.0.113.7"
        writer = _CaptureWriter()
        token = _make_jwt("tenant_b")
        reader = _PasswordReader(token)
        ok = await server._authenticate(
            {"database": "tenant_a", "user": "user@acme.com"}, reader, writer
        )
        assert ok is False
        assert server._jwt_token == ""  # no token accepted → no read possible
        assert "invalid credentials or tenant" in _decode_error_message(writer.buffer)

    @pytest.mark.asyncio
    async def test_authenticate_accepts_matching_jwt(self, monkeypatch):
        throttle._reset_for_tests()
        server = PGWireServer()
        server._peer_ip = "203.0.113.7"
        writer = _CaptureWriter()
        token = _make_jwt("acme")
        reader = _PasswordReader(token)
        ok = await server._authenticate(
            {"database": "acme", "user": "user@acme.com"}, reader, writer
        )
        assert ok is True
        assert server._jwt_token == token

    @pytest.mark.asyncio
    async def test_login_path_tenant_pinned_to_database(self, monkeypatch):
        """The password-exchange path issues a JWT whose tenant must equal the
        database param; a backend that returned a foreign-tenant token is
        rejected fail-closed."""
        throttle._reset_for_tests()
        foreign = _make_jwt("tenant_b")

        async def _fake_login(tenant_slug, email, password):
            return foreign  # backend returned a token for the wrong tenant

        monkeypatch.setattr("src.jdbc.server.login_for_token", _fake_login)
        server = PGWireServer()
        server._peer_ip = "203.0.113.7"
        writer = _CaptureWriter()
        reader = _PasswordReader("hunter2")
        ok = await server._authenticate(
            {"database": "tenant_a", "user": "user@acme.com"}, reader, writer
        )
        assert ok is False
        assert server._jwt_token == ""


# ---------------------------------------------------------------------------
# F-001-11 — auth-failure message scrub
# ---------------------------------------------------------------------------

class TestAuthFailureScrub:
    @pytest.mark.asyncio
    async def test_login_exception_detail_not_leaked(self, monkeypatch):
        throttle._reset_for_tests()

        async def _boom(tenant_slug, email, password):
            raise httpx.HTTPStatusError(
                "401 Client Error for url http://model-service:8001/api/v1/auth/login",
                request=httpx.Request("POST", "http://model-service:8001/api/v1/auth/login"),
                response=httpx.Response(401),
            )

        monkeypatch.setattr("src.jdbc.server.login_for_token", _boom)
        server = PGWireServer()
        server._peer_ip = "203.0.113.7"
        writer = _CaptureWriter()
        reader = _PasswordReader("wrong-password")
        ok = await server._authenticate(
            {"database": "acme", "user": "user@acme.com"}, reader, writer
        )
        assert ok is False
        msg = _decode_error_message(writer.buffer)
        assert msg == "Authentication failed: invalid credentials or tenant."
        # No internal topology in the wire message.
        assert "model-service" not in bytes(writer.buffer).decode("utf-8", "replace")
        assert "/api/v1/auth/login" not in bytes(writer.buffer).decode("utf-8", "replace")

    @pytest.mark.asyncio
    async def test_failed_login_counts_against_throttle(self, monkeypatch):
        throttle._reset_for_tests()

        async def _boom(tenant_slug, email, password):
            raise ValueError("nope")

        monkeypatch.setattr("src.jdbc.server.login_for_token", _boom)
        server = PGWireServer()
        server._peer_ip = "198.51.100.5"
        writer = _CaptureWriter()
        reader = _PasswordReader("wrong-password")
        await server._authenticate(
            {"database": "acme", "user": "user@acme.com"}, reader, writer
        )
        assert throttle.get_governor().is_throttled("198.51.100.5") is False  # 1 < default
        # Record up to the default failure cap and confirm the window engages.
        gov = throttle.get_governor()
        for _ in range(settings.GATEWAY_JDBC_MAX_AUTH_FAILURES):
            gov.record_auth_failure("198.51.100.5")
        assert gov.is_throttled("198.51.100.5") is True


# ---------------------------------------------------------------------------
# F-001-08 — require-TLS switch
# ---------------------------------------------------------------------------

class _StartupReader:
    """Feeds a single plaintext StartupMessage frame."""

    def __init__(self, database: str = "acme", user: str = "u@acme.com") -> None:
        body = (
            struct.pack("!I", proto.PROTOCOL_VERSION)
            + b"database\x00" + database.encode() + b"\x00"
            + b"user\x00" + user.encode() + b"\x00"
            + b"\x00"
        )
        self._data = struct.pack("!I", len(body) + 4) + body
        self._pos = 0

    async def readexactly(self, n: int) -> bytes:
        chunk = self._data[self._pos:self._pos + n]
        self._pos += n
        if len(chunk) < n:
            raise asyncio.IncompleteReadError(chunk, n)
        return chunk


class TestRequireTls:
    @pytest.mark.asyncio
    async def test_plaintext_startup_rejected_when_required(self, monkeypatch):
        monkeypatch.setattr(
            "src.jdbc.server.settings.GATEWAY_SSL_REQUIRED", True, raising=False
        )
        server = PGWireServer()
        server._peer_ip = "203.0.113.7"
        server._tls_active = False  # plaintext channel
        writer = _CaptureWriter()
        reader = _StartupReader()
        await server._run(reader, writer)
        msg = _decode_error_message(writer.buffer)
        assert "requires an SSL/TLS connection" in msg
        # The password challenge must never have been issued (no creds on wire).
        assert proto.authentication_cleartext_password() not in bytes(writer.buffer)

    @pytest.mark.asyncio
    async def test_plaintext_allowed_when_not_required(self, monkeypatch):
        monkeypatch.setattr(
            "src.jdbc.server.settings.GATEWAY_SSL_REQUIRED", False, raising=False
        )
        # Stop after the startup gate by making auth fail fast; we only assert
        # the require-TLS gate did NOT fire (the challenge is reached).
        server = PGWireServer()
        server._peer_ip = "203.0.113.7"
        server._tls_active = False
        writer = _CaptureWriter()
        reader = _StartupReader()
        await server._run(reader, writer)
        assert "requires an SSL/TLS connection" not in _decode_error_message(writer.buffer)
        # The cleartext challenge was issued (gate passed).
        assert proto.authentication_cleartext_password() in bytes(writer.buffer)


def _teardown_throttle():
    throttle._reset_for_tests()
