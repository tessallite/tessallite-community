"""Bug-8371 — turn.started must use the reserved AgentTurn identity."""
from __future__ import annotations

import types
import uuid

import pytest

from src.pipeline import run_turn
from src.sse.events import EventPublisher


@pytest.mark.asyncio
async def test_turn_started_emits_reserved_turn_id_without_minting_another():
    conversation_id = uuid.uuid4()
    reserved_turn_id = uuid.uuid4()
    cfg = types.SimpleNamespace(judge_mode="sync")
    conversation = types.SimpleNamespace(id=conversation_id, persona_id=None)
    publisher = EventPublisher()

    class _Scan:
        ok = False
        reason = "policy"
        matched_topic = None

    # Input refusal stops before any model work but still exercises the real
    # lifecycle emission at the top of run_turn.
    from unittest.mock import patch
    with patch("src.pipeline.scan_input_message", return_value=_Scan()):
        await run_turn(
            db=None,
            cfg=cfg,
            conversation=conversation,
            user_message="hello",
            jwt_token="jwt",
            publisher=publisher,
            turn_id=reserved_turn_id,
        )

    first = await publisher._queue.get()
    assert first.name == "turn.started"
    assert first.data["conversation_id"] == str(conversation_id)
    assert first.data["turn_id"] == str(reserved_turn_id)
    assert not any(item.name == "turn.started" and item.data.get("turn_id") != str(reserved_turn_id)
                   for item in list(publisher._queue._queue))
    # Drain/close is not needed for the identity assertion, but avoid leaving
    # the publisher's sentinel behind for shared event-loop cleanup.
    await publisher.close()
