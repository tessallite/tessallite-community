"""Bug-8974: a JDBC probe must observe the ACCEPT LOOP, not just the bind.

Why this file exists
--------------------
Nine places in this repo asserted "the gateway JDBC surface is alive" by
opening a TCP connection to port 5433. All nine were wrong in the same way,
and the first test below is the demonstration: a socket that is bound and
``listen()``ing but whose owner never calls ``accept()`` completes the TCP
three-way handshake in the KERNEL, inside the listen backlog. Every one of
those probes reported healthy against it. That is the "gateway reports
health=healthy while every host connection to 5433 times out" incident
recorded as Bug-8533 consequence (2).

``test_a_tcp_connect_cannot_tell_a_dead_accept_loop_from_a_live_one`` is the
fails-before-passes-after evidence for the whole change: it drives the OLD
probe shape and the NEW one against the SAME dead listener and asserts they
disagree. If the new probe were no better than the old one, this test goes red.

Execution scope: isolated (no DB, no live services, ephemeral loopback ports).
Gate tier: T2 (regression guard for a documented bug).
"""
from __future__ import annotations

import asyncio
import contextlib
import socket

import pytest

from shared.gateway_liveness import (
    SSL_REQUEST,
    probe_jdbc_accept_loop,
    probe_jdbc_accept_loop_async,
)


def _old_tcp_connect_probe(host: str, port: int, timeout: float = 3.0) -> bool:
    """The probe shape this bug is about, reproduced verbatim.

    This is what ``tests/e2e/conftest.py::_is_port_open``,
    ``tests/live/conftest.py`` preflight check 7,
    ``deploy/fast-rebuild-deploy/smoke_check.py::check_jdbc_port``,
    ``deploy/community/healthcheck.sh::probe_tcp`` and the compose healthcheck
    all did. Kept here deliberately: the guard is only meaningful if the thing
    it is compared against is the real prior art.
    """
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


@pytest.fixture
def dead_accept_loop() -> int:
    """A bound, listening socket whose owner never accepts. Yields its port.

    This is the exact runtime shape of the defect: ``asyncio``'s accept path
    handles EMFILE/ENFILE/ENOBUFS/ENOMEM by removing the listening socket's
    reader and retrying later, so under sustained fd exhaustion the socket
    stays bound and ``asyncio.Server.is_serving()`` keeps returning True while
    nothing is ever dequeued.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(100)  # asyncio's default backlog
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()


def test_a_tcp_connect_cannot_tell_a_dead_accept_loop_from_a_live_one(
    dead_accept_loop,
):
    """The whole premise of Bug-8974, proven rather than asserted.

    Fails before the fix by construction: the old probe is the only thing
    available before the fix, and it reports HEALTHY here. The new probe must
    report DEAD against the same socket, and must name the reason.
    """
    port = dead_accept_loop

    assert _old_tcp_connect_probe("127.0.0.1", port) is True, (
        "the premise of this bug did not reproduce: a TCP connect to a bound "
        "socket with no accept loop was expected to SUCCEED in the kernel "
        "backlog. If this ever fails, the platform changed and the whole "
        "Bug-8974 rationale needs re-deriving."
    )

    alive, detail = probe_jdbc_accept_loop("127.0.0.1", port, timeout=1.0)
    assert alive is False, (
        "the SSLRequest probe reported a dead accept loop as alive — it is no "
        "better than the TCP connect it replaced and Bug-8974 is not fixed"
    )
    assert "backlog" in detail, (
        f"the probe must say WHY, so an operator is not left guessing: {detail}"
    )


def test_the_probe_rejects_a_port_that_is_not_the_gateway(dead_accept_loop):
    """A foreign process answering on 5433 must not read as a live gateway.

    ``_SSL_REPLIES`` is an enumerated set rather than "any byte" precisely so
    that something else bound to the JDBC port is a probe FAILURE.
    """

    async def _serve_garbage(port_holder: list[int]) -> None:
        async def handler(reader, writer):
            await reader.read(8)
            writer.write(b"HTTP/1.1")  # not an S/N/E reply
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port_holder.append(server.sockets[0].getsockname()[1])
        async with server:
            await asyncio.sleep(2.0)

    async def _run() -> tuple[bool, str]:
        holder: list[int] = []
        task = asyncio.create_task(_serve_garbage(holder))
        while not holder:
            await asyncio.sleep(0.01)
        try:
            return await asyncio.to_thread(
                probe_jdbc_accept_loop, "127.0.0.1", holder[0], 2.0
            )
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    alive, detail = asyncio.run(_run())
    assert alive is False, "a non-PostgreSQL listener was mistaken for the gateway"
    assert "not an 'S'/'N'/'E' reply" in detail, detail


@pytest.mark.asyncio
async def test_probe_reports_alive_against_the_real_jdbc_listener(monkeypatch):
    """The other half: the probe must not be a false NEGATIVE on a live gateway.

    A false negative is worse than the false positive being fixed here — it
    would restart a working service — so the real ``start_jdbc_server()``
    listener is driven through the real serve loop and the probe must pass.
    """
    import src.jdbc.server as jdbc_server_module
    import src.main as gw

    monkeypatch.setattr(jdbc_server_module.settings, "JDBC_PORT", 0, raising=False)
    monkeypatch.setattr(
        jdbc_server_module.settings, "GATEWAY_SSL_ENABLED", False, raising=False
    )

    server = await jdbc_server_module.start_jdbc_server()
    port = server.sockets[0].getsockname()[1]
    serve_task = asyncio.create_task(gw._jdbc_serve(server))
    try:
        alive, detail = await probe_jdbc_accept_loop_async("127.0.0.1", port, 3.0)
        assert alive is True, f"the live JDBC listener was reported dead: {detail}"
    finally:
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()
        serve_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serve_task


def test_the_probe_sends_the_wire_bytes_a_real_client_sends():
    """No bespoke protocol: the probe must send the standard PG SSLRequest.

    The point of the decision this implements was to reuse what BI clients
    already do rather than invent a synthetic handshake, so the exact eight
    bytes are pinned.
    """
    assert SSL_REQUEST == b"\x00\x00\x00\x08\x04\xd2\x16\x2f"


@pytest.mark.asyncio
async def test_health_reports_jdbc_dead_with_503(monkeypatch, dead_accept_loop):
    """/health is the PRODUCER every other probe now consumes.

    Mutation: drop the ``response.status_code = 503`` line in ``src/main.py``
    -> the compose healthcheck, the Helm livenessProbe and the GCP VM deploy
    gate all keep passing against a wedged gateway (they act on the status
    code, not the body) -> red here.
    """
    from fastapi import Response

    import src.main as gw

    monkeypatch.setattr(gw.settings, "JDBC_PORT", dead_accept_loop, raising=False)
    monkeypatch.setattr(
        gw.settings, "GATEWAY_HEALTH_JDBC_PROBE_ENABLED", True, raising=False
    )
    monkeypatch.setattr(
        gw.settings, "GATEWAY_HEALTH_JDBC_PROBE_TIMEOUT_SECONDS", 1.0, raising=False
    )

    response = Response()
    body = await gw.health(response)

    assert body["jdbc_listening"] is False, body
    assert body["status"] == "degraded", body
    assert response.status_code == 503, (
        "a gateway whose JDBC accept loop is dead answered /health with "
        f"{response.status_code} — every consumer of this endpoint is still blind"
    )


@pytest.mark.asyncio
async def test_health_reports_jdbc_alive_against_the_real_listener(monkeypatch):
    """The producer must not fail closed on a healthy gateway either."""
    import src.jdbc.server as jdbc_server_module
    import src.main as gw
    from fastapi import Response

    monkeypatch.setattr(jdbc_server_module.settings, "JDBC_PORT", 0, raising=False)
    monkeypatch.setattr(
        jdbc_server_module.settings, "GATEWAY_SSL_ENABLED", False, raising=False
    )
    server = await jdbc_server_module.start_jdbc_server()
    port = server.sockets[0].getsockname()[1]
    serve_task = asyncio.create_task(gw._jdbc_serve(server))

    monkeypatch.setattr(gw.settings, "JDBC_PORT", port, raising=False)
    monkeypatch.setattr(
        gw.settings, "GATEWAY_HEALTH_JDBC_PROBE_ENABLED", True, raising=False
    )
    try:
        response = Response()
        body = await gw.health(response)
        assert body["jdbc_listening"] is True, body
        assert body["status"] == "ok", body
        assert response.status_code in (None, 200), response.status_code
    finally:
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()
        serve_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serve_task


@pytest.mark.asyncio
async def test_health_never_claims_jdbc_liveness_it_did_not_prove(monkeypatch):
    """With the probe disabled, ``jdbc_listening`` must be null, never true.

    The disabled path exists for a deployment that does not serve JDBC. It
    must degrade to "unproven", because a consumer reading ``true`` would be
    reading a claim nothing established — the same false assurance in a new
    place. ``deploy/community/healthcheck.sh`` treats null as a FAILURE for
    this reason.
    """
    from fastapi import Response

    import src.main as gw

    monkeypatch.setattr(
        gw.settings, "GATEWAY_HEALTH_JDBC_PROBE_ENABLED", False, raising=False
    )
    response = Response()
    body = await gw.health(response)

    assert body["jdbc_listening"] is None, body
    assert body["status"] == "ok", body
    assert response.status_code in (None, 200), response.status_code
