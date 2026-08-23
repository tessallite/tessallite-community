"""Regression guards for governance mapper / hash fixes.

Covers:
  - Bug-6491: payload_hash includes lifecycle status.
  - Bug-6492: Solidatus node carries status / owner / steward.
  - Bug-6497: Collibra responsibilities honor owner AND steward mappings.
  - Bug-6496: responsibility incremental hash key/hash helpers.
  - Bug-6141: dimension CLS restriction helper (fail-closed).

These are pure-function tests over the mappers and helpers — no live
Collibra/Solidatus round-trip (the clients are simulated per Bug-6038).
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from shared.model_snapshot.governance_graph import (
    GovernanceEdge,
    GovernanceGraph,
    GovernanceNode,
)

from src.collibra_mapper import map_graph_to_collibra
from src.solidatus_mapper import map_graph_to_solidatus
from src.governance_helpers import (
    payload_hash,
    responsibility_hash,
    responsibility_key,
)
from src.api.dimensions import _dim_touches_restricted_column
from shared.db.models import CollibraObjectMapping, SolidatusObjectMapping


def _graph() -> GovernanceGraph:
    return GovernanceGraph(
        nodes=[
            GovernanceNode(
                stable_key="measure.rev",
                object_type="measure",
                object_id=str(uuid.uuid4()),
                label="Revenue",
                status="active",
                owner="owner@example.com",
                steward="steward@example.com",
            ),
        ],
        edges=[],
    )


# ---------------------------------------------------------------------------
# Bug-6491 — payload_hash includes status
# ---------------------------------------------------------------------------

def test_payload_hash_reflects_status_change():
    graph = _graph()
    assets = map_graph_to_collibra(graph).assets
    asset = assets[0]
    h1 = payload_hash(asset)
    asset.status = "Deprecated"
    h2 = payload_hash(asset)
    assert h1 != h2, "a status-only change must change the payload hash"


def test_payload_hash_stable_for_statusless_edge():
    edge = types.SimpleNamespace(
        external_id="e1",
        type="contains",
        source_external_id="a",
        target_external_id="b",
        relation_type="contains",
        properties={},
    )
    # No status attribute -> must not raise and must be stable.
    assert payload_hash(edge) == payload_hash(edge)


# ---------------------------------------------------------------------------
# Bug-6492 — Solidatus node carries status / owner / steward
# ---------------------------------------------------------------------------

def test_solidatus_node_carries_governance_fields():
    payload = map_graph_to_solidatus(_graph())
    props = payload.nodes[0].properties
    assert props["status"] == "active"
    assert props["owner"] == "owner@example.com"
    assert props["steward"] == "steward@example.com"


def test_solidatus_node_omits_absent_governance_fields():
    graph = GovernanceGraph(
        nodes=[
            GovernanceNode(
                stable_key="t.1",
                object_type="table",
                object_id=str(uuid.uuid4()),
                label="Orders",
            )
        ],
        edges=[],
    )
    props = map_graph_to_solidatus(graph).nodes[0].properties
    assert "status" not in props
    assert "owner" not in props
    assert "steward" not in props


# ---------------------------------------------------------------------------
# Bug-6497 — Collibra responsibilities: owner + steward, honoring mapping
# ---------------------------------------------------------------------------

def test_collibra_exports_owner_and_steward_responsibilities():
    payload = map_graph_to_collibra(
        _graph(),
        responsibility_mapping={"owner": "Business Owner", "steward": "Data Steward"},
    )
    by_role = {(r.role, r.user_or_group) for r in payload.responsibilities}
    assert ("Business Owner", "owner@example.com") in by_role
    assert ("Data Steward", "steward@example.com") in by_role


def test_collibra_steward_uses_default_role_when_unmapped():
    payload = map_graph_to_collibra(_graph())
    roles = {r.role for r in payload.responsibilities}
    assert "Data Steward" in roles


# ---------------------------------------------------------------------------
# Bug-6496 — responsibility incremental hash
# ---------------------------------------------------------------------------

def test_responsibility_hash_changes_with_assignee():
    r1 = types.SimpleNamespace(
        asset_external_id="measure.rev", role="Owner", user_or_group="a@x.com"
    )
    r2 = types.SimpleNamespace(
        asset_external_id="measure.rev", role="Owner", user_or_group="b@x.com"
    )
    assert responsibility_key(r1) == responsibility_key(r2)
    assert responsibility_hash(r1) != responsibility_hash(r2)


def test_responsibility_key_is_namespaced():
    r = types.SimpleNamespace(
        asset_external_id="measure.rev", role="Owner", user_or_group="a@x.com"
    )
    assert responsibility_key(r)[0] == "responsibility"


def test_owner_and_steward_keys_distinct_under_shared_role_label():
    # Codex-R2: if owner and steward map to the SAME Collibra role label, the
    # incremental key must still distinguish them (by kind), or one assignee is
    # dropped / collides on the mapping unique constraint.
    payload = map_graph_to_collibra(
        _graph(),
        responsibility_mapping={"owner": "Data Owner", "steward": "Data Owner"},
    )
    resp_by_kind = {r.kind: r for r in payload.responsibilities}
    assert set(resp_by_kind) == {"owner", "steward"}
    k_owner = responsibility_key(resp_by_kind["owner"])
    k_steward = responsibility_key(resp_by_kind["steward"])
    assert k_owner != k_steward
    # Same role label, different assignee -> distinct hashes too.
    assert responsibility_hash(resp_by_kind["owner"]) != responsibility_hash(
        resp_by_kind["steward"]
    )


# ---------------------------------------------------------------------------
# Bug-6500 - ORM unique constraints mirror Alembic-created integration tables
# ---------------------------------------------------------------------------

def _unique_column_sets(model) -> set[tuple[str, ...]]:
    return {
        tuple(constraint.columns.keys())
        for constraint in model.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }


def test_governance_mapping_orm_declares_solidatus_unique_key():
    assert (
        "connection_id",
        "tessallite_object_type",
        "tessallite_object_id",
    ) in _unique_column_sets(SolidatusObjectMapping)


def test_governance_mapping_orm_declares_collibra_unique_key():
    assert (
        "connection_id",
        "tessallite_object_type",
        "tessallite_object_id",
        "collibra_resource_type",
    ) in _unique_column_sets(CollibraObjectMapping)


# ---------------------------------------------------------------------------
# Bug-6141 — dimension CLS restriction helper (fail-closed)
# ---------------------------------------------------------------------------

def test_dim_touches_restricted_column_on_source():
    col = uuid.uuid4()
    dim = types.SimpleNamespace(source_column_id=col, display_column_id=None)
    assert _dim_touches_restricted_column(dim, {col}) is True


def test_dim_touches_restricted_column_on_display():
    col = uuid.uuid4()
    dim = types.SimpleNamespace(source_column_id=uuid.uuid4(), display_column_id=col)
    assert _dim_touches_restricted_column(dim, {col}) is True


def test_dim_not_restricted_when_columns_clear():
    dim = types.SimpleNamespace(
        source_column_id=uuid.uuid4(), display_column_id=uuid.uuid4()
    )
    assert _dim_touches_restricted_column(dim, {uuid.uuid4()}) is False


def test_dim_not_restricted_with_empty_set():
    dim = types.SimpleNamespace(
        source_column_id=uuid.uuid4(), display_column_id=None
    )
    assert _dim_touches_restricted_column(dim, set()) is False


# ---------------------------------------------------------------------------
# Bug-6141 (Codex-R2) — redundant-partner hint must not leak a restricted
# partner column name on an otherwise-visible dimension.
# ---------------------------------------------------------------------------

def _mock_db_for_build_response(source_col, table):
    db = AsyncMock()

    async def _get(cls, pk):
        name = cls.__name__
        if name == "ModelColumn":
            return source_col if pk == source_col.id else None
        if name == "ModelTable":
            return table
        return None

    db.get = AsyncMock(side_effect=_get)
    return db


def _build_response_inputs():
    from src.api.dimensions import _build_response  # local import (heavy module)

    src_col_id = uuid.uuid4()
    partner_col_id = uuid.uuid4()
    table_id = uuid.uuid4()
    source_col = types.SimpleNamespace(
        id=src_col_id, column_name="visible_col", data_type="text",
        model_table_id=table_id, is_hidden=False, cardinality_estimate=None,
    )
    table = types.SimpleNamespace(
        id=table_id, alias="t", display_name="T", row_count_estimate=None,
    )
    dim = types.SimpleNamespace(
        id=uuid.uuid4(), model_id=uuid.uuid4(), name="Region",
        display_name="Region", description=None, display_folder=None,
        source_column_id=src_col_id, display_column_id=None,
        user_defined_attribute_id=None, is_time_dim=False, time_grain=None,
        is_invalid=False, invalid_reason=None,
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    hint = types.SimpleNamespace(
        partner_column_id=partner_col_id,
        partner_column_name="SECRET_SALARY",
        partner_table_name="fact", partner_physical_table="fact",
        join_type="inner", reason="Equivalent to fact.SECRET_SALARY",
    )
    partners = {src_col_id: hint}
    return _build_response, dim, source_col, table, partners, partner_col_id


@pytest.mark.asyncio
async def test_redundant_partner_suppressed_when_partner_column_restricted():
    (_build_response, dim, source_col, table, partners,
     partner_col_id) = _build_response_inputs()
    db = _mock_db_for_build_response(source_col, table)
    resp = await _build_response(
        db, dim, partners, glossary_texts={},
        restricted_cols={partner_col_id},
        # Prefetched-empty: this unit test exercises redundant-partner logic, not
        # attribute relationships, so pass an empty prefetch map to avoid an
        # unrelated relationship query against the minimal mock.
        attr_rels_by_dim={},
    )
    assert resp.redundant_partner is None


@pytest.mark.asyncio
async def test_redundant_partner_present_when_partner_column_clear():
    (_build_response, dim, source_col, table, partners,
     _partner_col_id) = _build_response_inputs()
    db = _mock_db_for_build_response(source_col, table)
    resp = await _build_response(
        db, dim, partners, glossary_texts={}, restricted_cols=set(),
        attr_rels_by_dim={},
    )
    assert resp.redundant_partner is not None
    assert resp.redundant_partner.partner_column_name == "SECRET_SALARY"
