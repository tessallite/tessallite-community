"""Bug-9028 — project import preserves derived-vs-never-derived context state."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from shared.model_snapshot.project_rehydrator import (
    _imported_agent_context_derived_at,
)
from shared.model_snapshot.project_serialiser import (
    _serialise_agent_model_context,
)


class _Context:
    __table__ = SimpleNamespace(columns=[
        SimpleNamespace(name="project_id"),
        SimpleNamespace(name="model_id"),
        SimpleNamespace(name="model_overview"),
        SimpleNamespace(name="analytical_capabilities"),
        SimpleNamespace(name="abbreviation_conflict_rules"),
        SimpleNamespace(name="example_questions"),
        SimpleNamespace(name="aggregates_summary"),
        SimpleNamespace(name="calendar_aliases"),
        SimpleNamespace(name="dimension_aliases"),
        SimpleNamespace(name="derived_at"),
        SimpleNamespace(name="published_at"),
        SimpleNamespace(name="updated_at"),
    ])

    def __init__(self, *, derived_at):
        self.project_id = uuid4()
        self.model_id = uuid4()
        self.model_overview = None
        self.analytical_capabilities = None
        self.abbreviation_conflict_rules = None
        self.example_questions = []
        self.aggregates_summary = []
        self.calendar_aliases = []
        self.dimension_aliases = []
        self.derived_at = derived_at
        self.published_at = None
        self.updated_at = None


def test_empty_derived_context_round_trips_as_derived():
    payload = _serialise_agent_model_context(
        _Context(derived_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    )

    assert payload["aggregates_summary"] == []
    assert payload["context_derived"] is True
    restored = _imported_agent_context_derived_at(payload)
    assert restored is not None


def test_never_derived_context_stays_degraded_after_import():
    payload = _serialise_agent_model_context(_Context(derived_at=None))

    assert payload["context_derived"] is False
    assert _imported_agent_context_derived_at(payload) is None


def test_legacy_nonempty_context_without_marker_is_derived():
    payload = {
        "aggregates_summary": [{"name": "agg_orders", "status": "active"}],
        "calendar_aliases": [],
        "dimension_aliases": [],
    }

    assert _imported_agent_context_derived_at(payload) is not None
