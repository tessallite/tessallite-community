"""Bug-7251: heuristic fallback entries must be visibly flagged in responses.

The ``is_heuristic_fallback`` field on ``GlossaryEntryResponse`` surfaces the
fallback status so consumers can tell at a glance which entries were generated
without LLM input (source="heuristic", confidence="low").
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone

import pytest

from src.api.glossary import _entry_to_response

pytestmark = pytest.mark.unit


def _make_entry(source: str = "heuristic", confidence: str = "low"):
    now = datetime.now(timezone.utc)
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=uuid.uuid4(),
        term="Total Revenue",
        definition="Sum of revenue.",
        context_notes=None,
        source=source,
        status="pending_review",
        version=1,
        superseded_by=None,
        created_by=None,
        proposed_is_hidden=None,
        visibility="review",
        confidence=confidence,
        sample_values=None,
        created_at=now,
        updated_at=now,
        synonyms=[],
        attachments=[],
    )


def test_heuristic_entry_flagged():
    """A heuristic fallback entry must have is_heuristic_fallback=True."""
    entry = _make_entry(source="heuristic", confidence="low")
    resp = _entry_to_response(entry)
    assert resp.is_heuristic_fallback is True
    assert resp.confidence == "low"
    assert resp.source == "heuristic"


def test_llm_entry_not_flagged():
    """An LLM-generated entry must have is_heuristic_fallback=False."""
    entry = _make_entry(source="llm", confidence="high")
    resp = _entry_to_response(entry)
    assert resp.is_heuristic_fallback is False


def test_user_entry_not_flagged():
    """A user-created entry must have is_heuristic_fallback=False."""
    entry = _make_entry(source="user", confidence="high")
    resp = _entry_to_response(entry)
    assert resp.is_heuristic_fallback is False


def test_llm_approved_entry_not_flagged():
    """An LLM-approved entry must have is_heuristic_fallback=False."""
    entry = _make_entry(source="llm_approved", confidence="high")
    resp = _entry_to_response(entry)
    assert resp.is_heuristic_fallback is False
