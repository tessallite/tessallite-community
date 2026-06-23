"""Server-Sent Events surface for the conversational agent — Phase C1.

Event names match plan §5.1:
  turn.started, plan.tool, recipe.step, query.rows, narration.delta,
  turn.completed, turn.judged, turn.blocked, turn.error.
"""
from src.sse.events import EventPublisher, format_sse

__all__ = ["EventPublisher", "format_sse"]
