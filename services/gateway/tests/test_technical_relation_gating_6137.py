"""F-008-31 / Bug-6137 — the auto-injected ``<slug>_technical`` relation must
be gated to the ``model_technical`` audience, not served to every JDBC/XMLA
caller.

The gateway auto-injects a ``<slug>_technical`` relation (physical column
names, ``include_hidden=True``) for models that lack a Technical persona
variant. That fallback re-exposed the hidden-column surface migration 0124 /
F-008-04 closed: a viewer with no ``model_technical`` grant received the
technical relation.

``fetch_model_metadata`` relies on ``get_model_personas`` (called with
``for_audience=true``), which returns a hidden-columns persona ONLY when the
caller is authorised for it (privileged users see all). The fix gates the
fallback on the presence of a hidden-columns persona in that audience-filtered
list, so an unauthorised caller never receives the ``_technical`` relation.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import router_client


MODEL = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
TABLE = "11111111-1111-1111-1111-111111111111"
TECH_PERSONA = "dddddddd-dddd-dddd-dddd-dddddddddddd"


def _patch(monkeypatch, personas):
    model = {
        "id": MODEL, "project_id": "p", "project_slug": "public",
        "slug": "sales", "deployed_version_id": None, "description": "Sales",
    }

    async def _list_models(tenant_slug, jwt_token):
        return [model]

    async def _measures(mid, *a, **kw):
        return []

    async def _dimensions(mid, *a, **kw):
        return []

    async def _personas(mid, *a, **kw):
        # Emulates the model-service audience filter: the caller only sees the
        # personas passed in (for a non-privileged viewer a gated technical
        # persona is absent).
        return list(personas)

    async def _snapshot(mid, *a, **kw):
        return {
            "tables": [
                {
                    "id": TABLE, "table_type": "fact",
                    "physical_name": "fact_sales", "alias": "sales",
                    "row_count_estimate": 100,
                },
            ],
            "columns": [
                {"model_table_id": TABLE, "column_name": "amount",
                 "data_type": "numeric", "is_hidden": False},
                {"model_table_id": TABLE, "column_name": "internal_cost",
                 "data_type": "numeric", "is_hidden": True},
            ],
        }

    async def _kpis(mid, *a, **kw):
        return []

    monkeypatch.setattr(router_client, "list_all_models_for_tenant", _list_models)
    monkeypatch.setattr(router_client, "get_model_measures", _measures)
    monkeypatch.setattr(router_client, "get_model_dimensions", _dimensions)
    monkeypatch.setattr(router_client, "get_model_personas", _personas)
    monkeypatch.setattr(router_client, "get_model_snapshot", _snapshot)
    monkeypatch.setattr(router_client, "get_model_kpis", _kpis)


@pytest.mark.asyncio
async def test_unauthorized_viewer_does_not_get_technical_relation(monkeypatch):
    # A viewer with no model_technical grant: the audience-filtered persona
    # list contains no hidden-columns persona.
    _patch(monkeypatch, personas=[])
    result = await router_client.fetch_model_metadata(None, "acme", "jwt")
    model_names = result[0]
    assert "sales" in model_names
    assert "sales_technical" not in model_names, (
        "the ungated technical fallback was re-exposed to a non-privileged caller"
    )


@pytest.mark.asyncio
async def test_business_persona_does_not_enable_technical_fallback(monkeypatch):
    # A non-admin caller may legitimately see a business persona through the
    # audience filter. That must not be treated as authorization for the
    # physical-name technical fallback.
    business_persona = {
        "id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
        "slug": "sales_team",
        "name": "Sales Team",
        "included_measure_ids": [],
        "included_dimension_ids": [],
        "included_hierarchy_ids": [],
        "restricted_column_ids": [],
        "includes_hidden_columns": False,
        "audience_roles": ["viewer"],
    }
    _patch(monkeypatch, personas=[business_persona])
    result = await router_client.fetch_model_metadata(None, "acme", "jwt")
    model_names = result[0]
    assert "sales_sales_team" in model_names
    assert "sales_technical" not in model_names


@pytest.mark.asyncio
async def test_authorized_technical_caller_gets_technical_relation(monkeypatch):
    # An authorised caller: the audience filter surfaced a hidden-columns
    # persona (slug != 'technical', so no persona variant already claims the
    # ``_technical`` name — the fallback should fire).
    tech_persona = {
        "id": TECH_PERSONA, "slug": "tech", "name": "Technical",
        "included_measure_ids": [], "included_dimension_ids": [],
        "included_hierarchy_ids": [], "restricted_column_ids": [],
        "includes_hidden_columns": True, "audience_roles": ["model_technical"],
    }
    _patch(monkeypatch, personas=[tech_persona])
    result = await router_client.fetch_model_metadata(None, "acme", "jwt")
    model_names = result[0]
    table_include_hidden = result[6]
    assert "sales_technical" in model_names, (
        "an authorised technical caller lost the physical-name technical view"
    )
    assert table_include_hidden.get("sales_technical") is True
