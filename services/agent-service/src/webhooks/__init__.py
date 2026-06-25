"""Outbound webhook dispatch — Phase C2.

Event types per spec §5.2:
  conversation.started, turn.completed, turn.refused,
  turn.judge_blocked, turn.feedback.

Signed with HMAC-SHA256; retried 3x with 10s/60s/300s back-off; bodies
larger than 32 KB are dropped to the DLQ table without an HTTP attempt.
"""
from src.webhooks.dispatcher import dispatch_event

__all__ = ["dispatch_event"]
