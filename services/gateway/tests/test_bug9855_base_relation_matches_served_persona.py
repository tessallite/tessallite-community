"""Bug-9855 -- JDBC ``SELECT *`` as a restricted persona: Describe said 144
columns, Execute carried 100, and the gateway's own shape check refused the
statement.

The base relation (``<slug>``) was built unrestricted for every caller while
the query-router auto-resolves the caller's persona at execute time. The
catalogue now mirrors ``resolve_effective_persona`` for a caller that names no
persona: privileged callers stay unrestricted; a non-privileged caller with one
assigned persona gets that persona's columns on the base relation (and the
relation carries the persona id so execution is explicit); a technical-persona
holder gets the technical persona; none or several leave the base unrestricted.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import router_client

MODEL = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PERSONA = "cccccccc-cccc-cccc-cccc-cccccccccccc"
PERSONA2 = "dddddddd-dddd-dddd-dddd-dddddddddddd"


def _patch(monkeypatch, personas):
    model = {
        "id": MODEL, "project_id": "p", "project_slug": "alpha",
        "slug": "sales", "deployed_version_id": None,
    }

    async def _list_models(tenant_slug, jwt_token):
        return [model]

    async def _measures(mid, *a, **kw):
        return [
            {"id": "m1", "name": "revenue", "default_agg": "sum", "is_hidden": False},
            {"id": "m2", "name": "cost", "default_agg": "sum", "is_hidden": False},
        ]

    async def _dimensions(mid, *a, **kw):
        return [
            {"id": "d1", "name": "region", "data_type": "text", "is_hidden": False},
            {"id": "d2", "name": "customer_email", "data_type": "text", "is_hidden": False},
        ]

    async def _personas(mid, *a, **kw):
        return personas

    async def _snapshot(mid, *a, **kw):
        return {"columns": [], "tables": []}

    async def _kpis(mid, *a, **kw):
        return []

    monkeypatch.setattr(router_client, "list_all_models_for_tenant", _list_models)
    monkeypatch.setattr(router_client, "get_model_measures", _measures)
    monkeypatch.setattr(router_client, "get_model_dimensions", _dimensions)
    monkeypatch.setattr(router_client, "get_model_personas", _personas)
    monkeypatch.setattr(router_client, "get_model_snapshot", _snapshot)
    monkeypatch.setattr(router_client, "get_model_kpis", _kpis)


def _viewer_persona(pid=PERSONA, slug="viewer"):
    return {
        "id": pid, "slug": slug, "name": slug.title(),
        "included_measure_ids": ["m1"], "included_dimension_ids": ["d1"],
        "includes_hidden_columns": False, "restricted_column_ids": [],
    }


async def _columns(monkeypatch, personas, privileged):
    _patch(monkeypatch, personas)
    monkeypatch.setattr(router_client, "_caller_is_privileged", lambda _tok: privileged)
    result = await router_client.fetch_model_metadata(None, "acme", "jwt")
    table_columns, table_persona_id = result[1], result[5]
    return {n: {c["name"] for c in cols} for n, cols in table_columns.items()}, table_persona_id


@pytest.mark.asyncio
async def test_single_persona_viewer_base_relation_is_the_persona_view(monkeypatch):
    names, persona_ids = await _columns(monkeypatch, [_viewer_persona()], privileged=False)
    assert names["sales"] == {"revenue", "region"}
    assert names["sales_viewer"] == {"revenue", "region"}
    assert persona_ids["sales"] == PERSONA


@pytest.mark.asyncio
async def test_privileged_caller_base_relation_stays_unrestricted(monkeypatch):
    names, persona_ids = await _columns(monkeypatch, [_viewer_persona()], privileged=True)
    assert names["sales"] == {"revenue", "cost", "region", "customer_email"}
    assert persona_ids["sales"] is None


@pytest.mark.asyncio
async def test_multi_persona_viewer_base_relation_stays_unrestricted(monkeypatch):
    """The router refuses the ambiguous base query explicitly; Describe must not guess."""
    names, persona_ids = await _columns(
        monkeypatch, [_viewer_persona(), _viewer_persona(PERSONA2, "finance")], privileged=False,
    )
    assert names["sales"] == {"revenue", "cost", "region", "customer_email"}
    assert persona_ids["sales"] is None


@pytest.mark.asyncio
async def test_technical_holder_base_relation_is_the_technical_persona(monkeypatch):
    tech = {**_viewer_persona(PERSONA2, "technical"), "includes_hidden_columns": True,
            "included_measure_ids": [], "included_dimension_ids": []}
    names, persona_ids = await _columns(monkeypatch, [_viewer_persona(), tech], privileged=False)
    assert persona_ids["sales"] == PERSONA2


def test_privilege_is_read_from_the_token_role_claims(monkeypatch):
    monkeypatch.setattr(router_client, "decode_access_token", lambda t: {"role": "tenant_admin"})
    assert router_client._caller_is_privileged("x") is True
    monkeypatch.setattr(router_client, "decode_access_token", lambda t: {"role": "viewer", "roles": ["modeler"]})
    assert router_client._caller_is_privileged("x") is True
    monkeypatch.setattr(router_client, "decode_access_token", lambda t: {"role": "viewer"})
    assert router_client._caller_is_privileged("x") is False

    def _boom(t):
        raise ValueError("bad token")
    monkeypatch.setattr(router_client, "decode_access_token", _boom)
    assert router_client._caller_is_privileged("x") is False
