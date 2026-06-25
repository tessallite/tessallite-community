from __future__ import annotations

import types
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.db.models import (
    CollibraConnection,
    CollibraObjectMapping,
    CollibraSyncRun,
    SolidatusConnection,
    SolidatusObjectMapping,
    SolidatusSyncRun,
)
from shared.model_snapshot.governance_graph import (
    GovernanceEdge,
    GovernanceGraph,
    GovernanceNode,
)
from src.collibra_client import CollibraUpsertResult
from src.collibra_sync import run_collibra_sync
from src.solidatus_client import SolidatusUpsertResult
from src.solidatus_sync import run_solidatus_sync

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    NOW,
    make_mock_db,
)


def _graph() -> GovernanceGraph:
    return GovernanceGraph(
        nodes=[
            GovernanceNode(
                stable_key="domain.sales",
                object_type="domain",
                object_id="domain-1",
                label="Sales",
            ),
            GovernanceNode(
                stable_key="model.sales",
                object_type="semantic_model",
                object_id="model-1",
                label="Sales Model",
            ),
        ],
        edges=[
            GovernanceEdge(
                stable_key="domain.sales.contains.model.sales",
                source_key="domain.sales",
                target_key="model.sales",
                relationship_type="contains",
            ),
        ],
    )


def _solidatus_conn() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        project_id=TEST_PROJECT_ID,
        model_id=TEST_MODEL_ID,
        base_url="https://solidatus.example.com",
        encrypted_credentials=b"solidatus-encrypted",
        workspace_id="workspace-1",
        model_ref="model-ref-1",
    )


def _collibra_conn() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        project_id=TEST_PROJECT_ID,
        model_id=TEST_MODEL_ID,
        base_url="https://collibra.example.com",
        encrypted_credentials=b"collibra-encrypted",
        domain_id="domain-1",
        asset_type_mapping={},
        relation_type_mapping={},
        responsibility_mapping={},
    )


async def _refresh_defaults(obj):
    if getattr(obj, "id", None) is None:
        obj.id = uuid.uuid4()
    if getattr(obj, "started_at", None) is None:
        obj.started_at = NOW


def _db_for(conn: types.SimpleNamespace):
    db = make_mock_db()
    added = []
    db.add = MagicMock(side_effect=added.append)
    db.commit = AsyncMock()
    db.refresh = AsyncMock(side_effect=_refresh_defaults)

    async def _get(_cls, pk):
        if pk == conn.id:
            return conn
        return None

    db.get = _get
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=result)
    return db, added


@pytest.mark.anyio
async def test_solidatus_dry_run_does_not_decrypt_or_push():
    conn = _solidatus_conn()
    db, added = _db_for(conn)

    with (
        patch("src.solidatus_sync.build_governance_graph", AsyncMock(return_value=_graph())),
        patch("src.solidatus_sync.decrypt_token", side_effect=AssertionError("decrypt called")),
        patch("src.solidatus_sync.SolidatusClient", side_effect=AssertionError("client called")),
    ):
        run = await run_solidatus_sync(
            db,
            connection_id=conn.id,
            project_id=TEST_PROJECT_ID,
            model_id=TEST_MODEL_ID,
            dry_run=True,
        )

    assert run.status == "succeeded"
    assert run.nodes_created == 2
    assert run.edges_created == 1
    assert not any(isinstance(obj, SolidatusObjectMapping) for obj in added)


@pytest.mark.anyio
async def test_collibra_dry_run_does_not_decrypt_or_push():
    conn = _collibra_conn()
    db, added = _db_for(conn)

    with (
        patch("src.collibra_sync.build_governance_graph", AsyncMock(return_value=_graph())),
        patch("src.collibra_sync.decrypt_token", side_effect=AssertionError("decrypt called")),
        patch("src.collibra_sync.CollibraClient", side_effect=AssertionError("client called")),
    ):
        run = await run_collibra_sync(
            db,
            connection_id=conn.id,
            project_id=TEST_PROJECT_ID,
            model_id=TEST_MODEL_ID,
            dry_run=True,
        )

    assert run.status == "succeeded"
    assert run.assets_created == 2
    assert run.relations_created == 1
    assert not any(isinstance(obj, CollibraObjectMapping) for obj in added)


@pytest.mark.anyio
async def test_collibra_sync_applies_connection_mapping_to_push_payload():
    captured: dict[str, list] = {}

    class MappingClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def upsert_assets(self, assets, domain_id: str = ""):
            captured["assets"] = assets
            return CollibraUpsertResult(created=len(assets))

        async def upsert_relations(self, relations):
            captured["relations"] = relations
            return CollibraUpsertResult(created=len(relations))

        async def upsert_responsibilities(self, responsibilities):
            captured["responsibilities"] = responsibilities
            return CollibraUpsertResult(created=len(responsibilities))

    # Use a KPI node carrying an owner: the real exporter only sets `owner`
    # on KPI and downstream-asset nodes (measures/dimensions have no owner
    # column), so this exercises a responsibility the system can actually
    # produce — not a synthetic owner the exporter never emits (F-CSI-05).
    graph = GovernanceGraph(
        nodes=[
            GovernanceNode(
                stable_key="kpi.revenue_attainment",
                object_type="kpi",
                object_id="kpi-1",
                label="Revenue Attainment",
                owner="owner@example.com",
            ),
        ],
        edges=[
            GovernanceEdge(
                stable_key="model.sales.contains.kpi.revenue_attainment",
                source_key="model.sales",
                target_key="kpi.revenue_attainment",
                relationship_type="contains",
            ),
        ],
    )
    conn = _collibra_conn()
    conn.asset_type_mapping = {"kpi": "Business Metric"}
    conn.relation_type_mapping = {"contains": "groups"}
    conn.responsibility_mapping = {"owner": "Data Steward"}
    db, _added = _db_for(conn)

    with (
        patch("src.collibra_sync.build_governance_graph", AsyncMock(return_value=graph)),
        patch("src.collibra_sync.decrypt_token", return_value="plain-token"),
        patch("src.collibra_sync.CollibraClient", MappingClient),
    ):
        run = await run_collibra_sync(
            db,
            connection_id=conn.id,
            project_id=TEST_PROJECT_ID,
            model_id=TEST_MODEL_ID,
            dry_run=False,
        )

    assert run.status == "succeeded"
    assert captured["assets"][0].asset_type == "Business Metric"
    assert captured["relations"][0].relation_type == "groups"
    assert captured["responsibilities"][0].role == "Data Steward"


@pytest.mark.anyio
async def test_solidatus_push_fails_without_real_client_and_decrypts_credentials():
    conn = _solidatus_conn()
    db, added = _db_for(conn)

    with (
        patch("src.solidatus_sync.build_governance_graph", AsyncMock(return_value=_graph())),
        patch("src.solidatus_sync.decrypt_token", return_value="plain-token") as decrypt_token,
        pytest.raises(NotImplementedError, match="Solidatus non-dry-run sync is not implemented"),
    ):
        await run_solidatus_sync(
            db,
            connection_id=conn.id,
            project_id=TEST_PROJECT_ID,
            model_id=TEST_MODEL_ID,
            dry_run=False,
        )

    decrypt_token.assert_called_once_with(conn.encrypted_credentials)
    assert added[0].status == "failed"
    assert "Solidatus non-dry-run sync is not implemented" in added[0].error_message


@pytest.mark.anyio
async def test_collibra_push_fails_without_real_client_and_decrypts_credentials():
    conn = _collibra_conn()
    db, added = _db_for(conn)

    with (
        patch("src.collibra_sync.build_governance_graph", AsyncMock(return_value=_graph())),
        patch("src.collibra_sync.decrypt_token", return_value="plain-token") as decrypt_token,
        pytest.raises(NotImplementedError, match="Collibra non-dry-run sync is not implemented"),
    ):
        await run_collibra_sync(
            db,
            connection_id=conn.id,
            project_id=TEST_PROJECT_ID,
            model_id=TEST_MODEL_ID,
            dry_run=False,
        )

    decrypt_token.assert_called_once_with(conn.encrypted_credentials)
    assert added[0].status == "failed"
    assert "Collibra non-dry-run sync is not implemented" in added[0].error_message


@pytest.mark.anyio
async def test_solidatus_persists_returned_remote_ids_without_stable_key_fallback():
    class MappingClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def upsert_nodes(self, nodes, workspace_id: str = ""):
            return SolidatusUpsertResult(
                created=len(nodes),
                object_mappings={
                    "domain.sales": "solidatus-domain-remote",
                    "model.sales": "solidatus-model-remote",
                },
            )

        async def upsert_edges(self, edges, workspace_id: str = ""):
            return SolidatusUpsertResult(
                created=len(edges),
                object_mappings={
                    "domain.sales.contains.model.sales": "solidatus-edge-remote",
                },
            )

    conn = _solidatus_conn()
    db, added = _db_for(conn)

    with (
        patch("src.solidatus_sync.build_governance_graph", AsyncMock(return_value=_graph())),
        patch("src.solidatus_sync.decrypt_token", return_value="plain-token"),
        patch("src.solidatus_sync.SolidatusClient", MappingClient),
    ):
        run = await run_solidatus_sync(
            db,
            connection_id=conn.id,
            project_id=TEST_PROJECT_ID,
            model_id=TEST_MODEL_ID,
            dry_run=False,
        )

    assert run.status == "succeeded"
    mappings = {
        (m.tessallite_object_type, m.tessallite_object_id): m
        for m in added
        if isinstance(m, SolidatusObjectMapping)
    }
    assert mappings[("domain", "domain-1")].solidatus_object_id == "solidatus-domain-remote"
    assert mappings[("semantic_model", "model-1")].solidatus_object_id == "solidatus-model-remote"
    assert mappings[("contains", "domain.sales.contains.model.sales")].solidatus_object_id == "solidatus-edge-remote"
    assert mappings[("domain", "domain-1")].solidatus_object_id != "domain.sales"


@pytest.mark.anyio
async def test_collibra_persists_returned_remote_ids_without_stable_key_fallback():
    class MappingClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def upsert_assets(self, assets, domain_id: str = ""):
            return CollibraUpsertResult(
                created=len(assets),
                object_mappings={
                    "domain.sales": "collibra-domain-remote",
                    "model.sales": "collibra-model-remote",
                },
            )

        async def upsert_relations(self, relations):
            return CollibraUpsertResult(
                created=len(relations),
                object_mappings={
                    "domain.sales.contains.model.sales": "collibra-relation-remote",
                },
            )

        async def upsert_responsibilities(self, responsibilities):
            return CollibraUpsertResult(created=len(responsibilities))

    conn = _collibra_conn()
    db, added = _db_for(conn)

    with (
        patch("src.collibra_sync.build_governance_graph", AsyncMock(return_value=_graph())),
        patch("src.collibra_sync.decrypt_token", return_value="plain-token"),
        patch("src.collibra_sync.CollibraClient", MappingClient),
    ):
        run = await run_collibra_sync(
            db,
            connection_id=conn.id,
            project_id=TEST_PROJECT_ID,
            model_id=TEST_MODEL_ID,
            dry_run=False,
        )

    assert run.status == "succeeded"
    mappings = {
        (m.tessallite_object_type, m.tessallite_object_id, m.collibra_resource_type): m
        for m in added
        if isinstance(m, CollibraObjectMapping)
    }
    assert mappings[("domain", "domain-1", "asset")].collibra_resource_id == "collibra-domain-remote"
    assert mappings[("semantic_model", "model-1", "asset")].collibra_resource_id == "collibra-model-remote"
    assert (
        mappings[("contains", "domain.sales.contains.model.sales", "relation")].collibra_resource_id
        == "collibra-relation-remote"
    )
    assert mappings[("domain", "domain-1", "asset")].collibra_resource_id != "domain.sales"


def test_solidatus_is_deprecated_column_is_in_orm_and_migration():
    migration_path = (
        Path(__file__).resolve().parents[3]
        / "shared"
        / "db"
        / "migrations"
        / "versions"
        / "0143_solidatus_collibra_integration.py"
    )
    migration_source = migration_path.read_text(encoding="utf-8")

    assert "is_deprecated" in SolidatusObjectMapping.__table__.c
    assert 'sa.Column("is_deprecated", sa.Boolean' in migration_source


def _migration_source() -> str:
    migration_path = (
        Path(__file__).resolve().parents[3]
        / "shared"
        / "db"
        / "migrations"
        / "versions"
        / "0143_solidatus_collibra_integration.py"
    )
    return migration_path.read_text(encoding="utf-8")


def test_orm_declared_indexes_match_migration_create_index():
    """Regression for F-CSI-02 (ORM/migration index drift).

    Every column declared with ``index=True`` on the 6 governance ORM tables
    must have a matching ``op.create_index`` in 0143's upgrade and a matching
    ``op.drop_index`` in its downgrade. ``index=True`` only emits DDL through
    ``create_all`` (test DBs); Alembic-migrated environments (staging/prod)
    get the index ONLY if the migration creates it explicitly.
    """
    source = _migration_source()

    governance_tables = [
        SolidatusConnection,
        SolidatusSyncRun,
        SolidatusObjectMapping,
        CollibraConnection,
        CollibraSyncRun,
        CollibraObjectMapping,
    ]

    # Collect (table_name, column_name) for every ORM-declared index.
    declared: list[tuple[str, str]] = []
    for model_cls in governance_tables:
        table = model_cls.__table__
        for index in table.indexes:
            cols = list(index.columns)
            assert len(cols) == 1, (
                f"unexpected composite index {index.name} on {table.name}"
            )
            declared.append((table.name, cols[0].name))

    assert declared, "expected at least one declared index on governance tables"

    for table_name, column_name in declared:
        expected_index = f"ix_{table_name}_{column_name}"
        create_call = (
            f'op.create_index("{expected_index}", "{table_name}", ["{column_name}"])'
        )
        drop_call = (
            f'op.drop_index("{expected_index}", table_name="{table_name}")'
        )
        assert create_call in source, (
            f"migration 0143 is missing create_index for {table_name}.{column_name}: "
            f"expected `{create_call}`"
        )
        assert drop_call in source, (
            f"migration 0143 downgrade is missing drop_index for "
            f"{table_name}.{column_name}: expected `{drop_call}`"
        )
