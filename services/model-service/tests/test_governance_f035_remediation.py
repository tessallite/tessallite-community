"""F-035 governance-integration remediation regressions.

Covers the export/preview/mapper/sync-history defect fixes (LIVE push stays
deferred, Bug-6038):

  - F-035-01: KPI and calculated-measure expression dependencies are exported
    as lineage edges; unresolvable expressions raise governance warnings.
  - F-035-02: project/model external IDs are UUID-backed and survive a rename.
  - F-035-03: deleting the last object of a category still deprecates its
    stranded mappings, and remote deprecation is invoked with stored remote ids.
  - F-035-06: sync-history rows record the exported snapshot identity.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.model_snapshot.governance_graph import (
    GovernanceEdge,
    GovernanceGraph,
    GovernanceNode,
    GovernanceSnapshotIdentity,
)
from src.governance_exporter import build_governance_graph
from src.governance_helpers import (
    deprecation_scope_node_types,
    is_in_deprecation_scope,
    validate_governance_graph,
)
from src.solidatus_client import SolidatusUpsertResult
from src.solidatus_sync import run_solidatus_sync

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    NOW,
    make_mock_db,
    make_model,
    make_project,
)


# ---------------------------------------------------------------------------
# Snapshot fixture with expression-defined KPI + calculated measure
# ---------------------------------------------------------------------------

def _expr_snapshot() -> tuple[dict, dict[str, str]]:
    ids = {
        "source": str(uuid.uuid4()),
        "table": str(uuid.uuid4()),
        "amount_col": str(uuid.uuid4()),
        "cost_col": str(uuid.uuid4()),
        "revenue": str(uuid.uuid4()),          # standard measure
        "cost": str(uuid.uuid4()),             # standard measure
        "gross_margin_pct": str(uuid.uuid4()), # calculated measure
        "expr_kpi": str(uuid.uuid4()),         # expression-only KPI
        "unresolved_kpi": str(uuid.uuid4()),   # references a missing measure
    }
    snap = {
        "model": {
            "id": str(TEST_MODEL_ID),
            "slug": "sales",
            "display_name": "Sales Model",
            "status": "active",
        },
        "data_sources": [
            {"id": ids["source"], "name": "Source", "source_type": "postgres"},
        ],
        "tables": [
            {"id": ids["table"], "data_source_id": ids["source"],
             "physical_name": "orders"},
        ],
        "columns": [
            {"id": ids["amount_col"], "model_table_id": ids["table"],
             "column_name": "amount", "data_type": "numeric", "is_hidden": False},
            {"id": ids["cost_col"], "model_table_id": ids["table"],
             "column_name": "cost", "data_type": "numeric", "is_hidden": False},
        ],
        "dimensions": [],
        "measures": [
            {"id": ids["revenue"], "name": "Revenue", "display_name": "Revenue",
             "measure_type": "standard", "source_column_id": ids["amount_col"],
             "is_hidden": False},
            {"id": ids["cost"], "name": "Cost", "display_name": "Cost",
             "measure_type": "standard", "source_column_id": ids["cost_col"],
             "is_hidden": False},
            # Calculated measure references two standard measures BY NAME only.
            {"id": ids["gross_margin_pct"], "name": "Gross Margin Pct",
             "display_name": "Gross Margin Pct", "measure_type": "calculated",
             "expression": 'measure("Revenue") - measure("Cost")',
             "is_hidden": False},
        ],
        "kpis": [
            # Expression-only KPI: no value/goal/target FK, deps live in expr.
            {"id": ids["expr_kpi"], "name": "Chargeback Rate",
             "display_name": "Chargeback Rate",
             "expression": 'safe_div(measure("Cost"), measure("Revenue"))',
             "certification_status": "certified",
             "owner_user_id": "owner@example.com"},
            # KPI whose expression references a measure not in the model.
            {"id": ids["unresolved_kpi"], "name": "Broken KPI",
             "display_name": "Broken KPI",
             "expression": 'measure("Nonexistent")',
             "certification_status": "draft",
             "owner_user_id": "owner@example.com"},
        ],
        "glossary_entries": [],
        "data_tags": [],
        "lineage_mappings": [],
    }
    return snap, ids


def _db_for_exporter(model, project):
    db = make_mock_db()

    async def _get(cls, pk):
        if cls.__name__ == "Model":
            return model
        if cls.__name__ == "Project":
            return project
        return None

    db.get = _get
    return db


# ---------------------------------------------------------------------------
# F-035-01 — expression lineage
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_calculated_measure_expression_edges_exported():
    """A calculated measure's measure() references must appear as derived_from
    edges even though they exist only in the expression string."""
    model = make_model()
    project = make_project()
    snap, ids = _expr_snapshot()
    db = _db_for_exporter(model, project)

    with patch("src.governance_exporter.consistent_snapshot", AsyncMock(return_value=snap)):
        graph = await build_governance_graph(
            db, project_id=TEST_PROJECT_ID, model_id=TEST_MODEL_ID,
            include_downstream_assets=False, export_draft=True,
        )

    edges = {
        (e.source_key, e.target_key, e.relationship_type) for e in graph.edges
    }
    calc_key = f"measure.{ids['gross_margin_pct']}"
    assert (calc_key, f"measure.{ids['revenue']}", "derived_from") in edges
    assert (calc_key, f"measure.{ids['cost']}", "derived_from") in edges


@pytest.mark.anyio
async def test_kpi_expression_measure_edges_exported():
    """An expression-only KPI must link to the measures its expression uses,
    with no stored value/goal/target FK present."""
    model = make_model()
    project = make_project()
    snap, ids = _expr_snapshot()
    db = _db_for_exporter(model, project)

    with patch("src.governance_exporter.consistent_snapshot", AsyncMock(return_value=snap)):
        graph = await build_governance_graph(
            db, project_id=TEST_PROJECT_ID, model_id=TEST_MODEL_ID,
            include_downstream_assets=False, export_draft=True,
        )

    edges = {
        (e.source_key, e.target_key, e.relationship_type) for e in graph.edges
    }
    kpi_key = f"kpi.{ids['expr_kpi']}"
    assert (kpi_key, f"measure.{ids['revenue']}", "uses_measure") in edges
    assert (kpi_key, f"measure.{ids['cost']}", "uses_measure") in edges


@pytest.mark.anyio
async def test_unresolved_kpi_expression_raises_warning():
    """A KPI expression referencing a missing measure must produce an explicit
    governance warning, not silently drop the dependency."""
    model = make_model()
    project = make_project()
    snap, ids = _expr_snapshot()
    db = _db_for_exporter(model, project)

    with patch("src.governance_exporter.consistent_snapshot", AsyncMock(return_value=snap)):
        graph = await build_governance_graph(
            db, project_id=TEST_PROJECT_ID, model_id=TEST_MODEL_ID,
            include_downstream_assets=False, export_draft=True,
        )

    codes = {w.get("code") for w in graph.export_warnings}
    assert "KPI_EXPRESSION_UNRESOLVED" in codes
    # And validate_governance_graph surfaces them alongside structural warnings.
    all_codes = {w.get("code") for w in validate_governance_graph(graph)}
    assert "KPI_EXPRESSION_UNRESOLVED" in all_codes


@pytest.mark.anyio
async def test_hidden_measure_dependency_raises_excluded_warning():
    """When a visible calculated measure references a hidden measure via
    expression, and include_hidden_objects=False, the edge must NOT be
    silently dropped; instead a DEPENDENCY_EXCLUDED warning must fire
    (Fable-R3)."""
    snap, ids = _expr_snapshot()
    # Make Cost hidden so it's excluded from the graph.
    snap = {**snap}
    snap["measures"] = [
        m if m["name"] != "Cost"
        else {**m, "is_hidden": True}
        for m in snap["measures"]
    ]
    model = make_model()
    project = make_project()
    db = _db_for_exporter(model, project)

    with patch("src.governance_exporter.consistent_snapshot", AsyncMock(return_value=snap)):
        graph = await build_governance_graph(
            db, project_id=TEST_PROJECT_ID, model_id=TEST_MODEL_ID,
            include_downstream_assets=False, export_draft=True,
            include_hidden_objects=False,
        )

    # The edge to the hidden Cost measure must NOT exist.
    edges = {
        (e.source_key, e.target_key, e.relationship_type) for e in graph.edges
    }
    calc_key = f"measure.{ids['gross_margin_pct']}"
    assert (calc_key, f"measure.{ids['cost']}", "derived_from") not in edges
    # But a DEPENDENCY_EXCLUDED warning must have fired.
    codes = {w.get("code") for w in graph.export_warnings}
    assert "DEPENDENCY_EXCLUDED" in codes


# ---------------------------------------------------------------------------
# F-035-02 — UUID-backed external identity survives rename
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_project_model_keys_are_uuid_backed_and_survive_rename():
    """Renaming the project/model slug must not change the domain/model node
    external ids or the contains-edge key."""
    snap, _ = _expr_snapshot()

    async def _build(project_slug, model_slug, model_slug_in_snap):
        model = make_model(slug=model_slug)
        project = make_project(slug=project_slug)
        s = {**snap, "model": {**snap["model"], "slug": model_slug_in_snap}}
        db = _db_for_exporter(model, project)
        with patch("src.governance_exporter.consistent_snapshot", AsyncMock(return_value=s)):
            return await build_governance_graph(
                db, project_id=TEST_PROJECT_ID, model_id=TEST_MODEL_ID,
                project_slug=project_slug, model_slug=model_slug,
                include_downstream_assets=False, export_draft=True,
            )

    before = await _build("sales-team", "quarterly", "quarterly")
    after = await _build("renamed-team", "annual", "annual")

    def _keys(graph, obj_type):
        return {n.stable_key for n in graph.nodes if n.object_type == obj_type}

    assert _keys(before, "domain") == _keys(after, "domain")
    assert _keys(before, "semantic_model") == _keys(after, "semantic_model")
    # The keys are UUID-backed, not slug-backed.
    assert _keys(before, "domain") == {f"domain.{TEST_PROJECT_ID}"}
    assert _keys(before, "semantic_model") == {f"model.{TEST_MODEL_ID}"}
    # The contains edge key is stable across the rename too.
    before_contains = {e.stable_key for e in before.edges if e.relationship_type == "contains"}
    after_contains = {e.stable_key for e in after.edges if e.relationship_type == "contains"}
    assert before_contains == after_contains


# ---------------------------------------------------------------------------
# F-035-06 — snapshot identity recorded
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_graph_carries_snapshot_identity():
    model = make_model()
    project = make_project()
    snap, _ = _expr_snapshot()
    db = _db_for_exporter(model, project)

    with patch("src.governance_exporter.consistent_snapshot", AsyncMock(return_value=snap)):
        graph = await build_governance_graph(
            db, project_id=TEST_PROJECT_ID, model_id=TEST_MODEL_ID,
            include_downstream_assets=False, export_draft=True,
        )

    assert graph.snapshot is not None
    assert graph.snapshot.export_draft is True
    assert len(graph.snapshot.content_hash) == 64  # sha-256 hex


# ---------------------------------------------------------------------------
# F-035-03 — deprecation scope from flags + remote deprecation
# ---------------------------------------------------------------------------

def test_deprecation_scope_from_flags():
    scope = deprecation_scope_node_types(
        include_technical=True, include_aggregates=True, include_glossary=False,
        include_security_tags=True, include_downstream_assets=False,
    )
    assert "measure" in scope and "column" in scope and "aggregate" in scope
    assert "data_tag" in scope
    assert "glossary_term" not in scope
    assert "downstream_asset" not in scope
    # An edge/relationship type (not a node type) is always in scope.
    assert is_in_deprecation_scope("uses", scope) is True
    # An excluded node category is out of scope.
    assert is_in_deprecation_scope("glossary_term", scope) is False


def _solidatus_conn():
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        project_id=TEST_PROJECT_ID,
        model_id=TEST_MODEL_ID,
        base_url="https://solidatus.example.com",
        encrypted_credentials=b"enc",
        workspace_id="ws-1",
        model_ref="model-ref-1",
    )


async def _refresh_defaults(obj):
    if getattr(obj, "id", None) is None:
        obj.id = uuid.uuid4()
    if getattr(obj, "started_at", None) is None:
        obj.started_at = NOW


@pytest.mark.anyio
async def test_delete_last_of_category_still_deprecates_remotely():
    """When the last object of an in-scope category is deleted (payload has no
    node of that type), its stranded mapping must still be deprecated and the
    remote deprecation call must fire with the stored remote id (F-035-03)."""
    conn = _solidatus_conn()
    db = make_mock_db()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock(side_effect=_refresh_defaults)

    async def _get(_cls, pk):
        return conn if pk == conn.id else None
    db.get = _get

    # Graph exports only a domain node — the measure category is now empty.
    graph = GovernanceGraph(
        nodes=[
            GovernanceNode(stable_key=f"domain.{TEST_PROJECT_ID}",
                           object_type="domain", object_id="d1", label="Sales"),
        ],
        edges=[],
        snapshot=GovernanceSnapshotIdentity(
            deployed_version_id=str(uuid.uuid4()),
            export_draft=False,
            content_hash="a" * 64,
        ),
    )

    # An existing mapping for a measure that no longer appears in the payload.
    stranded = types.SimpleNamespace(
        tessallite_object_type="measure",
        tessallite_object_id="m-gone",
        tessallite_stable_key="measure.m-gone",
        last_payload_hash="old",
        is_deprecated=False,
        last_synced_at=NOW,
        last_sync_run_id=None,
        solidatus_object_id="remote-m-gone",
        solidatus_object_ref=None,
    )
    existing = {("measure", "m-gone"): stranded}

    deprecate_calls: list[list[str]] = []

    class FakeClient:
        def __init__(self, *_a, **_k):
            pass
        async def upsert_nodes(self, nodes, workspace_id=""):
            return SolidatusUpsertResult(created=0, updated=len(nodes))
        async def upsert_edges(self, edges, workspace_id=""):
            return SolidatusUpsertResult(created=0, updated=len(edges))
        async def deprecate_nodes(self, external_ids):
            deprecate_calls.append(list(external_ids))
            return len(external_ids)
        async def deprecate_edges(self, external_ids):
            return len(external_ids)

    with (
        patch("src.solidatus_sync.build_governance_graph", AsyncMock(return_value=graph)),
        patch("src.solidatus_sync.decrypt_token", return_value="tok"),
        patch("src.solidatus_sync.SolidatusClient", FakeClient),
        patch("src.solidatus_sync.load_mappings", AsyncMock(return_value=existing)),
    ):
        run = await run_solidatus_sync(
            db, connection_id=conn.id, project_id=TEST_PROJECT_ID,
            model_id=TEST_MODEL_ID, dry_run=False, deprecate_missing=True,
            include_technical=True,
        )

    assert run.status == "succeeded"
    # Measure is in scope (include_technical -> business assets always on), so
    # even though the payload has no measure node, the stranded mapping is
    # deprecated and the remote call fired with its stored remote id.
    assert stranded.is_deprecated is True
    assert deprecate_calls == [["remote-m-gone"]]
    # Snapshot identity was recorded on the run (F-035-06).
    assert run.tessallite_snapshot_hash is not None
    assert run.solidatus_target_ref == "model-ref-1"
