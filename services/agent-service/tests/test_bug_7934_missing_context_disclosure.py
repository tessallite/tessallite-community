"""Bug-7934 — Missing ProjectAgentModelContext row causes silent prompt
degradation: the planner operates on a degraded grounding surface without
knowing it.

Business outcome under test: when a model has no ProjectAgentModelContext row
the LLM prompt must disclose that operational context (aggregates, calendars,
dimension aliases) is unavailable, so the planner does not make claims about
aggregate availability or calendar tables it cannot see.

Test escape: derive_model_context() silently returned None and the assembler
silently fell back to empty context attributes without any log or prompt note.
Guard: WARNING log in derive_model_context, WARNING log in _load_model_profiles,
and disclosure note in the AVAILABLE MODELS prompt section.
Tier: T1.
"""
from __future__ import annotations

import logging
import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.prompt import assembler as A
from src.prompt.assembler import (
    _ModelProfile,
    _format_model_layer,
    assemble_prompt,
)


# ---------------------------------------------------------------------------
# _format_model_layer — disclosure note appears when context_available=False
# ---------------------------------------------------------------------------


def _profile(
    model_id: uuid.UUID,
    display_name: str,
    context_available: bool = True,
) -> _ModelProfile:
    return _ModelProfile(
        id=model_id,
        slug=display_name.lower().replace(" ", "-"),
        display_name=display_name,
        overview=None,
        analytical_capabilities=None,
        abbreviation_conflict_rules=None,
        example_questions=[],
        measure_names=["revenue"],
        dimension_names=["country"],
        filterable_where_names=["country"],
        sortable_names=["country", "revenue"],
        aggregates_summary=[],
        calendar_aliases=[],
        dimension_aliases=[],
        tagged_fields={},
        dimension_value_hints={},
        context_available=context_available,
    )


class TestFormatModelLayerDisclosure:
    def test_missing_context_adds_disclosure_note(self):
        mid = uuid.uuid4()
        profiles = [_profile(mid, "Sales Model", context_available=False)]
        text = _format_model_layer(profiles)
        assert "Operational context is unavailable" in text
        assert "Pre-aggregated rollups" in text
        assert "do not make claims about whether" in text

    def test_present_context_omits_disclosure_note(self):
        mid = uuid.uuid4()
        profiles = [_profile(mid, "Sales Model", context_available=True)]
        text = _format_model_layer(profiles)
        assert "Operational context is unavailable" not in text

    def test_mixed_models_only_flags_missing(self):
        ok_id = uuid.uuid4()
        missing_id = uuid.uuid4()
        profiles = [
            _profile(ok_id, "OK Model", context_available=True),
            _profile(missing_id, "Missing Model", context_available=False),
        ]
        text = _format_model_layer(profiles)
        # Disclosure must appear exactly once (for the missing model) and
        # after the missing model header.
        sections = text.split("### Model:")
        # sections[0] is preamble; sections[1] is OK Model; sections[2] is Missing Model
        assert "Operational context is unavailable" not in sections[1]
        assert "Operational context is unavailable" in sections[2]


# ---------------------------------------------------------------------------
# _load_model_profiles — WARNING logged when context row missing
# ---------------------------------------------------------------------------


MODEL_WITH_CTX = uuid.uuid4()
MODEL_WITHOUT_CTX = uuid.uuid4()
PROJECT_ID = uuid.uuid4()


def _make_model(mid: uuid.UUID, name: str) -> MagicMock:
    m = MagicMock()
    m.id = mid
    m.slug = name.lower()
    m.display_name = name
    m.glossary_max_distinct = 20
    return m


def _make_ctx(
    project_id: uuid.UUID,
    model_id: uuid.UUID,
    derived_at: object = "__set__",
) -> MagicMock:
    c = MagicMock()
    c.project_id = project_id
    c.model_id = model_id
    c.model_overview = "overview"
    c.analytical_capabilities = None
    c.abbreviation_conflict_rules = None
    c.example_questions = []
    c.aggregates_summary = [{"name": "agg1", "status": "active", "grain": ["day"]}]
    c.calendar_aliases = []
    c.dimension_aliases = []
    # Bug-7936 — a row is only "derived" once derived_at is stamped. Default to
    # a concrete timestamp (the derived case); pass derived_at=None for the
    # never-derived case.
    c.derived_at = (
        datetime(2026, 1, 1, tzinfo=timezone.utc)
        if derived_at == "__set__"
        else derived_at
    )
    return c


@pytest.mark.asyncio
async def test_load_model_profiles_logs_warning_on_missing_context(caplog):
    """When _load_model_profiles processes a model without a context row, a
    WARNING must be logged naming the model_id and project_id."""
    models_result = MagicMock()
    models_result.scalars.return_value.all.return_value = [
        _make_model(MODEL_WITH_CTX, "ModelA"),
        _make_model(MODEL_WITHOUT_CTX, "ModelB"),
    ]

    ctx_result = MagicMock()
    ctx_result.scalars.return_value.all.return_value = [
        _make_ctx(PROJECT_ID, MODEL_WITH_CTX),
        # No context row for MODEL_WITHOUT_CTX
    ]

    empty_result = MagicMock()
    empty_result.scalars.return_value.all.return_value = []
    empty_result.all.return_value = []

    db = AsyncMock()
    # Call sequence: models select, ctx select, then many attribute/tag/stat/kpi/ns queries
    db.execute = AsyncMock(
        side_effect=[models_result, ctx_result] + [empty_result] * 40
    )
    db.get = AsyncMock(return_value=None)

    with patch.object(A, "list_model_attributes", AsyncMock(return_value=(["revenue"], ["country"]))), \
         caplog.at_level(logging.WARNING, logger="src.prompt.assembler"):
        profiles = await A._load_model_profiles(
            db, PROJECT_ID, [MODEL_WITH_CTX, MODEL_WITHOUT_CTX]
        )

    # Both models should produce profiles.
    assert len(profiles) == 2

    # The model without a context row should have context_available=False.
    by_id = {p.id: p for p in profiles}
    assert by_id[MODEL_WITH_CTX].context_available is True
    assert by_id[MODEL_WITHOUT_CTX].context_available is False

    # A WARNING log must have been emitted for the missing context, under
    # the assembler module logger (pinned so the signal cannot silently
    # migrate to another logger).
    assert any(
        rec.name == "src.prompt.assembler"
        and rec.levelno == logging.WARNING
        and str(MODEL_WITHOUT_CTX) in rec.message
        and "No ProjectAgentModelContext" in rec.message
        for rec in caplog.records
    )

    # No warning for the model that HAS context.
    assert not any(
        str(MODEL_WITH_CTX) in rec.message
        and "No ProjectAgentModelContext" in rec.message
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Bug-7936 — a context row that EXISTS but was never derived (derived_at is
# NULL) degrades the prompt identically to a missing row and must be treated
# the same: context_available=False, disclosure note, and a distinct WARNING.
# ---------------------------------------------------------------------------


MODEL_NEVER_DERIVED = uuid.uuid4()


@pytest.mark.asyncio
async def test_load_model_profiles_flags_never_derived_context_row(caplog):
    """A ProjectAgentModelContext row present but never derived (derived_at
    NULL) must set context_available=False and log a WARNING naming the model,
    exactly as a missing row does — a derived row (derived_at set) must not."""
    models_result = MagicMock()
    models_result.scalars.return_value.all.return_value = [
        _make_model(MODEL_WITH_CTX, "ModelA"),
        _make_model(MODEL_NEVER_DERIVED, "ModelB"),
    ]

    ctx_result = MagicMock()
    ctx_result.scalars.return_value.all.return_value = [
        _make_ctx(PROJECT_ID, MODEL_WITH_CTX),  # derived_at set (derived)
        _make_ctx(PROJECT_ID, MODEL_NEVER_DERIVED, derived_at=None),  # never derived
    ]

    empty_result = MagicMock()
    empty_result.scalars.return_value.all.return_value = []
    empty_result.all.return_value = []

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[models_result, ctx_result] + [empty_result] * 40
    )
    db.get = AsyncMock(return_value=None)

    with patch.object(A, "list_model_attributes", AsyncMock(return_value=(["revenue"], ["country"]))), \
         caplog.at_level(logging.WARNING, logger="src.prompt.assembler"):
        profiles = await A._load_model_profiles(
            db, PROJECT_ID, [MODEL_WITH_CTX, MODEL_NEVER_DERIVED]
        )

    by_id = {p.id: p for p in profiles}
    # The derived row is genuine context; the never-derived row is degraded.
    assert by_id[MODEL_WITH_CTX].context_available is True
    assert by_id[MODEL_NEVER_DERIVED].context_available is False

    # A WARNING naming the never-derived model must fire, worded distinctly
    # from the missing-row case so operators can tell them apart.
    assert any(
        rec.name == "src.prompt.assembler"
        and rec.levelno == logging.WARNING
        and str(MODEL_NEVER_DERIVED) in rec.message
        and "never been derived" in rec.message
        for rec in caplog.records
    )
    # The derived model triggers no degraded-context warning.
    assert not any(
        str(MODEL_WITH_CTX) in rec.message
        and ("never been derived" in rec.message or "No ProjectAgentModelContext" in rec.message)
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# derive_model_context — WARNING logged when no record exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_derive_model_context_logs_warning_on_missing_record(caplog):
    """derive_model_context must log a WARNING when no
    ProjectAgentModelContext row exists for the (project, model) pair."""
    from src.derived.context import derive_model_context

    db = AsyncMock()
    db.get = AsyncMock(return_value=None)

    project_id = uuid.uuid4()
    model_id = uuid.uuid4()

    with caplog.at_level(logging.WARNING, logger="src.derived.context"):
        result = await derive_model_context(db, project_id, model_id)

    assert result is None
    # Pinned to the module logger so the signal cannot silently migrate.
    assert any(
        rec.name == "src.derived.context"
        and rec.levelno == logging.WARNING
        and str(model_id) in rec.message
        and str(project_id) in rec.message
        and "No ProjectAgentModelContext" in rec.message
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# assemble_prompt integration — disclosure reaches the system prompt
# ---------------------------------------------------------------------------


def _cfg() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        project_id=PROJECT_ID,
        primary_model_id=None,
        pinned_model_id=None,
        session_history_depth=20,
        agent_role="data analyst",
        safety_policy="",
        default_locale="en-GB",
        project_brief="",
        disclosure_text="",
        brand_guidelines="",
        content_rules="",
        chart_type_selector="auto",
        chart_renderer="echarts",
    )


async def _profiles_missing_ctx(db, project_id, allow_list_ids):
    """Stand-in for _load_model_profiles: returns one profile with
    context_available=False."""
    return [
        _profile(mid, f"Model-{mid}", context_available=False)
        for mid in allow_list_ids
    ]


async def _profiles_with_ctx(db, project_id, allow_list_ids):
    """Stand-in for _load_model_profiles: returns profiles with
    context_available=True."""
    return [
        _profile(mid, f"Model-{mid}", context_available=True)
        for mid in allow_list_ids
    ]


def _make_allow_db(ids: list[uuid.UUID]) -> AsyncMock:
    allow_result = MagicMock()
    allow_result.scalars.return_value.all.return_value = list(ids)
    empty_result = MagicMock()
    empty_result.scalars.return_value.all.return_value = []
    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[allow_result] + [empty_result] * 20
    )
    return db


@pytest.mark.asyncio
async def test_assemble_prompt_includes_disclosure_when_context_missing():
    mid = uuid.uuid4()
    db = _make_allow_db([mid])

    with patch.object(A, "_load_model_profiles", side_effect=_profiles_missing_ctx), \
         patch.object(A, "list_glossary_cards", AsyncMock(return_value=[])), \
         patch.object(A, "retrieve_alias_maps", AsyncMock(return_value=[])), \
         patch.object(A, "_conversation_history", AsyncMock(return_value=[])):
        bundle = await assemble_prompt(
            db,
            _cfg(),
            conversation_id=uuid.uuid4(),
            user_message="show me revenue",
        )

    assert "Operational context is unavailable" in bundle.system


@pytest.mark.asyncio
async def test_assemble_prompt_omits_disclosure_when_context_present():
    mid = uuid.uuid4()
    db = _make_allow_db([mid])

    with patch.object(A, "_load_model_profiles", side_effect=_profiles_with_ctx), \
         patch.object(A, "list_glossary_cards", AsyncMock(return_value=[])), \
         patch.object(A, "retrieve_alias_maps", AsyncMock(return_value=[])), \
         patch.object(A, "_conversation_history", AsyncMock(return_value=[])):
        bundle = await assemble_prompt(
            db,
            _cfg(),
            conversation_id=uuid.uuid4(),
            user_message="show me revenue",
        )

    assert "Operational context is unavailable" not in bundle.system
