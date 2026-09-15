"""Bug-9895 -- the gateway's XMLA member path over the persona model query.

``router_client.get_hierarchy_preview`` forwards the CALLER'S JWT to the
model-service hierarchy preview. When Bug-9900 raised the query-router
``/introspect/batch`` route to modeller, the preview's only source hop closed
for a viewer, so a viewer-tier BI client (Excel over XMLA) received an EMPTY
hierarchy: ``MDSCHEMA_MEMBERS`` for a level returned no members and every drill
returned nothing. Live proof on the local stack at ``origin/main`` 7ce2b3ce0,
as ``viewer@acme-demo.com`` (project ``model_viewer``, persona "Business"):

    MDSCHEMA_MEMBERS [Hierarchies].[Geography Channel].[Country] -> rows=0
    preview warning: "Failed to sample hierarchy members:
                      403: Access denied: requires 'modeler', caller has 'model_viewer'"

Bug-9895 re-expressed the preview as a projection over the persona model query,
so the same call routes through ``/execute`` with the caller's own privileges
and the viewer gets persona-filtered members (6 countries; 3 cities under GB).

These guards pin the GATEWAY half of that contract:

* the member path never asks for the per-level distinct counts it does not read
  -- each is a routed query now, so asking would spend one query per level on a
  discarded value;
* every other preview argument the member path depends on is still sent, so the
  suppression cannot silently take anything else with it.
"""

from __future__ import annotations

import inspect

import pytest

from src import router_client
from src.dax import xmla_server
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
HIER = "[Geography Channel].[Geography Channel]"


def _capture(calls, members=None):
    async def fake(**kwargs):
        calls.append(kwargs)
        return {
            "members": members
            if members is not None
            else [
                {
                    "key_value": "GB",
                    "caption": "GB",
                    "parent_key": kwargs.get("parent_key"),
                    "level_name": "Country",
                },
            ],
            "levels_summary": [],
            "warnings": [],
        }

    return fake


@pytest.mark.asyncio
async def test_member_path_suppresses_the_level_count_queries(monkeypatch):
    """The XMLA member path reads ``members`` only, so it must ask for
    ``include_level_counts=False``."""
    calls: list = []
    monkeypatch.setattr(xmla_server, "get_hierarchy_preview", _capture(calls))
    await _load_hierarchy_member_data(
        model_id="m", project_id="p", dimension=DIMENSION,
        tenant_slug="t", jwt_token="j",
        restrictions={"LEVEL_UNIQUE_NAME": [f"{HIER}.[Country]"]},
    )
    assert calls, "the member path issued no preview call"
    assert calls[0]["include_level_counts"] is False


@pytest.mark.asyncio
async def test_drill_path_also_suppresses_the_level_counts(monkeypatch):
    """A TREE_OP drill is the hot path -- one wasted routed query per level per
    expansion. It must suppress the counts too, and keep its ancestor bound."""
    calls: list = []
    monkeypatch.setattr(xmla_server, "get_hierarchy_preview", _capture(calls))
    await _load_hierarchy_member_data(
        model_id="m", project_id="p", dimension=DIMENSION,
        tenant_slug="t", jwt_token="j",
        restrictions={
            "MEMBER_UNIQUE_NAME": [f"{HIER}.[City].&[GB]&[London]"],
            "TREE_OP": ["1"],
        },
    )
    assert calls[0]["include_level_counts"] is False
    # Bug-9871 bound is untouched by the suppression.
    assert calls[0]["parent_key"] == "London"
    assert calls[0]["ancestor_keys"] == ["GB"]


@pytest.mark.asyncio
async def test_member_path_still_sends_every_argument_it_depends_on(monkeypatch):
    """A regression that dropped ``persona_id`` or ``include_key_path`` while
    adding the count suppression would silently widen or break the member set."""
    calls: list = []
    monkeypatch.setattr(xmla_server, "get_hierarchy_preview", _capture(calls))
    await _load_hierarchy_member_data(
        model_id="m", project_id="p", dimension=DIMENSION,
        tenant_slug="t", jwt_token="j",
        restrictions={"LEVEL_UNIQUE_NAME": [f"{HIER}.[City]"]},
        persona_id="persona-1",
    )
    call = calls[0]
    assert call["persona_id"] == "persona-1"
    assert call["include_key_path"] is True
    assert call["jwt_token"] == "j"
    assert call["hierarchy_id"] == "h-geo"


def test_get_hierarchy_preview_defaults_to_asking_for_counts():
    """The REST contract is unchanged for every other caller: only a caller that
    opts out loses the level counts."""
    sig = inspect.signature(router_client.get_hierarchy_preview)
    assert sig.parameters["include_level_counts"].default is True


@pytest.mark.asyncio
async def test_suppression_is_sent_on_the_wire_not_just_accepted(monkeypatch):
    """``include_level_counts`` must reach the model-service as a query
    parameter -- a default-only signature change would suppress nothing."""
    seen: dict = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"members": [], "levels_summary": [], "warnings": []}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None, params=None):
            seen["params"] = params
            return _Resp()

    monkeypatch.setattr(router_client.httpx, "AsyncClient", lambda **kw: _Client())
    await router_client.get_hierarchy_preview(
        model_id="m", hierarchy_id="h", tenant_slug="t", jwt_token="j",
        project_id="p", include_level_counts=False,
    )
    assert seen["params"]["include_level_counts"] is False

    seen.clear()
    await router_client.get_hierarchy_preview(
        model_id="m", hierarchy_id="h", tenant_slug="t", jwt_token="j",
        project_id="p",
    )
    assert "include_level_counts" not in seen["params"]
