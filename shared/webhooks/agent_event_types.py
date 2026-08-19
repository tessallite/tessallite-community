"""Single source of truth for AGENT outbound webhook event types (Bug-8411).

This is the agent-service counterpart of ``shared/webhooks/event_types.py``.
The two catalogues are deliberately separate because they describe two
different products with two different subscriber tables:

* ``event_types.py`` — the platform-wide webhook endpoints
  (``WebhookEndpoint.event_filters``), emitted via
  ``shared.webhooks.dispatcher.emit_webhook``.
* this module — the per-project conversational-agent webhook
  (``ProjectAgentConfig.webhook_url`` / ``.webhook_event_filters``), emitted
  via ``agent-service``'s own ``src.webhooks.dispatcher.dispatch_event``.

Consumers:

- ``agent-service``'s ``dispatch_event`` skips an event the project has not
  subscribed to.
- ``agent-service``'s ``PUT``/``PATCH /agent/config`` validates
  ``webhook_event_filters`` against this catalogue.
- ``GET /projects/{id}/agent/webhook/event-types`` serves it, so the SPA
  never hard-codes a parallel list that could drift from what is emitted.

When you add a new ``dispatch_event(..., event_type="<name>", ...)`` call
site, add ``<name>`` here in the same change.
``tests/test_bug_8411_agent_webhook_event_filters.py::
TestAgentEventCatalogueMatchesEmitters`` enforces that the set here is
exactly the set of names actually dispatched across agent-service.
"""
from __future__ import annotations

from typing import Optional

# event name -> human-readable label. The label is advisory; the SPA
# localises it, but keeping one here makes the catalogue self-describing for
# an API consumer that is not the SPA.
AGENT_WEBHOOK_EVENT_LABELS: dict[str, str] = {
    "conversation.started": "Conversation Started",
    "turn.completed": "Turn Completed",
    "turn.refused": "Turn Refused",
    "turn.judge_blocked": "Turn Blocked by Judge",
    "turn.feedback": "Turn Feedback Submitted",
}

# The wildcard subscribes to every event, including ones added later.
WILDCARD = "*"

AGENT_WEBHOOK_EVENT_TYPES: frozenset[str] = frozenset(AGENT_WEBHOOK_EVENT_LABELS)

# Default subscription for a project that has never touched the filters:
# every event, matching the behaviour that shipped before filters existed.
DEFAULT_AGENT_EVENT_FILTERS: list[str] = [WILDCARD]


def is_valid_agent_filter(value: str) -> bool:
    """True if ``value`` is the wildcard or a known agent event name."""
    return value == WILDCARD or value in AGENT_WEBHOOK_EVENT_TYPES


def agent_event_catalogue() -> list[dict[str, str]]:
    """Ordered catalogue ``[{value, label}]`` for API/UI consumption."""
    return [
        {"value": name, "label": label}
        for name, label in AGENT_WEBHOOK_EVENT_LABELS.items()
    ]


class InvalidAgentEventFilters(ValueError):
    """A stored/incoming webhook subscription value is not usable."""


def validate_agent_event_filters(value: object) -> Optional[list[str]]:
    """Validate a webhook subscription and return it, or raise.

    Codex cross-family gate finding (2026-07-29): validation used to live
    only in agent-service's config API, so the OTHER write path — project
    import, which copies ``webhook_event_filters`` out of an untyped bundle
    straight into the ORM object — accepted anything. An imported JSON
    STRING like ``"turn.feedback"``, which reads as "only feedback please",
    is not a list, so ``agent_event_subscribed`` fell through to its
    deliver-everything branch and the receiver got every conversation event
    the operator had deselected. Exactly the Bug-7330 outcome by a different
    door.

    So the rule lives HERE, in the module both writers already import, and
    both call it. ``None`` is passed through (means "not set" / "every
    event"); everything else must be a non-empty list of known names.
    """
    if value is None:
        return None
    if not isinstance(value, list):
        raise InvalidAgentEventFilters(
            f"webhook_event_filters must be a list of event names, got "
            f"{type(value).__name__}"
        )
    if not value:
        raise InvalidAgentEventFilters(
            "webhook_event_filters must not be empty. Use "
            f"['{WILDCARD}'] to receive every agent event, or clear "
            "webhook_url to stop delivery entirely."
        )
    unknown = [f for f in value if not isinstance(f, str) or not is_valid_agent_filter(f)]
    if unknown:
        raise InvalidAgentEventFilters(
            f"Unknown agent webhook event_filters: {unknown}. Known events: "
            f"{sorted(AGENT_WEBHOOK_EVENT_TYPES)} (or '{WILDCARD}' for all)."
        )
    return list(value)


def agent_event_subscribed(event_type: str, filters: object) -> bool:
    """Should ``event_type`` be delivered given a project's stored filters?

    Fail-OPEN on a missing/malformed value, fail-CLOSED on an explicit
    non-empty subscription list. The asymmetry is deliberate and is the
    lesson of Bug-7330 on the platform-wide sibling:

    * ``None`` means the project predates the filters column, or the column
      was never written. It must keep receiving everything — silently
      dropping a webhook a customer already depends on because a new column
      defaulted badly is a regression, not a security improvement.
    * An empty list is NOT treated as "everything". On the platform-wide
      endpoint an empty list was silently coerced to match-all, which meant a
      subscriber who deliberately deselected every event kept getting them
      all. The API layer rejects an empty list outright; if one reaches here
      anyway (a legacy row, a direct DB edit) it is honoured as "nothing",
      which is what the operator actually asked for.
    * A non-list (a string, a dict — a corrupt row) is unreadable as a
      subscription, so it is treated the same as ``None``: deliver.
    """
    if filters is None:
        return True
    if not isinstance(filters, (list, tuple)):
        return True
    if WILDCARD in filters:
        return True
    return event_type in filters
