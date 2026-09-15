"""Bug-9871 -- a hierarchy drill was bounded by the parent's own key only.

Expanding [Month].&[2025]&[9] asked the preview for children of parent_key
"9" and got every day of every September in the calendar (300 rows on the
demo model). The drill must carry the keys of the levels above the parent
(root first) so the preview bounds it by the full ancestor path. This guard
pins the gateway side: every parent-bounded preview call passes
``ancestor_keys`` derived from the canonical member unique name.
"""

from __future__ import annotations

import pytest

from src.dax import xmla_server
from src.dax.xmla_server import _load_hierarchy_member_data

DIMENSION = {
    "name": "business_date Calendar",
    "source": "hierarchy",
    "hierarchy_id": "h-cal",
    "levels": [
        {"name": "Year", "ordinal": 0},
        {"name": "Month", "ordinal": 1},
        {"name": "Day", "ordinal": 2},
    ],
}
HIER = "[business_date Calendar].[business_date Calendar]"


def _capture(calls):
    async def fake(**kwargs):
        calls.append(kwargs)
        return {"members": [
            {"key_value": "2025-09-01", "caption": "2025-09-01", "parent_key": kwargs.get("parent_key"), "level_name": "Day"},
        ]}
    return fake


@pytest.mark.asyncio
async def test_children_drill_passes_the_ancestor_keys_above_the_parent(monkeypatch):
    calls: list = []
    monkeypatch.setattr(xmla_server, "get_hierarchy_preview", _capture(calls))
    await _load_hierarchy_member_data(
        model_id="m", project_id="p", dimension=DIMENSION, tenant_slug="t", jwt_token="j",
        restrictions={"MEMBER_UNIQUE_NAME": [f"{HIER}.[Month].&[2025]&[9]"], "TREE_OP": ["1"]},
    )
    assert calls[0]["parent_key"] == "9"
    assert calls[0]["ancestor_keys"] == ["2025"]
    assert calls[0]["expand_level"] == 2


@pytest.mark.asyncio
async def test_self_request_child_load_passes_the_ancestor_keys(monkeypatch):
    """Bug-9870's self-row child load shares the primitive and the exposure."""
    calls: list = []
    monkeypatch.setattr(xmla_server, "get_hierarchy_preview", _capture(calls))
    await _load_hierarchy_member_data(
        model_id="m", project_id="p", dimension=DIMENSION, tenant_slug="t", jwt_token="j",
        restrictions={"MEMBER_UNIQUE_NAME": [f"{HIER}.[Month].&[2025]&[9]"], "TREE_OP": ["8"]},
    )
    assert calls[0]["parent_key"] == "9"
    assert calls[0]["ancestor_keys"] == ["2025"]


@pytest.mark.asyncio
async def test_siblings_drill_passes_the_ancestors_above_the_shared_parent(monkeypatch):
    calls: list = []
    monkeypatch.setattr(xmla_server, "get_hierarchy_preview", _capture(calls))
    await _load_hierarchy_member_data(
        model_id="m", project_id="p", dimension=DIMENSION, tenant_slug="t", jwt_token="j",
        restrictions={"MEMBER_UNIQUE_NAME": [f"{HIER}.[Day].&[2025]&[9]&[2025-09-01]"], "TREE_OP": ["4"]},
    )
    assert calls[0]["parent_key"] == "9"
    assert calls[0]["ancestor_keys"] == ["2025"]
    assert calls[0]["expand_level"] == 2


@pytest.mark.asyncio
async def test_first_level_children_carry_no_ancestor_keys(monkeypatch):
    calls: list = []
    monkeypatch.setattr(xmla_server, "get_hierarchy_preview", _capture(calls))
    await _load_hierarchy_member_data(
        model_id="m", project_id="p", dimension=DIMENSION, tenant_slug="t", jwt_token="j",
        restrictions={"MEMBER_UNIQUE_NAME": [f"{HIER}.[Year].&[2025]"], "TREE_OP": ["1"]},
    )
    assert calls[0]["parent_key"] == "2025"
    assert calls[0]["ancestor_keys"] is None
