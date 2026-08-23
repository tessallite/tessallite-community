"""Bug-6575 — defense-in-depth prompt hygiene for embed-token model_ids.

The AUTHORITATIVE model-scope enforcement for a restricted embed token lives at
the query-router (``enforce_model_scope``): a restricted user cannot EXECUTE a
query against a disallowed model. These tests guard the ADDITIONAL, earlier
filter added here: the NAMES / metadata of models outside the embed token's
``model_ids`` allow-list must never be assembled into the LLM prompt, nor
advertised by the selectable-models picker, in the first place.

Test escape: the assembler intersected the project allow-list with persona and
pin scopes but never with the embed token's ``model_ids``, so a restricted
embed user's disallowed model names were still put in front of the planner LLM.
Guard: ``_apply_embed_model_scope`` runs before ``_load_model_profiles`` in
``assemble_prompt`` / ``load_selectable_models``.
Tier: T1.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.prompt import assembler as A
from src.prompt.assembler import (
    SelectableModelInfo,
    _apply_embed_model_scope,
    _ModelProfile,
    assemble_prompt,
    load_selectable_models,
)


# ---------------------------------------------------------------------------
# Pure helper — the intersection contract
# ---------------------------------------------------------------------------


def test_none_embed_scope_is_a_noop():
    a, b = uuid.uuid4(), uuid.uuid4()
    assert _apply_embed_model_scope([a, b], None) == [a, b]


def test_embed_scope_narrows_to_permitted_ids():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    # Embed token permits only `a` and `c` (lowercased string form).
    permitted = [str(a).lower(), str(c).lower()]
    assert _apply_embed_model_scope([a, b, c], permitted) == [a, c]


def test_embed_scope_is_order_preserving_on_allow_ids():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    permitted = [str(c).lower(), str(a).lower()]  # token order differs
    # Result follows allow_ids order, not the token order.
    assert _apply_embed_model_scope([a, b, c], permitted) == [a, c]


def test_embed_scope_cannot_widen_beyond_allow_list():
    a, b, outside = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    # Token names a model that is NOT on the project allow-list; it cannot be
    # added — intersection only narrows.
    permitted = [str(a).lower(), str(outside).lower()]
    assert _apply_embed_model_scope([a, b], permitted) == [a]


def test_empty_embed_scope_removes_everything():
    a, b = uuid.uuid4(), uuid.uuid4()
    assert _apply_embed_model_scope([a, b], []) == []


def test_embed_scope_case_insensitive_match():
    a = uuid.uuid4()
    # Middleware lowercases the claim; assert we match regardless of allow-id
    # string casing by comparing on the lowercased form.
    assert _apply_embed_model_scope([a], [str(a).upper().lower()]) == [a]


# ---------------------------------------------------------------------------
# assemble_prompt — restricted model names never reach the LLM prompt
# ---------------------------------------------------------------------------


def _profile(model_id: uuid.UUID, display_name: str) -> _ModelProfile:
    return _ModelProfile(
        id=model_id,
        slug=display_name.lower(),
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
    )


ALLOWED_ID = uuid.uuid4()
RESTRICTED_ID = uuid.uuid4()
ALLOWED_NAME = "Sales Model Alpha"
RESTRICTED_NAME = "Finance Model Bravo"


def _make_db_returning_allow_ids(allow_ids: list[uuid.UUID]) -> AsyncMock:
    """A mock DB whose only meaningful query is the ProjectAgentModel.model_id
    allow-list select; everything else returns empty."""
    allow_result = MagicMock()
    allow_result.scalars.return_value.all.return_value = list(allow_ids)

    empty_result = MagicMock()
    empty_result.scalars.return_value.all.return_value = []
    empty_result.scalar_one_or_none.return_value = None

    db = AsyncMock()
    # First execute() is the allow-list select; subsequent ones (recipes, etc.)
    # get the empty result.
    db.execute = AsyncMock(side_effect=[allow_result] + [empty_result] * 20)
    return db


def _cfg() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
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


async def _profiles_for_ids(db, project_id, allow_list_ids):
    """Stand-in for _load_model_profiles: build a profile per id it is asked
    for, so the test proves that only the intersected ids ever reach profile
    loading (and therefore the prompt text)."""
    name_by_id = {ALLOWED_ID: ALLOWED_NAME, RESTRICTED_ID: RESTRICTED_NAME}
    return [
        _profile(mid, name_by_id.get(mid, f"Model {mid}"))
        for mid in allow_list_ids
    ]


@pytest.mark.asyncio
async def test_restricted_model_name_absent_from_prompt_allowed_present():
    """An embed token permitting only ALLOWED_ID must yield a system prompt
    that names the allowed model and NOT the restricted model, even though both
    are on the project allow-list."""
    db = _make_db_returning_allow_ids([ALLOWED_ID, RESTRICTED_ID])

    with patch.object(A, "_load_model_profiles", side_effect=_profiles_for_ids), \
         patch.object(A, "list_glossary_cards", AsyncMock(return_value=[])), \
         patch.object(A, "retrieve_alias_maps", AsyncMock(return_value=[])), \
         patch.object(A, "_conversation_history", AsyncMock(return_value=[])):
        bundle = await assemble_prompt(
            db,
            _cfg(),
            conversation_id=uuid.uuid4(),
            user_message="what is revenue",
            embed_model_ids=[str(ALLOWED_ID).lower()],
        )

    assert ALLOWED_NAME in bundle.system
    assert RESTRICTED_NAME not in bundle.system
    # The allow-list carried into pipeline enforcement is also narrowed.
    assert bundle.allow_list_model_ids == [ALLOWED_ID]


@pytest.mark.asyncio
async def test_no_embed_scope_shows_all_allow_listed_models():
    """A non-embed caller (embed_model_ids=None) sees every allow-listed
    model — the new filter must not narrow the default path."""
    db = _make_db_returning_allow_ids([ALLOWED_ID, RESTRICTED_ID])

    with patch.object(A, "_load_model_profiles", side_effect=_profiles_for_ids), \
         patch.object(A, "list_glossary_cards", AsyncMock(return_value=[])), \
         patch.object(A, "retrieve_alias_maps", AsyncMock(return_value=[])), \
         patch.object(A, "_conversation_history", AsyncMock(return_value=[])):
        bundle = await assemble_prompt(
            db,
            _cfg(),
            conversation_id=uuid.uuid4(),
            user_message="what is revenue",
            embed_model_ids=None,
        )

    assert ALLOWED_NAME in bundle.system
    assert RESTRICTED_NAME in bundle.system
    assert set(bundle.allow_list_model_ids) == {ALLOWED_ID, RESTRICTED_ID}


# ---------------------------------------------------------------------------
# load_selectable_models — the in-chat picker respects the embed scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_selectable_models_hide_restricted_names():
    db = AsyncMock()

    with patch.object(A, "_load_model_profiles", side_effect=_profiles_for_ids):
        allowed = await load_selectable_models(
            db,
            project_id=uuid.uuid4(),
            allow_list_ids=[ALLOWED_ID, RESTRICTED_ID],
            embed_model_ids=[str(ALLOWED_ID).lower()],
        )

    assert [m.display_name for m in allowed] == [ALLOWED_NAME]
    assert all(isinstance(m, SelectableModelInfo) for m in allowed)
    assert RESTRICTED_ID not in [m.id for m in allowed]


@pytest.mark.asyncio
async def test_selectable_models_no_scope_returns_all():
    db = AsyncMock()

    with patch.object(A, "_load_model_profiles", side_effect=_profiles_for_ids):
        models = await load_selectable_models(
            db,
            project_id=uuid.uuid4(),
            allow_list_ids=[ALLOWED_ID, RESTRICTED_ID],
            embed_model_ids=None,
        )

    assert {m.id for m in models} == {ALLOWED_ID, RESTRICTED_ID}
