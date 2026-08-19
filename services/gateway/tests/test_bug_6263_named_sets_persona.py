"""Bug-6263: XMLA named-set surfaces must be persona-aware at the gateway.

The model-service ``list_named_sets`` endpoint already filters sets by persona
dimension scope when given a ``persona_id`` query param (Bug-5963). The gateway
resolves the persona from the catalog name but historically never forwarded it,
so named-set surfaces were persona-blind:

  * a multi-persona viewer tripped ``resolve_effective_persona``'s "please
    select one" 403 -> the gateway swallowed it -> silently EMPTY MDSCHEMA_SETS
    and no Execute-time inlining; and
  * a privileged caller browsing a persona-variant catalog got the UNFILTERED
    (over-broad) set list because a missing persona_id reads as "unrestricted".

This test pins the contract at the boundary the bug lived on: the gateway
client forwards the resolved persona_id as the ``persona_id`` query param, and
omits it (no filter) only when there is genuinely no persona.
"""
from __future__ import annotations

import pytest

import src.router_client as rc


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """Captures the params passed to .get and returns a canned payload."""

    captured: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        _FakeClient.captured = {"url": url, "params": params}
        return _FakeResp(
            [
                {"name": "TopCustomers", "certification_status": "certified"},
                {"name": "OldSet", "certification_status": "deprecated"},
            ]
        )


@pytest.fixture(autouse=True)
def _patch_httpx(monkeypatch):
    _FakeClient.captured = {}
    monkeypatch.setattr(rc.httpx, "AsyncClient", _FakeClient)


@pytest.mark.asyncio
async def test_persona_id_forwarded_as_query_param():
    rows = await rc.get_model_named_sets(
        "m1", "acme", "jwt", project_id="p1", persona_id="persona-abc",
    )
    assert _FakeClient.captured["params"]["persona_id"] == "persona-abc"
    # And the deprecated-set filter (F-018-13) still applies on top.
    assert [r["name"] for r in rows] == ["TopCustomers"]


@pytest.mark.asyncio
async def test_no_persona_sends_no_persona_filter():
    await rc.get_model_named_sets("m1", "acme", "jwt", project_id="p1")
    # Absent persona_id => unrestricted persona scope (base business catalog /
    # privileged unfiltered view). Bug-8384 added an unconditional
    # ``deployed_only`` param, so assert on the persona key specifically rather
    # than on the whole params dict being empty.
    assert "persona_id" not in _FakeClient.captured["params"]


@pytest.mark.asyncio
async def test_empty_persona_id_is_not_forwarded():
    await rc.get_model_named_sets(
        "m1", "acme", "jwt", project_id="p1", persona_id="",
    )
    assert "persona_id" not in _FakeClient.captured["params"]


@pytest.mark.asyncio
@pytest.mark.parametrize("persona_id", [None, "", "persona-abc"])
async def test_deployed_only_is_always_requested(persona_id):
    """Bug-8384: BI named-set reads are pinned to the DEPLOYED snapshot.

    Both callers of this client function are BI surfaces (XMLA MDSCHEMA_SETS
    discovery and Execute-time MDX inlining). A named set's expression IS query
    semantics, so an undeployed draft edit reaching a BI client silently changes
    what Excel/Power BI computes before Deploy. The flag must be sent on EVERY
    call, independent of persona resolution.
    """
    await rc.get_model_named_sets(
        "m1", "acme", "jwt", project_id="p1", persona_id=persona_id,
    )
    assert _FakeClient.captured["params"]["deployed_only"] == "true"
