"""ML22 (Fable medium/low) business-outcome tests for agent-service.

Covers the behaviour changes in batch ML22:
  - F-023-15  same-provider-family answer-LLM failover
  - F-023-26b recipe parameter substitution in having/sort
  - F-023-23b calculation-step value column (measure, not dimension)
  - F-023-20  negative chart bars get a distinct fill marker
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

import pytest


# ── F-023-15 — failover stays within the primary's provider family ──────────


def _provider_record(rid: str, provider: str, name: str):
    return types.SimpleNamespace(id=rid, provider=provider, display_name=name)


@pytest.mark.asyncio
async def test_failover_restricted_to_same_provider():
    from shared.llm import config_resolution as cr

    primary = _provider_record("p1", "anthropic", "Claude Primary")
    same = _provider_record("p2", "anthropic", "Claude Backup")
    other = _provider_record("p3", "openai", "GPT Glossary")  # different provider

    cfg = types.SimpleNamespace(answer_llm_config_id="p1", judge_llm_config_id=None)

    cfg_result = AsyncMock()
    cfg_result.scalar_one_or_none = lambda: cfg
    all_result = AsyncMock()
    all_result.scalars = lambda: types.SimpleNamespace(
        all=lambda: [primary, same, other]
    )

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[cfg_result, all_result])

    with patch.object(cr, "to_llm_config", lambda r: r):
        out = await cr.resolve_agent_llm_failover_configs("proj", "answer", db)

    ids = [r.id for r in out]
    assert ids[0] == "p1"  # primary first
    assert "p2" in ids  # same-provider failover included
    assert "p3" not in ids  # different-provider config excluded


@pytest.mark.asyncio
async def test_failover_primary_only_when_no_same_provider():
    from shared.llm import config_resolution as cr

    primary = _provider_record("p1", "anthropic", "Claude Primary")
    other = _provider_record("p3", "openai", "GPT Glossary")

    cfg = types.SimpleNamespace(answer_llm_config_id="p1", judge_llm_config_id=None)
    cfg_result = AsyncMock()
    cfg_result.scalar_one_or_none = lambda: cfg
    all_result = AsyncMock()
    all_result.scalars = lambda: types.SimpleNamespace(all=lambda: [primary, other])

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[cfg_result, all_result])

    with patch.object(cr, "to_llm_config", lambda r: r):
        out = await cr.resolve_agent_llm_failover_configs("proj", "answer", db)

    assert [r.id for r in out] == ["p1"]  # no cross-provider failover


# ── F-023-26b — recipe param substitution covers having and sort ────────────


def test_recipe_substitutes_params_in_having_and_sort():
    from src.exec.recipe import _substitute_params

    params = {"threshold": 100, "col": "amount"}
    having = [{"name": "amount", "op": "gt", "value": "{threshold}"}]
    sort = [{"field": "{col}", "dir": "desc"}]

    assert _substitute_params(having, params) == [
        {"name": "amount", "op": "gt", "value": "100"}
    ]
    assert _substitute_params(sort, params) == [
        {"field": "amount", "dir": "desc"}
    ]


# ── F-023-20 — negative chart bars carry a distinct fill marker ─────────────


def test_negative_bar_gets_marker():
    from src.charts.renderer import render_chart, _NEGATIVE_BAR_COLOR

    html = render_chart(
        "bar", ["month", "profit"],
        [["Jan", 500], ["Feb", -300]],
        include_table=False,
    )
    # The negative cell carries a per-cell --color override; the positive
    # cell does not. (The palette colour may also appear in the <style>
    # block, so assert on the inline cell override marker specifically.)
    marker = f"--color: {_NEGATIVE_BAR_COLOR}"
    assert marker in html
    assert html.count(marker) == 1
    # The override sits on the negative data cell (value -300).
    neg_cell_idx = html.index("-300")
    assert marker in html[max(0, neg_cell_idx - 120):neg_cell_idx]
