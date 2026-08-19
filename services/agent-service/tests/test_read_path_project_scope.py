"""Stored ids are dereferenced with the project predicate IN the query.

``test_body_fk_project_scope.py`` covers the WRITE half: a UUID arriving in a
REQUEST BODY is proven to belong to the path project before it is persisted.
This file covers the other half — what happens to ids that are ALREADY STORED,
which the write guards say nothing about. Rows bound to another project's
resource before those guards existed keep resolving, and each one was
dereferenced with a bare ``db.get``:

* **F-03** — ``ProjectAgentConfig.{answer,judge}_llm_config_id`` resolved through
  ``shared/llm/config_resolution.py``. ``LLMProviderConfig`` carries a
  Fernet-encrypted provider API key and a base_url, so a legacy foreign binding
  sent this project's prompts through, and billed them to, ANOTHER PROJECT'S
  provider account — a successful call, not a refusal. Same residual on the
  model-level override written by ``model-service/src/api/scheduler_config.py``,
  which resolves through the same module.
* **Bug-8933** — ``AgentConversation.persona_id`` resolved by
  ``_resolve_persona_name``, whose result is stamped onto every ``TurnResponse``.
  Persona names are business-descriptive, so a legacy foreign id disclosed
  another project's persona name on a 200.
* **Bug-8943** — ``ProjectAgentConfig.judge_rubric_id`` resolved in
  ``judge.run_judge``, whose ``sections`` are rendered into the judge prompt —
  another project's rubric text deciding how this project's answers are judged.

Disposition, uniformly: a foreign id behaves exactly as an id that resolves to
nothing already behaved. No new behaviour is invented, and the outcome is never
"use it anyway". For the LLM config that means the existing "no LLM
configuration available" refusal, because silently substituting a different
provider would hide the misconfiguration while changing where the data goes.

The sessions here EVALUATE the emitted predicate rather than returning a canned
row (``_PredicateSession``), so removing the project predicate makes these tests
red instead of leaving them passing against a lookup that proves nothing.

Guard: this file. Tier: T3 (cross-project isolation, credential use).
Test escape: every existing test built its fixture through the guarded write
path, so no stored id ever pointed outside the project, and the read was never
exercised against one that did.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.db.models import (
    AgentJudgeRubric,
    LLMProviderConfig,
    ProjectAgentConfig,
    ProjectPersona,
)
from shared.llm.config_resolution import resolve_agent_llm_config

from .conftest import TEST_PROJECT_ID
from .test_body_fk_project_scope import OTHER_PROJECT_ID, _PredicateSession, _llm

pytestmark = pytest.mark.asyncio


def _agent_config(**kw) -> ProjectAgentConfig:
    """A persisted-looking config row carrying whatever bindings the test needs.

    Constructed directly rather than through the API, because the API now
    REFUSES these bindings — which is the whole point. This is the state a
    tenant that used the product before the write guards landed is already in.
    """
    base = dict(
        id=uuid.uuid4(),
        project_id=TEST_PROJECT_ID,
        answer_llm_config_id=None,
        judge_llm_config_id=None,
        judge_rubric_id=None,
    )
    base.update(kw)
    return ProjectAgentConfig(**base)


# ---------------------------------------------------------------------------
# F-03 — a stored foreign LLM config is never loaded and never used
# ---------------------------------------------------------------------------


async def test_stored_foreign_answer_config_is_refused_not_used():
    """F-03. The binding is already in the database; resolving it must not
    return another project's provider row."""
    foreign_id = uuid.uuid4()
    db = _PredicateSession({
        ProjectAgentConfig: [_agent_config(answer_llm_config_id=foreign_id)],
        LLMProviderConfig: [_llm(OTHER_PROJECT_ID, foreign_id)],
    })

    with pytest.raises(ValueError) as exc:
        await resolve_agent_llm_config(TEST_PROJECT_ID, "answer", db)

    assert "No LLM configuration available" in str(exc.value), (
        "the agent must stop with the same message an UNSET config produces, "
        "not silently run on the foreign provider"
    )
    sql = str(db.executed[-1].compile())
    assert "llm_provider_configs.project_id =" in sql, (
        "the ownership predicate must be in the SELECT, not a comparison "
        f"after a db.get: {sql}"
    )


async def test_stored_foreign_judge_config_does_not_fall_through_to_answer():
    """A foreign judge binding must not be quietly replaced by the answer LLM.

    ``judge_llm_config_id or answer_llm_config_id`` is a null-coalesce, not a
    fallback-on-unresolvable: an id that resolves to nothing already raised,
    and a foreign id behaves identically. Silently judging with a different
    provider than the admin selected would hide the misconfiguration."""
    foreign_judge = uuid.uuid4()
    own_answer = uuid.uuid4()
    db = _PredicateSession({
        ProjectAgentConfig: [
            _agent_config(
                judge_llm_config_id=foreign_judge,
                answer_llm_config_id=own_answer,
            )
        ],
        LLMProviderConfig: [
            _llm(OTHER_PROJECT_ID, foreign_judge),
            _llm(TEST_PROJECT_ID, own_answer),
        ],
    })

    with pytest.raises(ValueError):
        await resolve_agent_llm_config(TEST_PROJECT_ID, "judge", db)


@patch("shared.llm.adapter.decrypt_api_key", return_value="sk-test")
async def test_a_config_this_project_owns_still_resolves(_dec):
    """The positive direction. A guard that refuses everything is the Bug-8864
    failure mode and only a positive test catches it."""
    own_id = uuid.uuid4()
    record = _llm(TEST_PROJECT_ID, own_id)
    db = _PredicateSession({
        ProjectAgentConfig: [_agent_config(answer_llm_config_id=own_id)],
        LLMProviderConfig: [record],
    })

    resolved = await resolve_agent_llm_config(TEST_PROJECT_ID, "answer", db)

    assert resolved.model_name == record.model_name


# The judge's documented null-coalesce onto the answer config (an UNSET
# judge_llm_config_id) keeps its existing home in
# ``test_judge.py::TestJudgeLlmFallback``, which now asserts the fallback and
# the project predicate together. Not duplicated here.


# ---------------------------------------------------------------------------
# Bug-8933 — a stored foreign persona_id discloses no name
# ---------------------------------------------------------------------------


def _persona(project_id: uuid.UUID, persona_id: uuid.UUID, name: str):
    return ProjectPersona(id=persona_id, project_id=project_id, name=name)


async def test_stored_foreign_persona_id_resolves_to_no_name():
    """Bug-8933. The name reaches the user on every TurnResponse, so returning
    it is a cross-project disclosure on a successful 200."""
    from src.api.conversations import _resolve_persona_name

    foreign_id = uuid.uuid4()
    db = _PredicateSession({
        ProjectPersona: [
            _persona(OTHER_PROJECT_ID, foreign_id, "EU-Restricted Finance")
        ],
    })

    name = await _resolve_persona_name(db, TEST_PROJECT_ID, foreign_id)

    assert name is None, "an out-of-project persona must resolve to no name"
    sql = str(db.executed[-1].compile())
    assert "project_personas.project_id =" in sql


async def test_a_persona_this_project_owns_still_resolves_its_name():
    from src.api.conversations import _resolve_persona_name

    own_id = uuid.uuid4()
    db = _PredicateSession({
        ProjectPersona: [_persona(TEST_PROJECT_ID, own_id, "Sales Analyst")],
    })

    assert await _resolve_persona_name(db, TEST_PROJECT_ID, own_id) == (
        "Sales Analyst"
    )


async def test_an_absent_persona_id_issues_no_lookup():
    from src.api.conversations import _resolve_persona_name

    db = _PredicateSession({ProjectPersona: []})

    assert await _resolve_persona_name(db, TEST_PROJECT_ID, None) is None
    assert db.executed == []


# ---------------------------------------------------------------------------
# Bug-8943 — a stored foreign judge_rubric_id never reaches the judge prompt
# ---------------------------------------------------------------------------


def _rubric(project_id: uuid.UUID, rubric_id: uuid.UUID, title: str):
    return AgentJudgeRubric(
        id=rubric_id,
        project_id=project_id,
        name=title,
        sections=[{"title": title, "criteria": f"{title} criteria"}],
    )


async def _run_judge_capturing_prompt(db, cfg):
    """Run the judge against a stubbed adapter and return the user prompt."""
    from src.judge.judge import run_judge

    captured: dict[str, str] = {}

    async def _complete(system, user, **_kw):
        captured["user"] = user
        return '{"verdict": "pass", "reasoning": "ok", "metrics": {}}'

    adapter = MagicMock()
    adapter.complete = AsyncMock(side_effect=_complete)
    adapter.last_usage = {"input_tokens": 1, "output_tokens": 1}
    llm_config = types.SimpleNamespace(provider="openai", model_name="gpt-4o")

    with (
        patch("src.judge.judge.resolve_agent_llm_config",
              AsyncMock(return_value=llm_config)),
        patch("src.judge.judge.build_adapter", return_value=adapter),
    ):
        await run_judge(
            db=db, cfg=cfg,
            system_prompt="## TASK\nYou are an analyst.\n",
            user_message="Show me revenue",
            plan={"query": {"model_id": "x"}},
            answer_text="Revenue is 100",
            sample_rows=[{"revenue": 100}],
            result_row_count=1,
        )
    return captured["user"]


async def test_stored_foreign_judge_rubric_is_not_rendered_into_the_prompt():
    """Bug-8943. Another project's rubric TEXT must not decide how this
    project's answers are judged; an unusable rubric means "no rubric
    configured", the same as an unset one."""
    foreign_id = uuid.uuid4()
    cfg = _agent_config(judge_rubric_id=foreign_id)
    db = _PredicateSession({
        AgentJudgeRubric: [_rubric(OTHER_PROJECT_ID, foreign_id, "ForeignRule")],
    })

    prompt = await _run_judge_capturing_prompt(db, cfg)

    assert "ForeignRule" not in prompt
    assert "no rubric configured" in prompt
    sql = str(db.executed[-1].compile())
    assert "agent_judge_rubrics.project_id =" in sql


async def test_a_rubric_this_project_owns_is_still_rendered():
    own_id = uuid.uuid4()
    cfg = _agent_config(judge_rubric_id=own_id)
    db = _PredicateSession({
        AgentJudgeRubric: [_rubric(TEST_PROJECT_ID, own_id, "OwnRule")],
    })

    prompt = await _run_judge_capturing_prompt(db, cfg)

    assert "OwnRule" in prompt
    assert "no rubric configured" not in prompt
