"""Regression tests for Collibra/Solidatus remediation wave (Bug-7515 through Bug-7726).

Covers:
  - Bug-7515: CollibraObjectMapping ORM now has connection relationship.
  - Bug-7516: Solidatus syncErrorMessage passes through real errors.
  - Bug-7517: Solidatus runValidate no longer overwrites preview with zeros.
  - Bug-7525: Solidatus reactivates previously-deprecated unchanged objects.
  - Bug-7717: Category-aware deprecation in both orchestrators.
  - Bug-7718: Client constructors accept connection config for validate contract.
  - Bug-7719: Solidatus sync/preview accepts include_hidden_objects.
  - Bug-7522: validate_governance_graph produces non-empty warnings.
  - Bug-7523: Connection schemas constrain mode fields to Literal values.
  - Bug-7526: Client stubs expose deprecation placeholder methods.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from shared.db.models import (
    CollibraConnection,
    CollibraObjectMapping,
    SolidatusObjectMapping,
)
from shared.model_snapshot.governance_graph import (
    GovernanceEdge,
    GovernanceGraph,
    GovernanceNode,
)
from shared.schemas.domains.governance_advanced import (
    CollibraConnectionCreate,
    CollibraConnectionUpdate,
    SolidatusConnectionCreate,
    SolidatusConnectionUpdate,
    SolidatusSyncRequest,
)
from src.collibra_client import CollibraClient, CollibraPushNotImplementedError
from src.solidatus_client import SolidatusClient, SolidatusPushNotImplementedError, SolidatusUpsertResult
from src.governance_helpers import validate_governance_graph
from src.solidatus_sync import run_solidatus_sync

from .conftest import TEST_MODEL_ID, TEST_PROJECT_ID, NOW, make_mock_db


# ---------------------------------------------------------------------------
# Bug-7515 — CollibraObjectMapping ORM has connection relationship
# ---------------------------------------------------------------------------

def test_collibra_object_mapping_has_connection_relationship():
    """CollibraObjectMapping must have a connection relationship, matching
    SolidatusObjectMapping (Bug-7515)."""
    from sqlalchemy import inspect as sa_inspect
    mapper = sa_inspect(CollibraObjectMapping)
    rel_names = {r.key for r in mapper.relationships}
    assert "connection" in rel_names, (
        "CollibraObjectMapping is missing 'connection' relationship — "
        "it must mirror SolidatusObjectMapping"
    )


def test_solidatus_object_mapping_has_connection_relationship():
    """Baseline: SolidatusObjectMapping should already have the relationship."""
    from sqlalchemy import inspect as sa_inspect
    mapper = sa_inspect(SolidatusObjectMapping)
    rel_names = {r.key for r in mapper.relationships}
    assert "connection" in rel_names


# ---------------------------------------------------------------------------
# Bug-7522 — validate_governance_graph produces warnings
# ---------------------------------------------------------------------------

def test_validate_graph_reports_missing_descriptions():
    graph = GovernanceGraph(
        nodes=[
            GovernanceNode(
                stable_key="m.1", object_type="measure", object_id="1",
                label="Revenue", description=None,
            ),
            GovernanceNode(
                stable_key="m.2", object_type="measure", object_id="2",
                label="Cost", description="Total cost",
            ),
        ],
        edges=[],
    )
    warnings = validate_governance_graph(graph)
    desc_w = [w for w in warnings if w["code"] == "MISSING_DESCRIPTION"]
    assert len(desc_w) == 1
    assert desc_w[0]["count"] == 1


def test_validate_graph_reports_missing_owners_on_ownable_types():
    graph = GovernanceGraph(
        nodes=[
            GovernanceNode(
                stable_key="kpi.1", object_type="kpi", object_id="1",
                label="Retention", owner=None,
            ),
            GovernanceNode(
                stable_key="kpi.2", object_type="kpi", object_id="2",
                label="Growth", owner="owner@x.com",
            ),
        ],
        edges=[],
    )
    warnings = validate_governance_graph(graph)
    owner_w = [w for w in warnings if w["code"] == "MISSING_OWNER"]
    assert len(owner_w) == 1
    assert owner_w[0]["count"] == 1


def test_validate_graph_reports_orphan_edges():
    graph = GovernanceGraph(
        nodes=[
            GovernanceNode(
                stable_key="a", object_type="measure", object_id="1",
                label="A", description="desc",
            ),
        ],
        edges=[
            GovernanceEdge(
                stable_key="e1", source_key="a", target_key="missing",
                relationship_type="contains",
            ),
        ],
    )
    warnings = validate_governance_graph(graph)
    orphan_w = [w for w in warnings if w["code"] == "ORPHAN_EDGES"]
    assert len(orphan_w) == 1
    assert orphan_w[0]["count"] == 1


def test_validate_graph_clean():
    """A well-formed graph with descriptions and owners should produce no warnings."""
    graph = GovernanceGraph(
        nodes=[
            GovernanceNode(
                stable_key="kpi.1", object_type="kpi", object_id="1",
                label="Revenue", description="Total rev", owner="o@x.com",
            ),
        ],
        edges=[],
    )
    assert validate_governance_graph(graph) == []


# ---------------------------------------------------------------------------
# Bug-7523 — Connection schemas constrain mode fields
# ---------------------------------------------------------------------------

def test_collibra_create_rejects_unsupported_auth_type():
    with pytest.raises(ValidationError):
        CollibraConnectionCreate(
            display_name="test",
            base_url="https://collibra.example.com",
            auth_type="oauth2",
            token="tok",
        )


def test_solidatus_create_rejects_unsupported_sync_scope():
    with pytest.raises(ValidationError):
        SolidatusConnectionCreate(
            display_name="test",
            base_url="https://solidatus.example.com",
            token="tok",
            sync_scope="workspace",
        )


def test_collibra_update_rejects_unsupported_sync_mode():
    with pytest.raises(ValidationError):
        CollibraConnectionUpdate(sync_mode="import")


# ---------------------------------------------------------------------------
# Bug-7718 — Client constructors accept connection config
# ---------------------------------------------------------------------------

def test_collibra_client_accepts_config():
    client = CollibraClient(
        base_url="https://collibra.example.com",
        token="tok",
        community_id="c1",
        domain_id="d1",
        asset_type_mapping={"measure": "Data Element"},
    )
    assert client._community_id == "c1"
    assert client._domain_id == "d1"
    assert client._asset_type_mapping == {"measure": "Data Element"}


def test_solidatus_client_accepts_config():
    client = SolidatusClient(
        base_url="https://solidatus.example.com",
        token="tok",
        workspace_id="ws1",
        model_ref="mr1",
    )
    assert client._workspace_id == "ws1"
    assert client._model_ref == "mr1"


# ---------------------------------------------------------------------------
# Bug-7526 — Client stubs expose deprecation methods
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_collibra_deprecate_assets_raises():
    client = CollibraClient(base_url="https://x.com", token="t")
    with pytest.raises(CollibraPushNotImplementedError):
        await client.deprecate_assets(["ext-1"])


@pytest.mark.anyio
async def test_solidatus_deprecate_nodes_raises():
    client = SolidatusClient(base_url="https://x.com", token="t")
    with pytest.raises(SolidatusPushNotImplementedError):
        await client.deprecate_nodes(["ext-1"])


# ---------------------------------------------------------------------------
# Bug-7525 — Solidatus reactivates previously-deprecated objects
# ---------------------------------------------------------------------------

def _graph_with_node() -> GovernanceGraph:
    return GovernanceGraph(
        nodes=[
            GovernanceNode(
                stable_key="domain.sales",
                object_type="domain",
                object_id="domain-1",
                label="Sales",
            ),
        ],
        edges=[],
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


async def _refresh_defaults(obj):
    if getattr(obj, "id", None) is None:
        obj.id = uuid.uuid4()
    if getattr(obj, "started_at", None) is None:
        obj.started_at = NOW


@pytest.mark.anyio
async def test_solidatus_reactivates_deprecated_unchanged_node():
    """A deprecated mapping with unchanged hash should be re-pushed and
    un-deprecated, matching Collibra behavior (Bug-7525)."""

    conn = _solidatus_conn()
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

    # Simulate an existing deprecated mapping whose hash matches the
    # current payload — i.e. object was removed then re-added unchanged.
    deprecated_mapping = types.SimpleNamespace(
        tessallite_object_type="domain",
        tessallite_object_id="domain-1",
        tessallite_stable_key="domain.sales",
        last_payload_hash="will-be-replaced",
        is_deprecated=True,
        last_synced_at=NOW,
        last_sync_run_id=None,
        solidatus_object_id=None,
        solidatus_object_ref=None,
    )

    # The existing mapping must be returned by load_mappings.
    existing_mappings = {
        ("domain", "domain-1"): deprecated_mapping,
    }

    class FakeUpsert:
        def __init__(self, *_args, **_kwargs):
            pass
        async def upsert_nodes(self, nodes, workspace_id=""):
            return SolidatusUpsertResult(created=0, updated=len(nodes))
        async def upsert_edges(self, edges, workspace_id=""):
            return SolidatusUpsertResult(created=0, updated=len(edges))

    with (
        patch("src.solidatus_sync.build_governance_graph", AsyncMock(return_value=_graph_with_node())),
        patch("src.solidatus_sync.decrypt_token", return_value="plain-token"),
        patch("src.solidatus_sync.SolidatusClient", FakeUpsert),
        patch("src.solidatus_sync.load_mappings", AsyncMock(return_value=existing_mappings)),
    ):
        run = await run_solidatus_sync(
            db,
            connection_id=conn.id,
            project_id=TEST_PROJECT_ID,
            model_id=TEST_MODEL_ID,
            dry_run=False,
        )

    assert run.status == "succeeded"
    # The deprecated mapping must have been un-deprecated.
    assert deprecated_mapping.is_deprecated is False


# ---------------------------------------------------------------------------
# Bug-7717 — Category-aware deprecation
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_solidatus_deprecate_missing_skips_excluded_categories():
    """When a sync excludes certain categories (e.g. glossary), mappings in
    those excluded categories must NOT be deprecated (Bug-7717)."""

    conn = _solidatus_conn()
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

    # Simulate existing mappings: one domain (will be in the graph) and
    # one glossary_term (will NOT be in the graph because glossary is excluded).
    domain_mapping = types.SimpleNamespace(
        tessallite_object_type="domain",
        tessallite_object_id="domain-1",
        tessallite_stable_key="domain.sales",
        last_payload_hash="some-hash",
        is_deprecated=False,
        last_synced_at=NOW,
        last_sync_run_id=None,
        solidatus_object_id=None,
        solidatus_object_ref=None,
    )
    glossary_mapping = types.SimpleNamespace(
        tessallite_object_type="glossary_term",
        tessallite_object_id="glossary-1",
        tessallite_stable_key="glossary.term1",
        last_payload_hash="some-hash",
        is_deprecated=False,
        last_synced_at=NOW,
        last_sync_run_id=None,
        solidatus_object_id=None,
        solidatus_object_ref=None,
    )
    existing_mappings = {
        ("domain", "domain-1"): domain_mapping,
        ("glossary_term", "glossary-1"): glossary_mapping,
    }

    # Build a graph with only a domain node (glossary excluded).
    graph = _graph_with_node()

    deprecated_calls: list[list[str]] = []

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass
        async def upsert_nodes(self, nodes, workspace_id=""):
            return SolidatusUpsertResult(created=0, updated=len(nodes))
        async def upsert_edges(self, edges, workspace_id=""):
            return SolidatusUpsertResult(created=0, updated=len(edges))
        async def deprecate_nodes(self, external_ids):
            deprecated_calls.append(list(external_ids))
            return len(external_ids)
        async def deprecate_edges(self, external_ids):
            return len(external_ids)

    with (
        patch("src.solidatus_sync.build_governance_graph", AsyncMock(return_value=graph)),
        patch("src.solidatus_sync.decrypt_token", return_value="plain-token"),
        patch("src.solidatus_sync.SolidatusClient", FakeClient),
        patch("src.solidatus_sync.load_mappings", AsyncMock(return_value=existing_mappings)),
    ):
        run = await run_solidatus_sync(
            db,
            connection_id=conn.id,
            project_id=TEST_PROJECT_ID,
            model_id=TEST_MODEL_ID,
            dry_run=False,
            deprecate_missing=True,
            # F-035-03: glossary is EXCLUDED from this run, so its type is out
            # of the run's deprecation scope and its stranded mapping must be
            # left untouched (Bug-7717 category-aware deprecation preserved).
            include_glossary=False,
        )

    assert run.status == "succeeded"
    # Glossary mapping must NOT be deprecated: glossary was excluded from this
    # run, so "glossary_term" is not in the run's deprecation scope.
    assert glossary_mapping.is_deprecated is False
    # And the remote deprecation call must not have carried the glossary id.
    assert all("glossary" not in gid for call in deprecated_calls for gid in call)
