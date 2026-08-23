"""Wave C #2 — TLS-required-by-default + production startup validation.

Contract: TLS-required password/JWT auth is the complete JDBC auth contract.
TLS is required BY DEFAULT; a production gateway cannot START with password auth
and TLS disabled; local dev has ONE explicit opt-out
(GATEWAY_ALLOW_INSECURE_TRANSPORT).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.jdbc import protocol as proto
from src.jdbc.server import (
    PGWireServer,
    TransportSecurityError,
    validate_transport_security,
)


# ---------------------------------------------------------------------------
# Per-connection: sslmode=disable (plaintext) is rejected when TLS is required
# ---------------------------------------------------------------------------

class _Writer:
    def __init__(self) -> None:
        self.payload = b""

    def write(self, payload: bytes) -> None:
        self.payload += payload

    async def drain(self) -> None:
        pass


@pytest.mark.asyncio
async def test_plaintext_startup_rejected_when_required(monkeypatch):
    # sslmode=disable: a plaintext startup (no negotiated TLS) is refused with
    # SQLSTATE 28000 before any auth challenge, so the password never crosses the
    # wire. TLS is required by default; assert against that default explicitly.
    monkeypatch.setattr("src.jdbc.server.settings.GATEWAY_SSL_REQUIRED", True)
    monkeypatch.setattr("src.jdbc.server.settings.GATEWAY_ALLOW_INSECURE_TRANSPORT", False)
    monkeypatch.setattr("src.jdbc.server.settings.GATEWAY_SSL_ENABLED", False)

    async def startup(_reader):
        return {"type": "startup", "params": {"application_name": "generic"}}

    monkeypatch.setattr(proto, "read_startup", startup)
    server = PGWireServer()
    writer = _Writer()
    await server._run(object(), writer)

    assert b"C28000\x00" in writer.payload
    assert b"requires an SSL/TLS connection" in writer.payload


@pytest.mark.asyncio
async def test_local_insecure_opt_out_reaches_auth_when_tls_is_required(monkeypatch):
    """The explicit local-dev opt-out applies to the connection gate too."""
    monkeypatch.setattr("src.jdbc.server.settings.GATEWAY_SSL_REQUIRED", True)
    monkeypatch.setattr("src.jdbc.server.settings.GATEWAY_SSL_ENABLED", False)
    monkeypatch.setattr("src.jdbc.server.settings.GATEWAY_ALLOW_INSECURE_TRANSPORT", True)

    async def startup(_reader):
        return {"type": "startup", "params": {"application_name": "generic"}}

    reached = False

    async def authenticate(_params, _reader, _writer):
        nonlocal reached
        reached = True
        return False

    monkeypatch.setattr(proto, "read_startup", startup)
    server = PGWireServer()
    monkeypatch.setattr(server, "_authenticate", authenticate)
    writer = _Writer()
    await server._run(object(), writer)

    assert reached is True
    assert b"C28000\x00" not in writer.payload


@pytest.mark.asyncio
async def test_tls_active_startup_reaches_auth(monkeypatch):
    # sslmode=require path: once TLS is active the require-TLS gate passes and the
    # connection reaches authentication.
    monkeypatch.setattr("src.jdbc.server.settings.GATEWAY_SSL_REQUIRED", True)
    monkeypatch.setattr("src.jdbc.server.settings.GATEWAY_ALLOW_INSECURE_TRANSPORT", False)

    async def startup(_reader):
        return {"type": "startup", "params": {"application_name": "generic"}}

    reached = False

    async def authenticate(_params, _reader, _writer):
        nonlocal reached
        reached = True
        return False

    monkeypatch.setattr(proto, "read_startup", startup)
    server = PGWireServer()
    server._tls_active = True  # negotiated TLS channel
    monkeypatch.setattr(server, "_authenticate", authenticate)
    writer = _Writer()
    await server._run(object(), writer)

    assert reached is True
    assert b"C28000\x00" not in writer.payload


# ---------------------------------------------------------------------------
# Startup validation — production cannot start with password auth + TLS disabled
# ---------------------------------------------------------------------------

def _cfg(**over):
    base = dict(
        GATEWAY_ALLOW_INSECURE_TRANSPORT=False,
        GATEWAY_SSL_ENABLED=False,
        GATEWAY_SSL_REQUIRED=True,
        GATEWAY_SSL_CERT_FILE="",
        GATEWAY_SSL_KEY_FILE="",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_validate_rejects_production_tls_disabled():
    # Default production posture, no TLS, no opt-out → refuse to start.
    with pytest.raises(TransportSecurityError):
        validate_transport_security(_cfg())


def test_validate_rejects_required_false_without_opt_out():
    # TLS enabled with certs but SSL_REQUIRED=False and no opt-out → the exact
    # "password auth + plaintext accepted" case the gate exists to block.
    with pytest.raises(TransportSecurityError):
        validate_transport_security(_cfg(
            GATEWAY_SSL_ENABLED=True,
            GATEWAY_SSL_CERT_FILE="/certs/x.crt",
            GATEWAY_SSL_KEY_FILE="/certs/x.key",
            GATEWAY_SSL_REQUIRED=False,
        ))


def test_validate_rejects_enabled_without_cert():
    with pytest.raises(TransportSecurityError):
        validate_transport_security(_cfg(GATEWAY_SSL_ENABLED=True))


def test_validate_passes_production_tls_enabled():
    # Provisioned TLS, required, with cert/key → starts.
    validate_transport_security(_cfg(
        GATEWAY_SSL_ENABLED=True,
        GATEWAY_SSL_REQUIRED=True,
        GATEWAY_SSL_CERT_FILE="/certs/x.crt",
        GATEWAY_SSL_KEY_FILE="/certs/x.key",
    ))


def test_validate_allows_explicit_insecure_opt_out():
    # Local-dev opt-out: plaintext is permitted, no exception.
    validate_transport_security(_cfg(GATEWAY_ALLOW_INSECURE_TRANSPORT=True))


def test_transport_error_is_not_swallowed_by_except_exception():
    # The lifespan wraps start_jdbc_server in `except Exception`. A refuse-to-start
    # must PROPAGATE past that handler (else the gateway comes up degraded — XMLA
    # serving, no JDBC — instead of refusing), so it is a BaseException, NOT an
    # Exception. Guards the introduced-then-fixed swallow bug.
    assert issubclass(TransportSecurityError, BaseException)
    assert not issubclass(TransportSecurityError, Exception)


@pytest.mark.asyncio
async def test_lifespan_refuses_to_start_when_insecure(monkeypatch):
    # End-to-end: an insecure production posture (no opt-out, TLS disabled) makes
    # the gateway lifespan REFUSE to start — the error is not swallowed by the
    # lifespan's broad `except Exception` around start_jdbc_server.
    from src import main as gw_main

    monkeypatch.setattr("src.jdbc.server.settings.GATEWAY_ALLOW_INSECURE_TRANSPORT", False)
    monkeypatch.setattr("src.jdbc.server.settings.GATEWAY_SSL_ENABLED", False)
    monkeypatch.setattr("src.jdbc.server.settings.GATEWAY_SSL_REQUIRED", True)

    async def _noop():
        return None

    monkeypatch.setattr(gw_main, "refresh_system_snapshot", _noop)

    with pytest.raises(TransportSecurityError):
        async with gw_main.lifespan(object()):
            pass
