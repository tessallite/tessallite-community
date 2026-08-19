"""F-008-05 — JDBC $KPIs must be registered per persona variant, carrying the
persona id, so an administrator impersonating a persona catalogue exercises the
SAME query-router KPI persona/CLS gate a real assigned user hits.

Before the fix a single model-level ``<slug>$KPIs`` relation (persona_id=None)
served every catalogue, so admin impersonation resolved to an unrestricted
persona for KPI queries — a faithful-impersonation failure and a potential KPI
lineage disclosure.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import router_client


MODEL = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PERSONA = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def _patch(monkeypatch):
    model = {
        "id": MODEL, "project_id": "p", "project_slug": "alpha",
        "slug": "sales", "deployed_version_id": None,
    }

    async def _list_models(tenant_slug, jwt_token):
        return [model]

    async def _measures(mid, *a, **kw):
        return []

    async def _dimensions(mid, *a, **kw):
        return []

    async def _personas(mid, *a, **kw):
        return [{
            "id": PERSONA, "slug": "eu", "name": "EU",
            "included_measure_ids": [], "included_dimension_ids": [],
            "includes_hidden_columns": False, "restricted_column_ids": [],
        }]

    async def _snapshot(mid, *a, **kw):
        return {"columns": [], "tables": []}

    async def _kpis(mid, *a, **kw):
        return [{"id": "k1", "name": "Revenue KPI"}]

    monkeypatch.setattr(router_client, "list_all_models_for_tenant", _list_models)
    monkeypatch.setattr(router_client, "get_model_measures", _measures)
    monkeypatch.setattr(router_client, "get_model_dimensions", _dimensions)
    monkeypatch.setattr(router_client, "get_model_personas", _personas)
    monkeypatch.setattr(router_client, "get_model_snapshot", _snapshot)
    monkeypatch.setattr(router_client, "get_model_kpis", _kpis)


async def test_kpis_relation_registered_per_persona_with_persona_id(monkeypatch):
    _patch(monkeypatch)
    result = await router_client.fetch_model_metadata(None, "acme", "jwt")
    model_names = result[0]
    table_persona_id = result[5]

    kpi_relations = [n for n in model_names if n.lower().endswith("$kpis")]

    # The base $KPIs (persona_id=None) AND a persona-scoped $KPIs are registered.
    base_kpis = [n for n in kpi_relations if table_persona_id.get(n) is None]
    persona_kpis = [n for n in kpi_relations if table_persona_id.get(n) == PERSONA]

    assert base_kpis, "base $KPIs relation missing"
    assert persona_kpis, (
        "F-008-05: persona-scoped $KPIs relation missing — admin impersonation "
        "would resolve to persona_id=None and skip the KPI persona/CLS gate"
    )
    # The persona-scoped relation carries the persona slug so a client can pick
    # it (`sales_eu$KPIs`), and it is distinct from the base.
    assert any("eu" in n.lower() for n in persona_kpis)
    assert set(base_kpis).isdisjoint(set(persona_kpis))
