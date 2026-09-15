"""Small bounded, cancellation-safe async single-flight coordination helper."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from typing import TypeVar


T = TypeVar("T")


async def run_singleflight(
    registry: dict[Hashable, asyncio.Task[T]],
    key: Hashable,
    loader: Callable[[], Awaitable[T]],
    *,
    max_entries: int,
) -> T:
    """Run one loader for each key while bounding the in-flight registry.

    Followers wait through ``asyncio.shield`` so cancellation of one caller
    never cancels work that may still be needed by another caller.  The task's
    done callback removes only the task it registered, which also makes a
    completed failure or cancellation immediately retryable.
    """
    task = registry.get(key)
    if task is not None and task.done():
        if registry.get(key) is task:
            registry.pop(key, None)
        task = None

    if task is None:
        if max_entries <= 0:
            return await loader()

        # Done callbacks normally run before another event-loop turn, but
        # sweep any completed entries here as well so stale terminal tasks do
        # not consume the bounded capacity.
        for existing_key, existing_task in list(registry.items()):
            if existing_task.done() and registry.get(existing_key) is existing_task:
                registry.pop(existing_key, None)

        if len(registry) >= max_entries:
            return await loader()

        task = asyncio.create_task(loader())
        registry[key] = task

        def _remove(done_task: asyncio.Task[T]) -> None:
            try:
                # A shielded loader can outlive its last waiter. Observe a
                # terminal exception in that detached case so the event loop
                # does not report it as unhandled; active waiters still get
                # the same exception when they await the task.
                if not done_task.cancelled():
                    done_task.exception()
            finally:
                if registry.get(key) is done_task:
                    registry.pop(key, None)

        task.add_done_callback(_remove)

    return await asyncio.shield(task)
