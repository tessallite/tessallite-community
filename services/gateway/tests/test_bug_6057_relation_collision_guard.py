"""Bug-6057 / F-001-20 — the $KPIs, _technical, and Looker adapter relations
must pass through the same F-008-15 ``_register_relation`` collision guard as
the base relations.

Before the fix these three relation types were written straight into the
catalogue maps (``table_model_id`` / ``table_query_name`` / ...). When another
model's base slug collided with a generated name (e.g. a model literally named
``sales$KPIs`` vs model ``sales``'s KPI virtual table), the second writer
silently overwrote the first and a JDBC client could be served the wrong
model's catalogue. The guard now project-prefixes the colliding relation so
both stay distinct and correctly owned (fail closed, never silent overwrite).
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import router_client


MODEL_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
MODEL_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
PERSONA_AUDIT = "dddddddd-dddd-dddd-dddd-dddddddddddd"


def _patch_models(monkeypatch, model_a, model_b, *, kpis_a=None,
                   personas_a=None, snapshot_a=None):
    async def _list_models(tenant_slug, jwt_token):
        return [model_a, model_b]

    async def _measures(mid, *a, **kw):
        return []

    async def _dimensions(mid, *a, **kw):
        return []

    async def _personas(mid, *a, **kw):
        if mid == MODEL_A:
            return personas_a or []
        return []

    async def _snapshot(mid, *a, **kw):
        if mid == MODEL_A:
            return snapshot_a or {"columns": [], "tables": []}
        return {"columns": [], "tables": []}

    async def _kpis(mid, *a, **kw):
        if mid == MODEL_A:
            return kpis_a or []
        return []

    monkeypatch.setattr(router_client, "list_all_models_for_tenant", _list_models)
    monkeypatch.setattr(router_client, "get_model_measures", _measures)
    monkeypatch.setattr(router_client, "get_model_dimensions", _dimensions)
    monkeypatch.setattr(router_client, "get_model_personas", _personas)
    monkeypatch.setattr(router_client, "get_model_snapshot", _snapshot)
    monkeypatch.setattr(router_client, "get_model_kpis", _kpis)


def _owner_disjoint(table_model_id):
    a = {n for n, mid in table_model_id.items() if mid == MODEL_A}
    b = {n for n, mid in table_model_id.items() if mid == MODEL_B}
    assert a, "model A relations missing"
    assert b, "model B relations missing"
    assert a.isdisjoint(b), "a relation resolved to BOTH models (silent overwrite)"
    return a, b


async def test_kpi_virtual_table_collision_is_guarded(monkeypatch):
    # Model A (slug ``sales``) with a KPI publishes ``sales$KPIs``. Model B's
    # base slug is literally ``sales$KPIs`` — the two names collide.
    model_a = {
        "id": MODEL_A, "project_id": "p", "project_slug": "alpha",
        "slug": "sales", "deployed_version_id": None,
    }
    model_b = {
        "id": MODEL_B, "project_id": "p", "project_slug": "beta",
        "slug": "sales$KPIs", "deployed_version_id": None,
    }
    _patch_models(
        monkeypatch, model_a, model_b,
        kpis_a=[{"id": "k1", "name": "Revenue"}],
    )

    result = await router_client.fetch_model_metadata(None, "acme", "jwt")
    model_names, table_model_id = result[0], result[2]
    table_query_name = result[7]

    a_rel, b_rel = _owner_disjoint(table_model_id)

    # The KPI virtual table (query_name preserves the ``$KPIs`` marker) is owned
    # by A and remains discoverable — its suffix survived the guard rewrite.
    kpi_rel = {
        n for n in a_rel
        if str(table_query_name.get(n, "")).endswith("$KPIs")
    }
    assert kpi_rel, "A's $KPIs virtual table was lost after collision"

    # Both the bare and a project-prefixed variant exist; B's base stays owned
    # by B (never overwritten by A's KPI relation).
    assert "sales$KPIs" in model_names
    assert {"alpha__sales$KPIs", "beta__sales$KPIs"} & set(model_names), (
        "the colliding $KPIs relation was not project-prefixed"
    )


async def test_technical_relation_collision_is_guarded(monkeypatch):
    # Model A (slug ``orders``) auto-injects ``orders_technical``. Model B's
    # base slug is literally ``orders_technical``.
    fact_id = "11111111-1111-1111-1111-111111111111"
    model_a = {
        "id": MODEL_A, "project_id": "p", "project_slug": "alpha",
        "slug": "orders", "deployed_version_id": None,
    }
    model_b = {
        "id": MODEL_B, "project_id": "p", "project_slug": "beta",
        "slug": "orders_technical", "deployed_version_id": None,
    }
    # A persona with includes_hidden_columns authorises the technical surface
    # (its slug is NOT "technical", so the auto-injection path fires).
    personas_a = [{
        "id": PERSONA_AUDIT, "slug": "auditor", "name": "Auditor",
        "included_measure_ids": [], "included_dimension_ids": [],
        "includes_hidden_columns": True, "restricted_column_ids": [],
    }]
    snapshot_a = {
        "tables": [{
            "id": fact_id, "table_type": "fact",
            "alias": "orders", "physical_name": "orders_fact",
            "row_count_estimate": 1000,
        }],
        "columns": [
            {"id": "c1", "model_table_id": fact_id, "column_name": "order_id",
             "data_type": "int", "is_nullable": False, "is_primary_key": True},
            {"id": "c2", "model_table_id": fact_id, "column_name": "amount",
             "data_type": "numeric", "is_nullable": True},
        ],
    }
    _patch_models(
        monkeypatch, model_a, model_b,
        personas_a=personas_a, snapshot_a=snapshot_a,
    )

    result = await router_client.fetch_model_metadata(None, "acme", "jwt")
    model_names, table_model_id = result[0], result[2]
    table_include_hidden = result[6]

    a_rel, b_rel = _owner_disjoint(table_model_id)

    # A's technical relation (physical-column surface, include_hidden=True) is
    # still present and owned by A.
    a_tech = {n for n in a_rel if table_include_hidden.get(n)}
    assert a_tech, "A's _technical relation was lost after collision"

    assert "orders_technical" in model_names
    assert {"alpha__orders_technical", "beta__orders_technical"} & set(model_names), (
        "the colliding _technical relation was not project-prefixed"
    )


class _LookerOn:
    """Enable the Looker gateway without mutating the shared pydantic settings
    instance: proxy every attribute except LOOKER_GATEWAY_ENABLED."""

    def __init__(self, base):
        object.__setattr__(self, "_base", base)

    def __getattr__(self, name):
        if name == "LOOKER_GATEWAY_ENABLED":
            return True
        return getattr(self._base, name)


async def test_looker_relation_collision_is_guarded(monkeypatch):
    # Model A (slug ``orders``) with a table alias ``fact`` publishes the Looker
    # adapter relation ``orders__fact``. Model B's base slug is literally
    # ``orders__fact``. Requires the Looker gateway to be enabled.
    fact_id = "22222222-2222-2222-2222-222222222222"
    model_a = {
        "id": MODEL_A, "project_id": "p", "project_slug": "alpha",
        "slug": "orders", "deployed_version_id": None,
    }
    model_b = {
        "id": MODEL_B, "project_id": "p", "project_slug": "beta",
        "slug": "orders__fact", "deployed_version_id": None,
    }
    snapshot_a = {
        "tables": [{
            "id": fact_id, "table_type": "fact",
            "alias": "fact", "physical_name": "orders_fact",
            "row_count_estimate": 500,
        }],
        "columns": [],
    }
    _patch_models(monkeypatch, model_a, model_b, snapshot_a=snapshot_a)

    monkeypatch.setattr(router_client, "settings", _LookerOn(router_client.settings))

    result = await router_client.fetch_model_metadata(None, "acme", "jwt")
    model_names, table_model_id = result[0], result[2]
    looker_relations = result[10]

    a_rel, b_rel = _owner_disjoint(table_model_id)

    assert "orders__fact" in model_names
    assert {"alpha__orders__fact", "beta__orders__fact"} & set(model_names), (
        "the colliding Looker relation was not project-prefixed"
    )
    # The guarded name (whichever won the bare slot) is the one exposed as a
    # Looker adapter relation, so exposure/FK wiring stays consistent.
    exposed = looker_relations & set(model_names)
    assert exposed, "no Looker relation was registered as an exposed adapter name"


async def test_intra_model_semantic_table_collision_is_guarded(monkeypatch):
    """Bug-6772: two semantic tables WITHIN ONE MODEL whose aliases normalise to
    the same relation name must NOT silently overwrite each other. ``Order-Items``
    and ``Order_Items`` both normalise to ``order_items`` via
    ``_relation_identifier``; with the old model-wide owner key (``<mid>:base``)
    the guard saw ``prior == owner_key`` and reused the name, so the second
    table's columns / FK wiring overwrote the first. The per-surface owner key
    now detects the collision and exposes both as distinct relations, each owning
    its OWN columns.
    """
    tbl_dash = "33333333-3333-3333-3333-333333333331"   # alias "Order-Items"
    tbl_underscore = "33333333-3333-3333-3333-333333333332"  # alias "Order_Items"
    model_a = {
        "id": MODEL_A, "project_id": "p", "project_slug": "alpha",
        "slug": "orders", "deployed_version_id": None,
    }
    model_b = {
        "id": MODEL_B, "project_id": "p", "project_slug": "beta",
        "slug": "unrelated", "deployed_version_id": None,
    }
    snapshot_a = {
        "tables": [
            {"id": tbl_dash, "table_type": "fact",
             "alias": "Order-Items", "physical_name": "order_items_dash",
             "row_count_estimate": 10},
            {"id": tbl_underscore, "table_type": "dim",
             "alias": "Order_Items", "physical_name": "order_items_us",
             "row_count_estimate": 20},
        ],
        # A dimension in each table so the two relations carry DISTINCT columns —
        # an overwrite would collapse them to one column set.
        "columns": [
            {"id": "cd", "model_table_id": tbl_dash, "column_name": "dash_col",
             "data_type": "text", "is_nullable": True},
            {"id": "cu", "model_table_id": tbl_underscore, "column_name": "us_col",
             "data_type": "text", "is_nullable": True},
        ],
    }

    # Dimensions scoped to each table (the Looker relation columns come from
    # dimensions/measures resolved via _in_table -> source_column_id).
    async def _dimensions(mid, *a, **kw):
        if mid == MODEL_A:
            return [
                {"id": "dimd", "name": "DashDim", "source_column_id": "cd",
                 "data_type": "text"},
                {"id": "dimu", "name": "UsDim", "source_column_id": "cu",
                 "data_type": "text"},
            ]
        return []

    _patch_models(monkeypatch, model_a, model_b, snapshot_a=snapshot_a)
    monkeypatch.setattr(router_client, "get_model_dimensions", _dimensions)
    monkeypatch.setattr(router_client, "settings", _LookerOn(router_client.settings))

    result = await router_client.fetch_model_metadata(None, "acme", "jwt")
    model_names, table_columns, table_model_id = result[0], result[1], result[2]
    looker_relations = result[10]

    # Both Looker relations for the two tables must be present as DISTINCT names
    # (one bare, one project-prefixed) — never collapsed to a single entry.
    looker_a = looker_relations & set(model_names)
    assert "orders__order_items" in looker_a, (
        "the first semantic-table relation was lost"
    )
    prefixed = {n for n in looker_a if n.startswith("alpha__orders__order_items")}
    assert prefixed, (
        "the intra-model colliding semantic-table relation was silently "
        f"overwritten instead of guarded; looker relations = {sorted(looker_a)}"
    )
    assert len(looker_a) >= 2, (
        f"both semantic tables must expose a relation; got {sorted(looker_a)}"
    )

    # Each distinct relation owns its OWN column, proving no overwrite occurred.
    col_names = {
        rel: {c.get("name") for c in table_columns.get(rel, [])}
        for rel in looker_a
    }
    all_cols = set().union(*col_names.values()) if col_names else set()
    assert {"DashDim", "UsDim"} <= all_cols, (
        f"a table's columns were lost to an overwrite; saw {col_names}"
    )
    # Both relations are owned by model A (never cross-attributed).
    for rel in looker_a:
        assert table_model_id.get(rel) == MODEL_A
