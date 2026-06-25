"""Unit tests for the shared governance exporter and export-preview/sync endpoints.

Tests the GovernanceGraph builder and the export-preview + sync API endpoints
for both Solidatus and Collibra routers.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.governance_exporter import build_governance_graph
from shared.db.models import LineageMapping
from shared.model_snapshot.governance_graph import GovernanceEdge, GovernanceGraph, GovernanceNode
from shared.model_snapshot.serialiser import _row_to_dict

from .conftest import (
    TEST_PROJECT_ID,
    TEST_MODEL_ID,
    NOW,
    async_gen_from,
    make_mock_db,
    make_model,
    make_project,
)

SOLIDATUS_PREFIX = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/solidatus"
)
COLLIBRA_PREFIX = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/collibra"
)


def _make_sample_graph() -> GovernanceGraph:
    """Return a small synthetic graph for preview/sync tests."""
    return GovernanceGraph(
        nodes=[
            GovernanceNode(
                stable_key="acme-demo",
                object_type="domain",
                object_id=str(uuid.uuid4()),
                label="Acme Demo",
            ),
            GovernanceNode(
                stable_key="acme-demo.sales",
                object_type="semantic_model",
                object_id=str(uuid.uuid4()),
                label="Sales Analytics",
                status="active",
            ),
            GovernanceNode(
                stable_key="measure.gross_revenue",
                object_type="measure",
                object_id=str(uuid.uuid4()),
                label="Gross Revenue",
                owner="owner@example.com",
            ),
        ],
        edges=[
            GovernanceEdge(
                stable_key="acme-demo.contains.acme-demo.sales",
                source_key="acme-demo",
                target_key="acme-demo.sales",
                relationship_type="contains",
            ),
        ],
    )


def _make_db_for_preview(model, project, conn=None):
    """Build a mock DB that works for export-preview endpoints."""
    db = make_mock_db()

    async def _get(cls, pk):
        if pk == TEST_MODEL_ID:
            return model
        if conn is not None and pk == conn.id:
            return conn
        return None

    db.get = _get
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = project
    db.execute = AsyncMock(return_value=exec_result)

    return db


async def _fake_sync_refresh(obj):
    """Populate ORM defaults for sync run objects."""
    if obj.id is None:
        object.__setattr__(obj, "id", uuid.uuid4())
    if getattr(obj, "status", None) is None:
        object.__setattr__(obj, "status", "running")


def _make_db_for_sync(model, project, conn):
    """Build a mock DB that works for sync endpoints."""
    db = make_mock_db()
    db.add = MagicMock()
    db.refresh = AsyncMock(side_effect=_fake_sync_refresh)
    db.commit = AsyncMock()

    async def _get(cls, pk):
        if pk == TEST_MODEL_ID:
            return model
        if pk == conn.id:
            return conn
        return project

    db.get = _get
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = project
    db.execute = AsyncMock(return_value=exec_result)

    return db


def _make_solidatus_conn(conn_id=None):
    return types.SimpleNamespace(
        id=conn_id or uuid.uuid4(),
        project_id=TEST_PROJECT_ID,
        model_id=TEST_MODEL_ID,
        display_name="Solidatus Dev",
        base_url="https://solidatus.example.com",
        auth_type="bearer_token",
        encrypted_credentials=b"enc",
        workspace_id="ws-123",
        model_ref="tessallite-sales",
        sync_scope="model",
        is_active=True,
        created_at=NOW,
        updated_at=NOW,
    )


def _make_collibra_conn(conn_id=None):
    return types.SimpleNamespace(
        id=conn_id or uuid.uuid4(),
        project_id=TEST_PROJECT_ID,
        model_id=TEST_MODEL_ID,
        display_name="Collibra Prod",
        base_url="https://company.collibra.com",
        auth_type="bearer_token",
        encrypted_credentials=b"enc",
        community_id="community-123",
        domain_id="domain-456",
        asset_type_mapping={},
        relation_type_mapping={},
        responsibility_mapping={},
        sync_scope="model",
        sync_mode="rest_api",
        is_active=True,
        created_at=NOW,
        updated_at=NOW,
    )


def _make_exporter_snapshot() -> tuple[dict, dict[str, str]]:
    ids = {
        "source": str(uuid.uuid4()),
        "table": str(uuid.uuid4()),
        "visible_col": str(uuid.uuid4()),
        "hidden_col": str(uuid.uuid4()),
        "visible_dim": str(uuid.uuid4()),
        "hidden_dim": str(uuid.uuid4()),
        "visible_measure": str(uuid.uuid4()),
        "hidden_measure": str(uuid.uuid4()),
        "kpi": str(uuid.uuid4()),
        "glossary": str(uuid.uuid4()),
        "tag": str(uuid.uuid4()),
    }
    visible_lineage = LineageMapping(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        semantic_field_name="gross_revenue",
        semantic_field_type="measure",
        aggregate_col_id=None,
        source_column_id=uuid.UUID(ids["visible_col"]),
    )
    hidden_lineage = LineageMapping(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        semantic_field_name="internal_margin",
        semantic_field_type="measure",
        aggregate_col_id=None,
        source_column_id=uuid.UUID(ids["hidden_col"]),
    )
    return {
        "model": {
            "id": str(TEST_MODEL_ID),
            "slug": "sales",
            "display_name": "Sales Model",
            "status": "active",
        },
        "data_sources": [
            {"id": ids["source"], "name": "Warehouse", "source_type": "postgres"},
        ],
        "tables": [
            {
                "id": ids["table"],
                "data_source_id": ids["source"],
                "physical_name": "orders",
            },
        ],
        "columns": [
            {
                "id": ids["visible_col"],
                "model_table_id": ids["table"],
                "column_name": "revenue",
                "data_type": "numeric",
                "is_hidden": False,
            },
            {
                "id": ids["hidden_col"],
                "model_table_id": ids["table"],
                "column_name": "internal_margin",
                "data_type": "numeric",
                "is_hidden": True,
            },
        ],
        "dimensions": [
            {
                "id": ids["visible_dim"],
                "name": "Revenue Band",
                "display_name": "Revenue Band",
                "source_column_id": ids["visible_col"],
                "is_hidden": False,
            },
            {
                "id": ids["hidden_dim"],
                "name": "Internal Segment",
                "display_name": "Internal Segment",
                "source_column_id": ids["hidden_col"],
                "is_hidden": True,
            },
        ],
        "measures": [
            {
                "id": ids["visible_measure"],
                "name": "gross_revenue",
                "display_name": "Gross Revenue",
                "source_column_id": ids["visible_col"],
                "is_hidden": False,
            },
            {
                "id": ids["hidden_measure"],
                "name": "internal_margin",
                "display_name": "Internal Margin",
                "source_column_id": ids["hidden_col"],
                "is_hidden": True,
            },
        ],
        "kpis": [
            {
                "id": ids["kpi"],
                "name": "revenue_attainment",
                "display_name": "Revenue Attainment",
                "target_measure_id": ids["visible_measure"],
                "business_definition": {
                    "formula": {"measure_id": ids["visible_measure"]},
                },
                "certification_status": "certified",
                "owner_user_id": "owner@example.com",
            },
        ],
        "glossary_entries": [
            {
                "id": ids["glossary"],
                "term": "Revenue",
                "definition": "Recognized revenue.",
                "status": "accepted",
                "synonyms": [],
                "attachments": [
                    {
                        "target_type": "measure",
                        "target_id": ids["visible_measure"],
                    },
                    {
                        "target_type": "measure",
                        "target_id": ids["hidden_measure"],
                    },
                ],
            },
        ],
        "data_tags": [
            {
                "id": ids["tag"],
                "name": "Sensitive",
                "tag_type": "security",
                "column_ids": [ids["visible_col"], ids["hidden_col"]],
            },
        ],
        "lineage_mappings": [
            _row_to_dict(visible_lineage),
            _row_to_dict(hidden_lineage),
        ],
    }, ids


def _make_db_for_exporter(model, project, version=None):
    db = make_mock_db()

    async def _get(cls, pk):
        if cls.__name__ == "Model":
            return model
        if cls.__name__ == "Project":
            return project
        if cls.__name__ == "ModelVersion":
            return version
        return None

    db.get = _get
    return db


class TestBuildGovernanceGraph:
    @pytest.mark.anyio
    async def test_uses_deployed_snapshot_by_default(self):
        deployed_version_id = uuid.uuid4()
        model = make_model()
        model.deployed_version_id = deployed_version_id
        project = make_project()
        snap, ids = _make_exporter_snapshot()
        version = types.SimpleNamespace(
            id=deployed_version_id,
            model_id=TEST_MODEL_ID,
            snapshot_json=snap,
        )
        db = _make_db_for_exporter(model, project, version)

        with patch("src.governance_exporter.snapshot_model", AsyncMock()) as draft_snapshot:
            graph = await build_governance_graph(
                db,
                project_id=TEST_PROJECT_ID,
                model_id=TEST_MODEL_ID,
                project_slug=project.slug,
                model_slug=model.slug,
                include_downstream_assets=False,
                export_draft=False,
            )

        draft_snapshot.assert_not_called()
        assert any(n.object_id == ids["visible_measure"] for n in graph.nodes)

    @pytest.mark.anyio
    async def test_hidden_objects_lineage_glossary_and_kpi_edges(self):
        model = make_model()
        project = make_project()
        snap, ids = _make_exporter_snapshot()
        db = _make_db_for_exporter(model, project)

        with patch("src.governance_exporter.snapshot_model", AsyncMock(return_value=snap)):
            graph = await build_governance_graph(
                db,
                project_id=TEST_PROJECT_ID,
                model_id=TEST_MODEL_ID,
                project_slug=project.slug,
                model_slug=model.slug,
                include_downstream_assets=False,
                include_hidden_objects=False,
                export_draft=True,
            )

        node_keys = {node.stable_key for node in graph.nodes}
        object_ids = {node.object_id for node in graph.nodes}
        edge_keys = {
            (edge.source_key, edge.target_key, edge.relationship_type)
            for edge in graph.edges
        }
        assert ids["hidden_col"] not in object_ids
        assert ids["hidden_dim"] not in object_ids
        assert ids["hidden_measure"] not in object_ids
        assert (
            f"column.{ids['visible_col']}",
            f"measure.{ids['visible_measure']}",
            "feeds_semantic_field",
        ) in edge_keys
        assert (
            f"column.{ids['hidden_col']}",
            f"measure.{ids['hidden_measure']}",
            "feeds_semantic_field",
        ) not in edge_keys
        assert all(
            edge.source_key in node_keys and edge.target_key in node_keys
            for edge in graph.edges
        )
        assert any(
            edge.relationship_type == "governed_by_term"
            and edge.source_key == f"measure.{ids['visible_measure']}"
            for edge in graph.edges
        )
        assert any(
            edge.relationship_type == "uses_measure"
            and edge.source_key == f"kpi.{ids['kpi']}"
            and edge.target_key == f"measure.{ids['visible_measure']}"
            for edge in graph.edges
        )

    @pytest.mark.anyio
    async def test_can_exclude_business_assets(self):
        model = make_model()
        project = make_project()
        snap, _ = _make_exporter_snapshot()
        db = _make_db_for_exporter(model, project)

        with patch("src.governance_exporter.snapshot_model", AsyncMock(return_value=snap)):
            graph = await build_governance_graph(
                db,
                project_id=TEST_PROJECT_ID,
                model_id=TEST_MODEL_ID,
                project_slug=project.slug,
                model_slug=model.slug,
                include_business_assets=False,
                include_downstream_assets=False,
                export_draft=True,
            )

        node_types = {node.object_type for node in graph.nodes}
        assert "measure" not in node_types
        assert "dimension" not in node_types
        assert "kpi" not in node_types
        assert "glossary_term" not in node_types

    @pytest.mark.anyio
    async def test_owner_populated_only_on_kpi_and_downstream_nodes(self):
        """Regression for F-CSI-05.

        The real exporter sets ``owner`` only on KPI and downstream-asset
        nodes (the two object types that carry an explicit owner column).
        Measures and dimensions have no owner column, so they must never
        receive an owner — otherwise the Collibra mapper would emit
        responsibilities the system cannot actually produce.
        """
        model = make_model()
        project = make_project()
        snap, ids = _make_exporter_snapshot()
        db = _make_db_for_exporter(model, project)

        with patch("src.governance_exporter.snapshot_model", AsyncMock(return_value=snap)):
            graph = await build_governance_graph(
                db,
                project_id=TEST_PROJECT_ID,
                model_id=TEST_MODEL_ID,
                project_slug=project.slug,
                model_slug=model.slug,
                include_downstream_assets=False,
                export_draft=True,
            )

        by_id = {node.object_id: node for node in graph.nodes}
        # KPI carries its owner (from owner_user_id).
        assert by_id[ids["kpi"]].owner == "owner@example.com"
        # Measures and dimensions never carry an owner.
        for node in graph.nodes:
            if node.object_type in ("measure", "dimension"):
                assert node.owner is None, (
                    f"{node.object_type} {node.label} unexpectedly carries an owner"
                )
        # Only KPI (and downstream assets, excluded here) yields owner.
        owner_types = {n.object_type for n in graph.nodes if n.owner}
        assert owner_types == {"kpi"}


# ------------------------------------------------------------------ #
# POST /solidatus/export-preview
# ------------------------------------------------------------------ #


class TestSolidatusExportPreview:
    @pytest.mark.anyio
    async def test_preview_returns_counts(self, client):
        model = make_model()
        project = make_project()
        db = _make_db_for_preview(model, project)
        graph = _make_sample_graph()

        with patch("src.api.solidatus.get_tenant_db", async_gen_from(db)), \
             patch("src.api.solidatus.build_governance_graph", AsyncMock(return_value=graph)):
            resp = await client.post(
                f"{SOLIDATUS_PREFIX}/export-preview",
                json={"include_technical": True, "export_draft": True},
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["nodes_total"] == 3
        assert data["edges_total"] == 1
        assert "domain" in data["by_type"]
        assert data["by_type"]["domain"] == 1
        assert data["by_type"]["measure"] == 1

    @pytest.mark.anyio
    async def test_preview_with_scoped_options(self, client):
        model = make_model()
        project = make_project()
        db = _make_db_for_preview(model, project)
        graph = _make_sample_graph()

        with patch("src.api.solidatus.get_tenant_db", async_gen_from(db)), \
             patch("src.api.solidatus.build_governance_graph", AsyncMock(return_value=graph)):
            resp = await client.post(
                f"{SOLIDATUS_PREFIX}/export-preview",
                json={
                    "include_technical": False,
                    "include_aggregates": False,
                    "include_downstream_assets": False,
                    "include_glossary": False,
                    "include_security_tags": False,
                    "export_draft": True,
                },
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["nodes_total"] == 3

    @pytest.mark.anyio
    async def test_preview_accepts_connection_id(self, client):
        conn = _make_solidatus_conn()
        model = make_model()
        project = make_project()
        db = _make_db_for_preview(model, project, conn)
        graph = _make_sample_graph()

        with patch("src.api.solidatus.get_tenant_db", async_gen_from(db)), \
             patch("src.api.solidatus.build_governance_graph", AsyncMock(return_value=graph)):
            resp = await client.post(
                f"{SOLIDATUS_PREFIX}/export-preview",
                json={
                    "connection_id": str(conn.id),
                    "include_technical": True,
                    "export_draft": True,
                },
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["nodes_total"] == 3

    @pytest.mark.anyio
    async def test_preview_blocks_undeployed_model_by_default(self, client):
        model = make_model()
        project = make_project()
        db = _make_db_for_preview(model, project)

        with patch("src.api.solidatus.get_tenant_db", async_gen_from(db)), \
             patch("src.api.solidatus.build_governance_graph", AsyncMock()) as build_graph:
            resp = await client.post(
                f"{SOLIDATUS_PREFIX}/export-preview",
                json={"include_technical": True},
            )

        assert resp.status_code == 409
        build_graph.assert_not_called()


# ------------------------------------------------------------------ #
# POST /collibra/export-preview
# ------------------------------------------------------------------ #


class TestCollibraExportPreview:
    @pytest.mark.anyio
    async def test_preview_returns_counts(self, client):
        model = make_model()
        project = make_project()
        db = _make_db_for_preview(model, project)
        graph = _make_sample_graph()

        with patch("src.api.collibra.get_tenant_db", async_gen_from(db)), \
             patch("src.api.collibra.build_governance_graph", AsyncMock(return_value=graph)):
            resp = await client.post(
                f"{COLLIBRA_PREFIX}/export-preview",
                json={
                    "include_business_assets": True,
                    "include_technical_assets": True,
                    "export_draft": True,
                },
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["assets_total"] == 3
        assert data["relations_total"] == 1
        assert data["attributes_total"] > 0
        assert data["responsibilities_total"] == 1
        assert "Data Domain" in data["by_asset_type"]
        assert data["by_asset_type"]["Data Domain"] == 1
        assert data["by_asset_type"]["Metric"] == 1

    @pytest.mark.anyio
    async def test_preview_with_scoped_options(self, client):
        model = make_model()
        project = make_project()
        db = _make_db_for_preview(model, project)
        graph = _make_sample_graph()

        with patch("src.api.collibra.get_tenant_db", async_gen_from(db)), \
             patch("src.api.collibra.build_governance_graph", AsyncMock(return_value=graph)):
            resp = await client.post(
                f"{COLLIBRA_PREFIX}/export-preview",
                json={
                    "include_business_assets": True,
                    "include_technical_assets": False,
                    "include_hidden_objects": False,
                    "include_glossary": False,
                    "include_downstream_assets": False,
                    "include_aggregates": False,
                    "include_data_tags": False,
                    "include_responsibilities": False,
                    "export_draft": True,
                },
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["assets_total"] == 3
        assert data["relations_total"] == 1
        assert data["responsibilities_total"] == 0

    @pytest.mark.anyio
    async def test_preview_applies_connection_mapping(self, client):
        conn = _make_collibra_conn()
        conn.asset_type_mapping = {"measure": "Business Metric"}
        model = make_model()
        project = make_project()
        db = _make_db_for_preview(model, project, conn)
        graph = _make_sample_graph()

        with patch("src.api.collibra.get_tenant_db", async_gen_from(db)), \
             patch("src.api.collibra.build_governance_graph", AsyncMock(return_value=graph)):
            resp = await client.post(
                f"{COLLIBRA_PREFIX}/export-preview",
                json={
                    "connection_id": str(conn.id),
                    "include_business_assets": True,
                    "include_technical_assets": True,
                    "export_draft": True,
                },
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["by_asset_type"]["Business Metric"] == 1
        assert "Metric" not in data["by_asset_type"]

    @pytest.mark.anyio
    async def test_preview_blocks_undeployed_model_by_default(self, client):
        model = make_model()
        project = make_project()
        db = _make_db_for_preview(model, project)

        with patch("src.api.collibra.get_tenant_db", async_gen_from(db)), \
             patch("src.api.collibra.build_governance_graph", AsyncMock()) as build_graph:
            resp = await client.post(
                f"{COLLIBRA_PREFIX}/export-preview",
                json={"include_business_assets": True},
            )

        assert resp.status_code == 409
        build_graph.assert_not_called()


# ------------------------------------------------------------------ #
# POST /solidatus/sync
# ------------------------------------------------------------------ #


class TestSolidatusSync:
    @pytest.mark.anyio
    async def test_dry_run_sync(self, client):
        conn = _make_solidatus_conn()
        model = make_model()
        model.deployed_version_id = uuid.uuid4()
        project = make_project()
        db = _make_db_for_sync(model, project, conn)
        graph = _make_sample_graph()

        with patch("src.api.solidatus.get_tenant_db", async_gen_from(db)), \
             patch("src.api.solidatus.audit", new_callable=AsyncMock) as audit_mock, \
             patch("src.solidatus_sync.build_governance_graph", AsyncMock(return_value=graph)):
            resp = await client.post(
                f"{SOLIDATUS_PREFIX}/sync",
                json={
                    "connection_id": str(conn.id),
                    "mode": "dry_run",
                },
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "succeeded"
        assert data["nodes_total"] == 3
        assert data["edges_total"] == 1
        # Dry run: reports what WOULD be created (all nodes/edges are new)
        assert data["nodes_created"] == 3
        assert data["edges_created"] == 1
        audit_mock.assert_awaited_once()
        assert audit_mock.await_args.kwargs["action"] == "solidatus.sync.trigger"
        assert audit_mock.await_args.kwargs["target_id"] == conn.id

    @pytest.mark.anyio
    async def test_sync_mode_contract_and_inconsistent_mode(self, client):
        """F-CSI-04: /sync only advertises the modes it performs.

        ``validate`` and ``file_export`` were advertised in the schema but
        rejected at runtime, so they were removed from the ``mode`` enum.
        Sending them now fails request validation (422). The inconsistent
        ``push`` + ``dry_run=true`` combination is still rejected with the
        explicit domain error.
        """
        from shared.schemas.pydantic_models import SolidatusSyncRequest

        mode_field = SolidatusSyncRequest.model_fields["mode"]
        # The Literal only advertises the two performed modes.
        assert set(mode_field.annotation.__args__) == {"dry_run", "push"}

        conn = _make_solidatus_conn()
        model = make_model()
        model.deployed_version_id = uuid.uuid4()
        project = make_project()
        db = _make_db_for_sync(model, project, conn)

        with patch("src.api.solidatus.get_tenant_db", async_gen_from(db)):
            unsupported_resp = await client.post(
                f"{SOLIDATUS_PREFIX}/sync",
                json={"connection_id": str(conn.id), "mode": "validate"},
            )
            inconsistent_resp = await client.post(
                f"{SOLIDATUS_PREFIX}/sync",
                json={
                    "connection_id": str(conn.id),
                    "mode": "push",
                    "dry_run": True,
                },
            )

        # 'validate' is no longer a valid enum value → request-validation 422.
        assert unsupported_resp.status_code == 422
        # push + dry_run=true still rejected with the explicit domain error.
        assert inconsistent_resp.status_code == 422
        assert inconsistent_resp.json()["detail"]["code"] == "inconsistent_solidatus_sync_mode"


# ------------------------------------------------------------------ #
# POST /collibra/sync
# ------------------------------------------------------------------ #


class TestCollibraSync:
    @pytest.mark.anyio
    async def test_dry_run_sync(self, client):
        conn = _make_collibra_conn()
        model = make_model()
        model.deployed_version_id = uuid.uuid4()
        project = make_project()
        db = _make_db_for_sync(model, project, conn)
        graph = _make_sample_graph()

        with patch("src.api.collibra.get_tenant_db", async_gen_from(db)), \
             patch("src.api.collibra.audit", new_callable=AsyncMock) as audit_mock, \
             patch("src.collibra_sync.build_governance_graph", AsyncMock(return_value=graph)):
            resp = await client.post(
                f"{COLLIBRA_PREFIX}/sync",
                json={
                    "connection_id": str(conn.id),
                    "dry_run": True,
                },
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "succeeded"
        assert data["assets_total"] == 3
        assert data["relations_total"] == 1
        # Dry run: reports what WOULD be created (all assets/relations are new)
        assert data["assets_created"] == 3
        assert data["relations_created"] == 1
        audit_mock.assert_awaited_once()
        assert audit_mock.await_args.kwargs["action"] == "collibra.sync.trigger"
        assert audit_mock.await_args.kwargs["target_id"] == conn.id

    @pytest.mark.anyio
    async def test_sync_contract_drops_unsupported_scope_flags(self, client):
        """F-CSI-04: /sync no longer advertises options it cannot honor.

        ``include_business_assets`` and ``include_hidden_objects`` were
        advertised but always dropped (the sync path forces them True), so
        they were removed from the request schema. They are no longer part
        of the contract, and a sync without them succeeds normally.
        """
        from shared.schemas.pydantic_models import CollibraSyncRequest

        fields = CollibraSyncRequest.model_fields
        assert "include_business_assets" not in fields
        assert "include_hidden_objects" not in fields

        conn = _make_collibra_conn()
        model = make_model()
        model.deployed_version_id = uuid.uuid4()
        project = make_project()
        db = _make_db_for_sync(model, project, conn)
        graph = _make_sample_graph()

        with patch("src.api.collibra.get_tenant_db", async_gen_from(db)), \
             patch("src.api.collibra.audit", new_callable=AsyncMock), \
             patch("src.collibra_sync.build_governance_graph", AsyncMock(return_value=graph)):
            resp = await client.post(
                f"{COLLIBRA_PREFIX}/sync",
                json={
                    "connection_id": str(conn.id),
                    "dry_run": True,
                    "include_responsibilities": False,
                },
            )

        # include_responsibilities=false is now honored (consistent with
        # /export-preview), not rejected with a 422.
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "succeeded"
