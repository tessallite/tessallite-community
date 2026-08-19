"""Behavioural guards for the gateway's JDBC listener lifecycle (Bug-8533).

Why this file exists
--------------------
Bug-8533 reported that the gateway "stops accepting host connections while the
container stays healthy", and named a lost client connection as the trigger:
the gateway log ended with ``JDBC handler error (pid=33): Connection lost`` and
recorded no further JDBC activity. Bug-8797 then recorded that the proposed
remediation's own tests never established that causal chain — they monkeypatched
``asyncio.Server.serve_forever`` to raise a synthetic ``OSError`` and asserted
only that a restart function had been called.

These two tests replace that with observable behaviour and no mocks of the
boundary under test:

1. ``test_listener_accepts_a_new_client_after_connections_are_lost`` drives the
   REAL ``start_jdbc_server()`` listener through the REAL ``_jdbc_serve`` serve
   loop, destroys more clients with TCP RSTs (the "Connection lost" path) than
   the per-IP admission cap admits, and then proves a further client still
   completes a real PostgreSQL-wire ``SSLRequest`` exchange. That is the
   user-visible outcome Bug-8533 is about: a BI client can still connect.

2. ``test_lifespan_releases_the_jdbc_port_on_shutdown`` pins the listener's
   OWNERSHIP contract: the gateway's lifespan is the only creator and the only
   closer of the JDBC listener, so after the lifespan exits the port must be
   free. This is the guard for the refusal recorded against Bug-8818 — any
   future listener that the lifespan's close/cancel pair does not reach (for
   instance one recreated into a serve loop's local variable) stays bound, and
   this test goes red with ``EADDRINUSE``.

Execution scope: isolated (no DB, no live services, ephemeral loopback ports).
Gate tier: T2 (regression guard for a documented bug).
"""
from __future__ import annotations

import asyncio
import contextlib
import socket
import struct

import pytest

# PostgreSQL v3 SSLRequest: int32 length (8) + int32 request code (80877103).
# It is the first thing a libpq/JDBC client sends and it is answered pre-auth
# with a single byte ('S' accept / 'N' deny), so it proves the accept loop
# dequeued the connection AND the real handler ran — without any credentials.
_SSL_REQUEST = struct.pack("!II", 8, 80877103)


def _free_port() -> int:
    """Reserve and immediately release an ephemeral port number.

    Reserved on the WILDCARD address because ``start_jdbc_server()`` binds
    ``0.0.0.0`` — reserving on loopback only would not prove the wildcard bind
    can succeed.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("0.0.0.0", 0))
        return probe.getsockname()[1]


async def _ssl_request_reply(port: int, timeout: float = 5.0) -> bytes:
    """Open a real client, send SSLRequest, return the server's reply byte."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", port), timeout=timeout
    )
    try:
        writer.write(_SSL_REQUEST)
        await writer.drain()
        return await asyncio.wait_for(reader.readexactly(1), timeout=timeout)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


@pytest.fixture(autouse=True)
def _isolate_connection_governor():
    """Rebuild the process-global per-IP admission governor around each test.

    These tests deliberately drive many loopback connections through the real
    admission path, and ``jdbc.throttle._governor`` is a module singleton — a
    leaked slot here would silently change the verdict of any later test in the
    suite. The module is importable under two names (see ``conftest.py``), so
    both are reset.
    """
    resets = []
    for name in ("src.jdbc.throttle", "jdbc.throttle"):
        try:
            resets.append(__import__(name, fromlist=["_reset_for_tests"])._reset_for_tests)
        except Exception:
            continue
    for reset in resets:
        reset()
    yield
    for reset in resets:
        reset()


@pytest.fixture
def _jdbc_listener_env(monkeypatch):
    """Bind the JDBC listener on an ephemeral port with TLS off.

    The real ``start_jdbc_server()`` reads ``settings.JDBC_PORT`` and the SSL
    settings at call time, so the tests exercise the production function rather
    than a stand-in ``asyncio.start_server``.
    """
    import src.jdbc.server as jdbc_server_module

    monkeypatch.setattr(jdbc_server_module.settings, "JDBC_PORT", 0, raising=False)
    monkeypatch.setattr(
        jdbc_server_module.settings, "GATEWAY_SSL_ENABLED", False, raising=False
    )
    return jdbc_server_module


def _abort_connection(port: int) -> None:
    """Open a JDBC connection and destroy it with a RST mid-startup-packet.

    ``SO_LINGER`` with a zero timeout makes ``close()`` send RST instead of
    FIN, which is what produces the ``JDBC handler error (pid=N): Connection
    lost`` line the Bug-8533 incident report cites — a client that vanished,
    not one that said goodbye.
    """
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sock.sendall(_SSL_REQUEST[:4])  # half a frame, then vanish
    finally:
        sock.close()


@pytest.mark.asyncio
async def test_listener_accepts_a_new_client_after_connections_are_lost(
    _jdbc_listener_env,
):
    """Bug-8533: losing JDBC clients must not stop the listener accepting.

    More connections are aborted than the per-IP concurrency cap admits, so the
    test fails for either of the two ways this surface can stop serving BI
    clients: the accept loop dying, or an admission slot leaking on the
    connection-lost path. The assertion is on the observable outcome — a later
    client gets a valid SSLRequest reply — not on any internal call.

    Mutation: drop ``governor.release(self._peer_ip)`` from ``handle_client``'s
    ``finally`` (jdbc/server.py) -> every aborted connection permanently holds a
    slot, the final client is refused with an ErrorResponse ('E') instead of an
    SSL reply -> red.
    """
    import src.main as gw
    from shared.config.settings import get_settings

    server = await _jdbc_listener_env.start_jdbc_server()
    port = server.sockets[0].getsockname()[1]
    serve_task = asyncio.create_task(gw._jdbc_serve(server))
    try:
        assert await _ssl_request_reply(port) in (b"S", b"N"), (
            "the JDBC listener did not answer a first client's SSLRequest"
        )

        # Derived from the deployed setting rather than hardcoded, so the test
        # keeps exceeding the cap if the cap is retuned.
        losses = get_settings().GATEWAY_JDBC_MAX_CONN_PER_IP + 2
        for _ in range(losses):
            _abort_connection(port)
            await asyncio.sleep(0)  # let each handler observe the RST
        await asyncio.sleep(0.5)  # settle the connection-lost handlers

        assert await _ssl_request_reply(port) in (b"S", b"N"), (
            f"Bug-8533: the JDBC listener stopped serving new clients after "
            f"{losses} connections were lost — a BI client can no longer "
            f"connect while the XMLA surface still reports healthy"
        )
        assert not serve_task.done(), "the JDBC serve loop exited on its own"
    finally:
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()
        serve_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serve_task


@pytest.mark.asyncio
async def test_lifespan_releases_the_jdbc_port_on_shutdown(monkeypatch):
    """Bug-8818: the lifespan must own EVERY listener it is responsible for.

    Ownership is asserted by its only observable consequence: once the lifespan
    context has exited, nothing in this process is still bound to the JDBC port,
    so a fresh bind succeeds. It also proves the lifespan brings the REAL
    ``start_jdbc_server()`` listener up far enough to answer a real client,
    which the fake-server drain tests in ``test_shutdown_jdbc_drain.py`` cannot.

    Mutation: drop BOTH ``jdbc_server.close()`` and the ``jdbc_task.cancel()``
    block from the lifespan shutdown -> the listener is owned by nobody, the
    rebind fails with ``EADDRINUSE`` -> red. (Dropping only one of the two stays
    green, because ``Server.close()`` cancels the ``serve_forever`` future and
    ``serve_forever``'s own cancellation handler closes the server — the two
    paths are deliberately redundant.)
    """
    import src.jdbc.server as jdbc_server_module
    import shared.source_pool as source_pool
    import src.main as gw

    port = _free_port()
    monkeypatch.setattr(jdbc_server_module.settings, "JDBC_PORT", port, raising=False)
    monkeypatch.setattr(
        jdbc_server_module.settings, "GATEWAY_SSL_ENABLED", False, raising=False
    )
    monkeypatch.setattr(gw, "refresh_system_snapshot", lambda: asyncio.sleep(0))

    pools_closed = asyncio.Event()

    async def _fake_close_all_pools():
        pools_closed.set()

    monkeypatch.setattr(source_pool, "close_all_pools", _fake_close_all_pools)

    async with gw.lifespan(gw.app):
        assert await _ssl_request_reply(port) in (b"S", b"N"), (
            "the lifespan did not bring up a usable JDBC listener"
        )

    assert pools_closed.is_set(), "the lifespan never reached close_all_pools()"

    rebind = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        rebind.bind(("0.0.0.0", port))
    except OSError as exc:  # pragma: no cover - the failure this test exists for
        pytest.fail(
            f"the JDBC port {port} is still bound after the gateway lifespan "
            f"shut down ({exc}) — a listener escaped the lifespan's ownership"
        )
    finally:
        rebind.close()
