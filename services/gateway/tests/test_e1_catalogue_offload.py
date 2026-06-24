"""E1 / F-001-16 — catalogue SQLite runs off the event loop.

The per-connection catalogue is a synchronous in-memory SQLite engine. A
DBeaver metadata flood on one connection must not block the single asyncio
event loop (and therefore every other connection). The query path offloads
``CatalogueDB.execute`` to a worker thread via ``asyncio.to_thread``.

This is a wiring assertion: the synchronous ``execute`` calls in the async
query handlers must be dispatched through ``asyncio.to_thread`` so a refactor
that re-introduces a blocking call is caught.
"""
from __future__ import annotations

import inspect

from src.jdbc import server


def test_catalogue_execute_offloaded_in_async_handlers():
    src = inspect.getsource(server)
    # Both async-context catalogue executions must go through to_thread.
    assert "asyncio.to_thread(self._catalogue.execute" in src
    # And the blocking direct call must not have crept back into the handlers.
    assert "result = self._catalogue.execute(sql)" not in src
