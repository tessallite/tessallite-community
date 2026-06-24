"""SSE event publisher — Phase C1.

The pipeline emits events through an EventPublisher; the streaming
endpoint drains the queue and serialises each event as an SSE frame.
A None sentinel signals end-of-stream.

Event payloads are kept small and JSON-serialisable. Field names match
plan §5.1.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import UUID

logger = logging.getLogger(__name__)

_END_SENTINEL = object()
# F-023-26(a) — yielded by ``drain`` when no event arrives within the
# heartbeat interval, so the endpoint can emit an SSE keep-alive without
# reaching into the publisher's private queue.
_HEARTBEAT_SENTINEL = object()


def _json_default(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


@dataclass
class Event:
    name: str
    data: dict[str, Any] = field(default_factory=dict)


class EventPublisher:
    """Async queue wrapper. Pipeline calls .emit(...); endpoint calls
    .drain() to consume frames until close()."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._closed = False

    async def emit(self, name: str, **data: Any) -> None:
        if self._closed:
            return
        await self._queue.put(Event(name=name, data=data))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._queue.put(_END_SENTINEL)

    async def drain(self, heartbeat_interval: float | None = None):
        """Yield queued events until ``close()``.

        F-023-26(a) — the streaming endpoint previously read
        ``publisher._queue`` directly (breaking encapsulation while
        ``drain`` sat unused). With ``heartbeat_interval`` set, ``drain``
        owns the timeout and yields ``_HEARTBEAT_SENTINEL`` when idle, so
        the endpoint never touches the private queue."""
        while True:
            if heartbeat_interval is None:
                item = await self._queue.get()
            else:
                try:
                    item = await asyncio.wait_for(
                        self._queue.get(), timeout=heartbeat_interval
                    )
                except asyncio.TimeoutError:
                    yield _HEARTBEAT_SENTINEL
                    continue
            if item is _END_SENTINEL:
                return
            yield item


def format_sse(event: Event) -> str:
    payload = json.dumps(event.data, default=_json_default, separators=(",", ":"))
    return f"event: {event.name}\ndata: {payload}\n\n"


def format_heartbeat() -> str:
    return ": heartbeat\n\n"


def format_raw(name: str, data: Optional[dict[str, Any]] = None) -> str:
    return format_sse(Event(name=name, data=data or {}))
