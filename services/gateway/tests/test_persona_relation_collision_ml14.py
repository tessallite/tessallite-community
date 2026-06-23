"""F-008-15 — persona-variant relation name must not silently collide with
another model's base/variant name.

Model A (slug ``sales``) with persona ``eu`` produces the relation ``sales_eu``.
Model B's base slug is also ``sales_eu``. Before the fix the second writer
overwrote ``table_model_id``/``table_persona_id`` for ``sales_eu``, so a JDBC
client querying ``sales_eu`` could be served the wrong model. The fix
project-prefixes the colliding relation so both remain resolvable and distinct.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import router_client


MODEL_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
MODEL_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
PERSONA_EU = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def _patch(monkeypatch):
    model_a = {
        "id": MODEL_A, "project_id": "p", "project_slug": "alpha",
        "slug": "sales", "deployed_version_id": None,
    }
    model_b = {
        "id": MODEL_B, "project_id": "p", "project_slug": "beta",
        "slug": "sales_eu", "deployed_version_id": None,
    }

    async def _list_models(tenant_slug, jwt_token):
        return [model_a, model_b]

    async def _measures(mid, *a, **kw):
        return []

    async def _dimensions(mid, *a, **kw):
        return []

    async def _personas(mid, *a, **kw):
        if mid == MODEL_A:
            return [{
                "id": PERSONA_EU, "slug": "eu", "name": "EU",
                "included_measure_ids": [], "included_dimension_ids": [],
                "includes_hidden_columns": False, "restricted_column_ids": [],
            }]
        return []

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


async def test_persona_variant_does_not_overwrite_other_model(monkeypatch):
    _patch(monkeypatch)
    result = await router_client.fetch_model_metadata(None, "acme", "jwt")
    model_names = result[0]
    table_model_id = result[2]
    table_persona_id = result[5]

    # Every model must be reachable: model B's base must still map to model B.
    a_relations = {n for n, mid in table_model_id.items() if mid == MODEL_A}
    b_relations = {n for n, mid in table_model_id.items() if mid == MODEL_B}
    assert a_relations, "model A relations missing"
    assert b_relations, "model B relations missing"

    # No relation name may map to both models (the silent-overwrite bug).
    assert a_relations.isdisjoint(b_relations)

    # Exactly one of the colliding pair keeps the bare ``sales_eu`` name; the
    # other is project-prefixed and remains reachable. Whichever order they
    # register in, both endpoints stay distinct and correctly owned.
    assert "sales_eu" in model_names
    assert {"alpha__sales_eu", "beta__sales_eu"} & set(model_names), (
        "the colliding relation was not project-prefixed"
    )

    # model A's EU persona variant survives and still carries the persona id
    # (not silently dropped); model B's base survives with no persona.
    a_persona_relations = {
        n for n in a_relations if table_persona_id.get(n) == PERSONA_EU
    }
    assert a_persona_relations, "model A EU persona variant was lost"
    b_base_relations = {
        n for n in b_relations if table_persona_id.get(n) is None
    }
    assert b_base_relations, "model B base catalogue was lost"
