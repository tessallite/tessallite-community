"""Bug-9870 -- a placed hierarchy could not be expanded by hand in Excel.

Excel asks MDSCHEMA_MEMBERS for the clicked member with TREE_OP 8 (self) and
expands only when that row's CHILDREN_CARDINALITY is non-zero. The self-only
loader returned the member alone, so the count was 0 for every non-leaf
member of a lazily loaded hierarchy (owner's ALEX session 2026-09-04:
Geography Channel placed, `+` did nothing, no request reached the gateway).
The harness never saw it because ``DrilledDown`` through COM bypasses the
check. A self request on a non-leaf member now loads that member's children
with the same parent-bounded preview the TREE_OP 1 path uses.
"""

from __future__ import annotations

import pytest

from src.dax import xmla_server
from src.dax.mdschema import _rows_members
from src.dax.xmla_server import _load_hierarchy_member_data

DIMENSION = {
    "name": "Geography Channel",
    "source": "hierarchy",
    "hierarchy_id": "h-geo",
    "levels": [
        {"name": "Country", "ordinal": 0},
        {"name": "City", "ordinal": 1},
        {"name": "Channel", "ordinal": 2},
    ],
}
# Internal grammar: _load_discover_member_data normalises Excel's wire form
# ([Hierarchies].[...]) to this before the loader runs (Bug-9771).
HIER = "[Geography Channel].[Geography Channel]"
WIRE = "[Hierarchies].[Geography Channel]"


def _preview(calls):
    async def fake(**kwargs):
        calls.append(kwargs)
        if kwargs.get("parent_key") == "GB":
            return {"members": [
                {"key_value": c, "caption": c, "parent_key": "GB", "level_name": "City"}
                for c in ("Berlin", "London", "Madrid")
            ]}
        return {"members": []}
    return fake


@pytest.mark.asyncio
async def test_self_request_on_a_non_leaf_member_loads_its_children(monkeypatch):
    calls: list = []
    monkeypatch.setattr(xmla_server, "get_hierarchy_preview", _preview(calls))
    data = await _load_hierarchy_member_data(
        model_id="m", project_id="p", dimension=DIMENSION, tenant_slug="t",
        jwt_token="j",
        restrictions={
            "MEMBER_UNIQUE_NAME": [f"{HIER}.[Country].&[GB]"],
            "LEVEL_UNIQUE_NAME": [f"{HIER}.[Country]"],
            "TREE_OP": ["8"],
        },
    )
    assert calls and calls[0]["parent_key"] == "GB" and calls[0]["expand_level"] == 1
    assert [m["name"] for m in data["members_by_level"]["0"]] == ["GB"]
    assert [m["name"] for m in data["members_by_level"]["1"]] == ["Berlin", "London", "Madrid"]

    rows = _rows_members(
        "m", [{"name": "base_amount"}], [DIMENSION],
        {"MEMBER_UNIQUE_NAME": [f"{WIRE}.[Country].&[GB]"], "TREE_OP": ["8"]},
        {"Geography Channel": data},
        properties={"SspropInitAppName": "Microsoft Office Excel"},
    )
    gb = [r for r in rows if r.get("MEMBER_UNIQUE_NAME", "").endswith("[Country].&[GB]")]
    assert len(gb) == 1
    assert gb[0]["CHILDREN_CARDINALITY"] == "3"


@pytest.mark.asyncio
async def test_self_request_on_a_leaf_member_makes_no_preview_call(monkeypatch):
    calls: list = []
    monkeypatch.setattr(xmla_server, "get_hierarchy_preview", _preview(calls))
    data = await _load_hierarchy_member_data(
        model_id="m", project_id="p", dimension=DIMENSION, tenant_slug="t",
        jwt_token="j",
        restrictions={
            "MEMBER_UNIQUE_NAME": [f"{HIER}.[Channel].&[GB]&[London]&[API]"],
            "TREE_OP": ["8"],
        },
    )
    assert calls == []
    assert [m["name"] for m in data["members_by_level"]["2"]] == ["API"]


@pytest.mark.asyncio
async def test_self_request_below_the_first_level_is_answered_with_its_children(monkeypatch):
    """A path-qualified member ([City].&[GB]&[London]) used to produce NO self
    row at all (the matcher needs the ancestor path), so a city could never be
    expanded to its channels."""
    async def fake(**kwargs):
        if kwargs.get("parent_key") == "London" and kwargs.get("expand_level") == 2:
            return {"members": [
                {"key_value": c, "caption": c, "parent_key": "London", "level_name": "Channel"}
                for c in ("API", "BRANCH")
            ]}
        return {"members": []}
    monkeypatch.setattr(xmla_server, "get_hierarchy_preview", fake)
    data = await _load_hierarchy_member_data(
        model_id="m", project_id="p", dimension=DIMENSION, tenant_slug="t",
        jwt_token="j",
        restrictions={
            # Excel sends no LEVEL_UNIQUE_NAME with its self request.
            "MEMBER_UNIQUE_NAME": [f"{HIER}.[City].&[GB]&[London]"],
            "TREE_OP": ["8"],
        },
    )
    rows = _rows_members(
        "m", [{"name": "base_amount"}], [DIMENSION],
        {"MEMBER_UNIQUE_NAME": [f"{WIRE}.[City].&[GB]&[London]"], "TREE_OP": ["8"]},
        {"Geography Channel": data},
        properties={"SspropInitAppName": "Microsoft Office Excel"},
    )
    london = [r for r in rows if r.get("MEMBER_UNIQUE_NAME", "").endswith("[City].&[GB]&[London]")]
    assert len(london) == 1, [r.get("MEMBER_UNIQUE_NAME") for r in rows]
    assert london[0]["CHILDREN_CARDINALITY"] == "2"
    assert london[0]["PARENT_UNIQUE_NAME"].endswith("[Country].&[GB]")


@pytest.mark.asyncio
async def test_children_request_without_a_level_restriction_drills_the_right_level(monkeypatch):
    """Excel's TREE_OP 1 request for a month carries no LEVEL_UNIQUE_NAME
    either; the children must be the level below the member's own level."""
    calls: list = []

    async def fake(**kwargs):
        calls.append(kwargs)
        return {"members": [
            {"key_value": "2025-09-01", "caption": "2025-09-01", "parent_key": "9", "level_name": "Channel"},
        ]}
    monkeypatch.setattr(xmla_server, "get_hierarchy_preview", fake)
    await _load_hierarchy_member_data(
        model_id="m", project_id="p", dimension=DIMENSION, tenant_slug="t",
        jwt_token="j",
        restrictions={
            "MEMBER_UNIQUE_NAME": [f"{HIER}.[City].&[GB]&[London]"],
            "TREE_OP": ["1"],
        },
    )
    assert calls[0]["expand_level"] == 2 and calls[0]["parent_key"] == "London"
