"""Tests for Gateway SSL/TLS support (Block H)."""
from __future__ import annotations

import asyncio
import ssl
import struct
from unittest.mock import patch

import pytest

from src.jdbc import protocol as proto
from src.jdbc.server import (
    PGWireServer,
    _client_kind_from_application_name,
    _client_kind_from_sql,
)


# ---------------------------------------------------------------------------
# Protocol-level SSL response tests
# ---------------------------------------------------------------------------

def test_ssl_deny_returns_n():
    assert proto.ssl_deny() == b"N"


def test_ssl_accept_returns_s():
    assert proto.ssl_accept() == b"S"


@pytest.mark.parametrize(
    ("application_name", "expected"),
    [
        ("Looker", "looker_cloud"),
        ("Looker JDBC", "looker_cloud"),
        ("Looker Studio", "looker_studio"),
        ("Data Studio", "looker_studio"),
        ("DBeaver", None),
    ],
)
def test_client_kind_from_application_name(application_name, expected):
    assert _client_kind_from_application_name(application_name) == expected


def test_generated_lookml_relation_is_conservative_cloud_signature():
    assert _client_kind_from_sql("SELECT payment_id FROM public.modelx__payment_transaction") == "looker_cloud"
    assert _client_kind_from_sql(
        "SELECT payment_id FROM public.modelx__payment_transaction",
        {"other__relation"},
    ) is None
    assert _client_kind_from_sql("SELECT payment_id FROM modelx") is None


@pytest.mark.asyncio
async def test_plaintext_looker_startup_is_rejected(monkeypatch):
    monkeypatch.setattr("src.jdbc.server.settings.LOOKER_GATEWAY_ENABLED", True)
    # This test exercises the LOOKER-specific TLS gate; disable the generic
    # require-TLS gate (Wave C #2 default) so it does not preempt with 28000.
    monkeypatch.setattr("src.jdbc.server.settings.GATEWAY_SSL_REQUIRED", False)
    server = PGWireServer()

    class Writer:
        def __init__(self) -> None:
            self.payload = b""

        def write(self, payload: bytes) -> None:
            self.payload += payload

        async def drain(self) -> None:
            pass

    async def startup(_reader):
        return {"type": "startup", "params": {"application_name": "Looker"}}

    monkeypatch.setattr(proto, "read_startup", startup)
    writer = Writer()
    await server._run(object(), writer)

    assert b"C08004\x00" in writer.payload
    assert b"require TLS" in writer.payload


@pytest.mark.asyncio
async def test_disabled_looker_support_rejects_declared_client_before_auth(monkeypatch):
    server = PGWireServer()

    class Writer:
        def __init__(self) -> None:
            self.payload = b""

        def write(self, payload: bytes) -> None:
            self.payload += payload

        async def drain(self) -> None:
            pass

    async def startup(_reader):
        return {"type": "startup", "params": {"application_name": "Looker"}}

    monkeypatch.setattr(proto, "read_startup", startup)
    monkeypatch.setattr("src.jdbc.server.settings.LOOKER_GATEWAY_ENABLED", False)
    # Exercises the Looker-disabled gate, not the generic require-TLS gate.
    monkeypatch.setattr("src.jdbc.server.settings.GATEWAY_SSL_REQUIRED", False)
    writer = Writer()
    await server._run(object(), writer)

    assert b"C0A000\x00" in writer.payload
    assert b"disabled" in writer.payload


@pytest.mark.asyncio
async def test_disabled_lookml_adapter_does_not_block_direct_data_studio_startup(monkeypatch):
    server = PGWireServer()

    class Writer:
        def __init__(self) -> None:
            self.payload = b""

        def write(self, payload: bytes) -> None:
            self.payload += payload

        async def drain(self) -> None:
            pass

    async def startup(_reader):
        return {"type": "startup", "params": {"application_name": "Data Studio"}}

    authenticated = False

    async def authenticate(_params, _reader, _writer):
        nonlocal authenticated
        authenticated = True
        return False

    monkeypatch.setattr(proto, "read_startup", startup)
    monkeypatch.setattr("src.jdbc.server.settings.LOOKER_GATEWAY_ENABLED", False)
    # Not-required scenario: TLS is available/optional here, so a plaintext
    # Data Studio startup must reach auth (client-kind gating only).
    monkeypatch.setattr("src.jdbc.server.settings.GATEWAY_SSL_REQUIRED", False)
    monkeypatch.setattr(server, "_authenticate", authenticate)
    writer = Writer()
    await server._run(object(), writer)

    assert authenticated is True
    assert b"disabled" not in writer.payload


@pytest.mark.asyncio
async def test_tls_enabled_allows_plaintext_generic_client_auth(monkeypatch):
    from src.jdbc import server as server_mod

    server = PGWireServer()

    class Writer:
        def __init__(self) -> None:
            self.payload = b""

        def write(self, payload: bytes) -> None:
            self.payload += payload

        async def drain(self) -> None:
            pass

    async def startup(_reader):
        return {"type": "startup", "params": {"application_name": "generic"}}

    authenticated = False

    async def authenticate(_params, _reader, _writer):
        nonlocal authenticated
        authenticated = True
        return False

    monkeypatch.setattr(proto, "read_startup", startup)
    monkeypatch.setattr(server_mod, "_ssl_context", object())
    # TLS is available but NOT required in this scenario, so a plaintext generic
    # client must still reach auth (Wave C #2: required is the default; here it is
    # explicitly relaxed).
    monkeypatch.setattr("src.jdbc.server.settings.GATEWAY_SSL_REQUIRED", False)
    monkeypatch.setattr(server, "_authenticate", authenticate)
    writer = Writer()
    await server._run(object(), writer)

    assert authenticated is True
    assert b"C08004\x00" not in writer.payload


@pytest.mark.asyncio
async def test_looker_tls_handshake_reaches_authentication(self_signed_cert, monkeypatch):
    from src.jdbc import server as server_mod
    from src.jdbc.server import _build_ssl_context

    cert_path, key_path = self_signed_cert
    with patch("src.jdbc.server.settings") as mock_settings:
        mock_settings.GATEWAY_SSL_ENABLED = True
        mock_settings.GATEWAY_SSL_CERT_FILE = cert_path
        mock_settings.GATEWAY_SSL_KEY_FILE = key_path
        mock_settings.GATEWAY_SSL_CA_FILE = ""
        mock_settings.LOOKER_GATEWAY_ENABLED = True
        server_context = _build_ssl_context()

    monkeypatch.setattr(server_mod, "_ssl_context", server_context)
    monkeypatch.setattr(server_mod.settings, "LOOKER_GATEWAY_ENABLED", True)
    listener = await asyncio.start_server(
        lambda reader, writer: PGWireServer().handle_client(reader, writer),
        "127.0.0.1",
        0,
    )
    port = listener.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(struct.pack("!II", 8, proto.SSL_REQUEST_CODE))
        await writer.drain()
        assert await reader.readexactly(1) == b"S"

        client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_context.check_hostname = False
        client_context.verify_mode = ssl.CERT_NONE
        await writer.start_tls(client_context, server_hostname=None)

        startup_payload = (
            struct.pack("!I", proto.PROTOCOL_VERSION)
            + b"user\x00test@example.com\x00database\x00tenant\x00"
            + b"application_name\x00Looker\x00\x00"
        )
        writer.write(struct.pack("!I", len(startup_payload) + 4) + startup_payload)
        await writer.drain()

        assert await reader.readexactly(1) == b"R"
        length = struct.unpack("!I", await reader.readexactly(4))[0]
        payload = await reader.readexactly(length - 4)
        assert struct.unpack("!I", payload)[0] == proto.AUTH_CLEARTEXT
    finally:
        writer.close()
        await writer.wait_closed()
        listener.close()
        await listener.wait_closed()


# ---------------------------------------------------------------------------
# SSL context builder tests
# ---------------------------------------------------------------------------

@pytest.fixture
def self_signed_cert(tmp_path):
    """Generate a self-signed cert+key pair for testing."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import datetime

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
        )
        .sign(key, hashes.SHA256())
    )

    cert_path = str(tmp_path / "cert.pem")
    key_path = str(tmp_path / "key.pem")

    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
    return cert_path, key_path


def test_build_ssl_context_disabled():
    from src.jdbc.server import _build_ssl_context
    with patch("src.jdbc.server.settings") as mock_settings:
        mock_settings.GATEWAY_SSL_ENABLED = False
        result = _build_ssl_context()
    assert result is None


def test_build_ssl_context_enabled_valid_cert(self_signed_cert):
    cert_path, key_path = self_signed_cert
    from src.jdbc.server import _build_ssl_context
    with patch("src.jdbc.server.settings") as mock_settings:
        mock_settings.GATEWAY_SSL_ENABLED = True
        mock_settings.GATEWAY_SSL_CERT_FILE = cert_path
        mock_settings.GATEWAY_SSL_KEY_FILE = key_path
        mock_settings.GATEWAY_SSL_CA_FILE = ""
        ctx = _build_ssl_context()
    assert isinstance(ctx, ssl.SSLContext)


def test_build_ssl_context_missing_cert_path():
    from src.jdbc.server import _build_ssl_context
    with patch("src.jdbc.server.settings") as mock_settings:
        mock_settings.GATEWAY_SSL_ENABLED = True
        mock_settings.GATEWAY_SSL_CERT_FILE = "/nonexistent/cert.pem"
        mock_settings.GATEWAY_SSL_KEY_FILE = "/nonexistent/key.pem"
        mock_settings.GATEWAY_SSL_CA_FILE = ""
        with pytest.raises(RuntimeError, match="cert file not found"):
            _build_ssl_context()


def test_build_ssl_context_enabled_but_no_paths():
    from src.jdbc.server import _build_ssl_context
    with patch("src.jdbc.server.settings") as mock_settings:
        mock_settings.GATEWAY_SSL_ENABLED = True
        mock_settings.GATEWAY_SSL_CERT_FILE = ""
        mock_settings.GATEWAY_SSL_KEY_FILE = ""
        mock_settings.GATEWAY_SSL_CA_FILE = ""
        with pytest.raises(RuntimeError, match="not set"):
            _build_ssl_context()


def test_build_ssl_context_with_ca(self_signed_cert):
    cert_path, key_path = self_signed_cert
    from src.jdbc.server import _build_ssl_context
    with patch("src.jdbc.server.settings") as mock_settings:
        mock_settings.GATEWAY_SSL_ENABLED = True
        mock_settings.GATEWAY_SSL_CERT_FILE = cert_path
        mock_settings.GATEWAY_SSL_KEY_FILE = key_path
        mock_settings.GATEWAY_SSL_CA_FILE = cert_path
        ctx = _build_ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_OPTIONAL
