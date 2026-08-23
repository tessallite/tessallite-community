"""Gateway catalog field list must be pinned to the DEPLOYED snapshot (B15 / F-013-01).

Bug-1235: the XMLA/JDBC catalog measure/dimension field list was built from the
LIVE ``/measures`` + ``/dimensions`` endpoints, so a draft add/rename/hide on an
already-deployed model leaked into the Excel / Power BI field list before the
next Save+Deploy. The fix sources the catalog field list from the deployed
version's immutable ``snapshot_json`` when the model is deployed.

These tests drive ``fetch_model_metadata`` end-to-end with the model-service HTTP
client functions monkeypatched, and assert the REAL invariant: the published
catalog field set equals the DEPLOYED snapshot's measure/dimension names — not
the live draft — and only changes after a new version is deployed.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import router_client


MODEL_ID = "11111111-1111-1111-1111-111111111111"
PROJECT_ID = "22222222-2222-2222-2222-222222222222"
DEPLOYED_VERSION_ID = "33333333-3333-3333-3333-333333333333"

# The deployed snapshot the model was deployed with. Field list MUST match this.
DEPLOYED_SNAPSHOT = {
    "measures": [
        {"id": "m1", "name": "Revenue", "display_name": "Revenue", "default_agg": "sum"},
        {"id": "m2", "name": "Cost", "display_name": "Cost", "default_agg": "sum"},
    ],
    "dimensions": [
        {"id": "d1", "name": "Region", "data_type": "text"},
        {"id": "d2", "name": "Product", "data_type": "text"},
    ],
    "columns": [],
    "tables": [],
}

# The LIVE draft state: "Revenue" was renamed to "Net Revenue", a brand-new
# measure "Profit" was added, and a new dimension "Channel" was added — all
# WITHOUT a Save+Deploy. None of these may appear in the deployed catalog.
LIVE_DRAFT_MEASURES = [
    {"id": "m1", "name": "Net Revenue", "display_name": "Net Revenue", "default_agg": "sum"},
    {"id": "m2", "name": "Cost", "display_name": "Cost", "default_agg": "sum"},
    {"id": "m3", "name": "Profit", "display_name": "Profit", "default_agg": "sum"},
]
LIVE_DRAFT_DIMENSIONS = [
    {"id": "d1", "name": "Region", "data_type": "text"},
    {"id": "d2", "name": "Product", "data_type": "text"},
    {"id": "d3", "name": "Channel", "data_type": "text"},
]


def _patch_clients(
    monkeypatch,
    *,
    deployed_version_id,
    live_measures,
    live_dimensions,
    deployed_snapshot,
):
    """Wire fetch_model_metadata's HTTP client calls to in-memory fixtures."""
    model = {
        "id": MODEL_ID,
        "project_id": PROJECT_ID,
        "project_slug": "public",
        "slug": "sales",
        "deployed_version_id": deployed_version_id,
    }

    async def _list_models(tenant_slug, jwt_token):
        return [model]

    async def _measures(mid, tenant_slug, jwt_token, project_id=""):
        return live_measures

    async def _dimensions(mid, tenant_slug, jwt_token, project_id=""):
        return live_dimensions

    async def _personas(mid, tenant_slug, jwt_token, project_id=""):
        return []

    async def _live_snapshot(mid, tenant_slug, jwt_token, project_id=""):
        # The live "snapshot-export" snapshot reflects the DRAFT, not the
        # deployed version. Returning the draft columns proves the pin uses the
        # deployed-version snapshot rather than this live one.
        return {"columns": [], "tables": []}

    async def _kpis(mid, tenant_slug, jwt_token, project_id=""):
        return []

    captured = {"version_id": None}

    async def _version_snapshot(mid, version_id, tenant_slug, jwt_token, project_id=""):
        captured["version_id"] = version_id
        return deployed_snapshot

    monkeypatch.setattr(router_client, "list_all_models_for_tenant", _list_models)
    monkeypatch.setattr(router_client, "get_model_measures", _measures)
    monkeypatch.setattr(router_client, "get_model_dimensions", _dimensions)
    monkeypatch.setattr(router_client, "get_model_personas", _personas)
    monkeypatch.setattr(router_client, "get_model_snapshot", _live_snapshot)
    monkeypatch.setattr(router_client, "get_model_kpis", _kpis)
    monkeypatch.setattr(router_client, "get_model_version_snapshot", _version_snapshot)
    return captured


def _field_names(table_columns):
    base = table_columns["sales"]
    return {c["name"] for c in base}


async def test_deployed_catalog_ignores_draft_rename_and_add(monkeypatch):
    """After deploy, a draft rename/add must NOT appear in the field list.

    The deployed snapshot has Revenue/Cost + Region/Product. The live draft
    renamed Revenue->Net Revenue and added Profit + Channel. The catalog must
    advertise EXACTLY the deployed names.
    """
    captured = _patch_clients(
        monkeypatch,
        deployed_version_id=DEPLOYED_VERSION_ID,
        live_measures=LIVE_DRAFT_MEASURES,
        live_dimensions=LIVE_DRAFT_DIMENSIONS,
        deployed_snapshot=DEPLOYED_SNAPSHOT,
    )

    result = await router_client.fetch_model_metadata(MODEL_ID, "acme", "jwt")
    table_columns = result[1]
    names = _field_names(table_columns)

    # The deployed version snapshot was the source.
    assert captured["version_id"] == DEPLOYED_VERSION_ID

    # Catalog == deployed snapshot field set, exactly.
    assert names == {"Revenue", "Cost", "Region", "Product"}

    # The draft leak is closed: none of the draft-only edits surface.
    assert "Net Revenue" not in names, "draft rename leaked into deployed catalog"
    assert "Profit" not in names, "draft-added measure leaked into deployed catalog"
    assert "Channel" not in names, "draft-added dimension leaked into deployed catalog"


@pytest.mark.asyncio
async def test_bug9433_catalog_honors_hidden_source_columns_for_select_star(monkeypatch):
    """The gateway descriptor must match the deployed SELECT * projection.

    Bug-9433: deployed physical columns are independently curated from the
    semantic dimension/measure rows.  A source-backed object whose semantic
    ``is_hidden`` flag is false is still removed by query-router when its
    deployed snapshot column is hidden.  Business catalogues must omit it,
    while an authorised hidden-column persona keeps the field available.
    """
    deployed_snapshot = {
        "measures": [
            {"id": "m-visible", "name": "Revenue", "default_agg": "sum",
             "source_column_id": "c-visible-measure", "is_hidden": False},
            {"id": "m-hidden", "name": "Internal Revenue", "default_agg": "sum",
             "source_column_id": "c-hidden-measure", "is_hidden": False},
        ],
        "dimensions": [
            {"id": "d-visible", "name": "Region", "data_type": "text",
             "source_column_id": "c-visible-dimension", "is_hidden": False},
            {"id": "d-hidden", "name": "Payment Token", "data_type": "text",
             "source_column_id": "c-hidden-dimension", "is_hidden": False},
        ],
        "columns": [
            {"id": "c-visible-measure", "model_table_id": "table-1",
             "is_hidden": False},
            {"id": "c-hidden-measure", "model_table_id": "table-1",
             "is_hidden": True},
            {"id": "c-visible-dimension", "model_table_id": "table-1",
             "is_hidden": False},
            {"id": "c-hidden-dimension", "model_table_id": "table-1",
             "is_hidden": True},
        ],
        "tables": [{
            "id": "table-1",
            "alias": "sales_detail",
            "table_type": "fact",
            "row_count_estimate": 100,
        }],
    }
    _patch_clients(
        monkeypatch,
        deployed_version_id=DEPLOYED_VERSION_ID,
        live_measures=[],
        live_dimensions=[],
        deployed_snapshot=deployed_snapshot,
    )

    async def _authorized_persona(mid, tenant_slug, jwt_token, project_id=""):
        return [{
            "id": "persona-technical",
            "slug": "analyst",
            "includes_hidden_columns": True,
            "included_measure_ids": [],
            "included_dimension_ids": [],
            "restricted_column_ids": [],
        }]

    monkeypatch.setattr(router_client, "get_model_personas", _authorized_persona)
    monkeypatch.setattr(router_client.settings, "LOOKER_GATEWAY_ENABLED", True)
    result = await router_client.fetch_model_metadata(MODEL_ID, "acme", "jwt")
    table_columns = result[1]

    base_names = {column["name"] for column in table_columns["sales"]}
    assert base_names == {"Region", "Revenue"}
    assert "Payment Token" not in base_names
    assert "Internal Revenue" not in base_names

    persona_columns = {
        column["name"]: column for column in table_columns["sales_analyst"]
    }
    assert {"Region", "Revenue", "Payment Token", "Internal Revenue"} == set(persona_columns)
    assert persona_columns["Payment Token"]["is_hidden"] is True
    assert persona_columns["Internal Revenue"]["is_hidden"] is True

    # The table-scoped Looker relation is intentionally technical: hidden
    # fields remain available, but its metadata must carry the same effective
    # physical-column curation as the business/persona surfaces. Assert both
    # changed branches so a semantic-only mutant in either assignment fails.
    looker_columns = {
        column["name"]: column
        for column in table_columns["sales__sales_detail"]
    }
    assert set(looker_columns) == {
        "Region", "Revenue", "Payment Token", "Internal Revenue",
    }
    assert looker_columns["Region"]["is_hidden"] is False
    assert looker_columns["Revenue"]["is_hidden"] is False
    assert looker_columns["Payment Token"]["is_hidden"] is True
    assert looker_columns["Internal Revenue"]["is_hidden"] is True


async def test_catalog_updates_after_save_and_deploy(monkeypatch):
    """After Save+Deploy, the new version's snapshot IS the catalog.

    Deploying a new version (which now contains the renamed measure and the
    added measure/dimension) flips the catalog to the new field set.
    """
    new_deployed_snapshot = {
        "measures": LIVE_DRAFT_MEASURES,        # Net Revenue, Cost, Profit
        "dimensions": LIVE_DRAFT_DIMENSIONS,    # Region, Product, Channel
        "columns": [],
        "tables": [],
    }
    new_version_id = "44444444-4444-4444-4444-444444444444"
    captured = _patch_clients(
        monkeypatch,
        deployed_version_id=new_version_id,
        live_measures=LIVE_DRAFT_MEASURES,
        live_dimensions=LIVE_DRAFT_DIMENSIONS,
        deployed_snapshot=new_deployed_snapshot,
    )

    result = await router_client.fetch_model_metadata(MODEL_ID, "acme", "jwt")
    names = _field_names(result[1])

    assert captured["version_id"] == new_version_id
    assert names == {"Net Revenue", "Cost", "Profit", "Region", "Product", "Channel"}


async def test_empty_deployed_snapshot_must_not_leak_draft_fields(monkeypatch):
    """CONTRACT (fail-closed, Bug-7979): an empty deployed snapshot must NOT
    expose drafts.

    For a DEPLOYED model, the published catalog field list is the deployed
    contract. If the deployed snapshot is missing/empty/corrupt, the correct
    behaviour is to fail CLOSED -- advertise the empty deployed field set, never
    fall back to live draft metadata. This prevents unpublished renames/additions
    from leaking into Excel / Power BI field lists.

    Wave 2 Lane A1 fix: the gateway catalogue now ALWAYS uses the deployed
    snapshot content when a deploy pointer exists, even when empty.
    """
    _patch_clients(
        monkeypatch,
        deployed_version_id=DEPLOYED_VERSION_ID,
        live_measures=LIVE_DRAFT_MEASURES,
        live_dimensions=LIVE_DRAFT_DIMENSIONS,
        deployed_snapshot={"measures": [], "dimensions": [], "columns": []},
    )

    result = await router_client.fetch_model_metadata(MODEL_ID, "acme", "jwt")
    names = _field_names(result[1])

    # Fail-closed: draft-only edits that were never deployed must NOT leak into
    # the published catalog, even when the deployed snapshot is empty.
    assert "Net Revenue" not in names, "draft rename leaked via empty-snapshot fallback"
    assert "Profit" not in names, "draft-added measure leaked via empty-snapshot fallback"
    assert "Channel" not in names, "draft-added dimension leaked via empty-snapshot fallback"


async def test_undeployed_model_uses_live_list(monkeypatch):
    """A model with no deploy pointer keeps the prior live-load behaviour.

    Belt-and-braces: in the real BI path only deployed models reach the
    gateway, but the pin must be additive — when deployed_version_id is None
    the field list is the live draft and no version fetch happens.
    """
    captured = _patch_clients(
        monkeypatch,
        deployed_version_id=None,
        live_measures=LIVE_DRAFT_MEASURES,
        live_dimensions=LIVE_DRAFT_DIMENSIONS,
        deployed_snapshot=DEPLOYED_SNAPSHOT,
    )

    result = await router_client.fetch_model_metadata(MODEL_ID, "acme", "jwt")
    names = _field_names(result[1])

    assert captured["version_id"] is None, "no version fetch for undeployed model"
    assert names == {"Net Revenue", "Cost", "Profit", "Region", "Product", "Channel"}
