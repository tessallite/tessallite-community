"""Single source of truth for outbound webhook event types.

Every event name a webhook endpoint can subscribe to is declared here,
exactly once. The list is generated from the events the backend actually
emits via ``shared.webhooks.dispatcher.emit_webhook`` — there is no
second hand-maintained list anywhere.

Consumers:

- ``shared/webhooks/dispatcher`` validates emitted event names against
  this set so a typo at an emit site fails loud in tests.
- ``model-service`` serves this catalogue from
  ``GET /admin/webhooks/event-types`` and validates ``event_filters`` on
  webhook create/update against it.
- the frontend webhook page fetches the catalogue from that endpoint
  rather than hard-coding a parallel list, so the picker can never drift
  from what the backend emits.

When you add a new ``emit_webhook(tenant, "<name>", ...)`` call site, add
``<name>`` here (with a human label) in the same change. The test
``test_webhook_event_catalogue_matches_emitters`` enforces that the set
here is exactly the set of names emitted across the codebase.
"""
from __future__ import annotations

# event name -> human-readable label (label is advisory; the frontend may
# localise, but having one here keeps the catalogue self-describing).
WEBHOOK_EVENT_LABELS: dict[str, str] = {
    "model.published": "Model Published",
    "model.undeployed": "Model Undeployed",
    "model.reverted": "Model Reverted",
    "model.deleted": "Model Deleted",
    "user.created": "User Created",
    "user.deleted": "User Deleted",
    "settings.changed": "Settings Changed",
    "schema_drift.detected": "Schema Drift Detected",
    "refresh.sla_breach": "Refresh SLA Breach",
    "refresh.sla_recovered": "Refresh SLA Recovered",
    # F-012-22: scheduled and manual aggregate refreshes emit a completion
    # event so orchestrators can observe outcomes without polling run history.
    "refresh.completed": "Aggregate Refresh Completed",
    "refresh.failed": "Aggregate Refresh Failed",
}

# The wildcard subscribes to every event. Stored filters may contain it.
WILDCARD = "*"

WEBHOOK_EVENT_TYPES: frozenset[str] = frozenset(WEBHOOK_EVENT_LABELS)


def is_valid_filter(value: str) -> bool:
    """True if ``value`` is the wildcard or a known event name."""
    return value == WILDCARD or value in WEBHOOK_EVENT_TYPES


def event_catalogue() -> list[dict[str, str]]:
    """Ordered catalogue [{value, label}] for API/UI consumption."""
    return [
        {"value": name, "label": label}
        for name, label in WEBHOOK_EVENT_LABELS.items()
    ]
