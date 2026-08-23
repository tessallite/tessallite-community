"""Behavioural guard for the gateway's shutdown drain (Bug-8041 R7 round-2 F4).

Round 2 of the R7 review proposed this test and it was not added at the time;
the only coverage was an AST assertion in the shared unit suite, which a
reviewer mutation (`timeout=3600`) walked straight through. This is the
behavioural half: a REAL ``asyncio.Server`` with a REAL client connection whose
handler never returns, driven through the REAL ``lifespan`` context manager.

What it protects: ``close_all_pools()`` is the last statement of the gateway's
lifespan shutdown. Every phase before it (the DAX session-store flush, then
``jdbc_server.wait_closed()``) must be bounded, or a single JDBC client
mid-query postpones the source-pool close indefinitely and the platform SIGKILLs
the container with authenticated source connections still open.

Execution scope: isolated (no DB, no live services). Gate tier: T1 (producer/
consumer contract between the shutdown budget and the gateway lifespan).
"""
from __future__ import annotations

import asyncio
import time

import pytest

from shared.config.shutdown_budget import resolve_shutdown_budget


@pytest.mark.asyncio
async def test_lifespan_reaches_close_all_pools_despite_a_stuck_jdbc_client(
    monkeypatch,
):
    """A JDBC handler that never returns must not stop the pool close.

    Mutation: remove the ``asyncio.wait_for`` around ``wait_closed()`` in
    ``src/main.py`` -> the lifespan never reaches ``close_all_pools`` and this
    test times out -> red.
    """
    import src.main as gw

    handler_started = asyncio.Event()

    async def _stuck_handler(reader, writer):
        handler_started.set()
        await asyncio.sleep(3600)  # a client mid-query at shutdown

    server = await asyncio.start_server(_stuck_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    closed_pools = asyncio.Event()

    async def _fake_close_all_pools():
        closed_pools.set()

    monkeypatch.setattr(gw, "refresh_system_snapshot", lambda: asyncio.sleep(0))
    monkeypatch.setattr(gw, "start_jdbc_server", lambda: asyncio.sleep(0, result=server))
    # close_all_pools is imported inside the lifespan body, so patch at source.
    import shared.source_pool as sp
    monkeypatch.setattr(sp, "close_all_pools", _fake_close_all_pools)

    app = gw.app

    async with gw.lifespan(app):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"hello")
        await writer.drain()
        await asyncio.wait_for(handler_started.wait(), timeout=5)
        # The handler is now parked; wait_closed() would block forever on it.

    # Exiting the context manager ran the shutdown body.
    assert closed_pools.is_set(), (
        "the lifespan never reached close_all_pools(): a stuck JDBC handler "
        "postponed the source-pool close past the shutdown budget"
    )

    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    server.close()


@pytest.mark.asyncio
async def test_pre_close_phases_share_one_bounded_deadline(monkeypatch):
    """The session-store flush and the JDBC drain must share ONE budget slice.

    Giving each its own timeout would let the pre-close work take 2x the funded
    slice and push the total past the platform window — the additive-phases
    defect this lane was already failed for once.

    Mutation: give the flush its own full-length timeout instead of
    ``_remaining()`` -> elapsed exceeds the slice -> red.
    """
    import src.main as gw
    import shared.source_pool as sp

    async def _slow_flush():
        await asyncio.sleep(3600)

    async def _stuck_handler(reader, writer):
        await asyncio.sleep(3600)

    server = await asyncio.start_server(_stuck_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    closed = asyncio.Event()
    monkeypatch.setattr(gw, "refresh_system_snapshot", lambda: asyncio.sleep(0))
    monkeypatch.setattr(gw, "start_jdbc_server", lambda: asyncio.sleep(0, result=server))
    monkeypatch.setattr(sp, "close_all_pools", lambda: _set(closed))

    import src.dax.session_store as store
    monkeypatch.setattr(store, "flush_now", _slow_flush)

    budget = resolve_shutdown_budget()
    slice_s = budget.pre_close_drain_seconds

    async with gw.lifespan(gw.app):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"x")
        await writer.drain()
        await asyncio.sleep(0.1)
        start = time.monotonic()
    elapsed = time.monotonic() - start

    assert closed.is_set(), "close_all_pools was never reached"
    # Both phases are stuck; together they must not exceed the funded slice
    # (plus a small scheduling margin).
    # slice_s is 1 at the default budget, so a +1.0 margin was 100% of the slice
    # and the named mutation (two independent 1s timeouts, ~2.0s) landed exactly
    # on the bound -- a coin flip on scheduling jitter.
    assert elapsed <= slice_s + 0.4, (
        f"pre-close phases took {elapsed:.2f}s against a {slice_s}s funded "
        f"slice — they are not sharing one deadline"
    )

    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    server.close()


async def _set(ev: asyncio.Event) -> None:
    ev.set()
