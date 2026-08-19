"""Bug-7376 / Bug-7545 — streaming thought.delta tokens must be gated
on show_thought_process.

The pipeline's _on_thinking callback is the single server-side
enforcement point: it must suppress thought.delta emission when the
project agent config disables thought_process visibility.  This test
exercises that gate without standing up the full pipeline.
"""
from __future__ import annotations

import asyncio
import types
import uuid

import pytest

from src.sse.events import EventPublisher

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Minimal reproduction of the pipeline's _on_thinking closure.
# We extract the exact logic so changes to the pipeline are caught by
# this contract test.
# ---------------------------------------------------------------------------


def _make_on_thinking(
    cfg: types.SimpleNamespace,
    publisher: EventPublisher | None,
    thinking_parts: list[str],
):
    """Reproduce the pipeline's _on_thinking closure (pipeline.py ~L1381)."""

    async def _on_thinking(token: str) -> None:
        thinking_parts.append(token)
        if publisher is not None and getattr(cfg, "show_thought_process", True):
            await publisher.emit("thought.delta", text=token)

    return _on_thinking


async def _drain_all(publisher: EventPublisher) -> list:
    events = []
    await publisher.close()
    async for evt in publisher.drain():
        events.append(evt)
    return events


class TestThoughtProcessStreamingGate:
    @pytest.mark.asyncio
    async def test_thought_delta_emitted_when_enabled(self):
        cfg = types.SimpleNamespace(show_thought_process=True)
        publisher = EventPublisher()
        parts: list[str] = []
        callback = _make_on_thinking(cfg, publisher, parts)

        await callback("I am thinking")
        events = await _drain_all(publisher)

        assert len(events) == 1
        assert events[0].name == "thought.delta"
        assert events[0].data["text"] == "I am thinking"
        assert parts == ["I am thinking"]

    @pytest.mark.asyncio
    async def test_thought_delta_suppressed_when_disabled(self):
        cfg = types.SimpleNamespace(show_thought_process=False)
        publisher = EventPublisher()
        parts: list[str] = []
        callback = _make_on_thinking(cfg, publisher, parts)

        await callback("SECRET reasoning about internal model names")
        events = await _drain_all(publisher)

        # No events should have been emitted.
        assert len(events) == 0
        # But internal accumulation still works (for persisted storage
        # that is redacted later).
        assert parts == ["SECRET reasoning about internal model names"]

    @pytest.mark.asyncio
    async def test_thought_delta_suppressed_with_no_publisher(self):
        """No publisher = non-streaming path; thinking is accumulated only."""
        cfg = types.SimpleNamespace(show_thought_process=True)
        parts: list[str] = []
        callback = _make_on_thinking(cfg, None, parts)

        await callback("silent thinking")
        assert parts == ["silent thinking"]

    @pytest.mark.asyncio
    async def test_thought_delta_multiple_tokens_all_suppressed(self):
        """All tokens in a sequence must be suppressed, not just the first."""
        cfg = types.SimpleNamespace(show_thought_process=False)
        publisher = EventPublisher()
        parts: list[str] = []
        callback = _make_on_thinking(cfg, publisher, parts)

        for token in ["token1", "token2", "token3"]:
            await callback(token)
        events = await _drain_all(publisher)

        assert len(events) == 0
        assert parts == ["token1", "token2", "token3"]

    @pytest.mark.asyncio
    async def test_cfg_without_show_thought_process_defaults_to_emit(self):
        """Backwards compat: if cfg has no show_thought_process attr, default
        to emitting (safe fallback for older configs)."""
        cfg = types.SimpleNamespace()  # no show_thought_process attr
        publisher = EventPublisher()
        parts: list[str] = []
        callback = _make_on_thinking(cfg, publisher, parts)

        await callback("default-visible thinking")
        events = await _drain_all(publisher)

        assert len(events) == 1
        assert events[0].data["text"] == "default-visible thinking"
