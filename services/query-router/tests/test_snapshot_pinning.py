"""B15 / F-013-01 — deploy pins the served snapshot (gate G1 Option A).

Business contract: a deployed model serves its DEPLOYED snapshot, not the
live draft. A draft edit to a measure expression must NOT reach BI clients
until the next Deploy.

These tests assert the binder resolves the pinned shape (from the deployed
version's snapshot_json) rather than the live tables, and that the
snapshot resolver hydrates measures/dimensions/hidden-columns faithfully
from a snapshot dict.
"""
from __future__ import annotations

import sys
import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest

_SHARED_DB_SESSION = sys.modules.get("shared.db.session")
_POLLUTED = _SHARED_DB_SESSION is not None and getattr(
    _SHARED_DB_SESSION, "__file__", None
) is None

pytestmark = pytest.mark.skipif(
    _POLLUTED,
    reason="sys.modules polluted by another test; run this file alone",
)

from src.ir.logical_query import LogicalQuery
from src.semantic import snapshot_resolver
from src.semantic.snapshot_resolver import (
    DeployedShape,
    LiveMetadataBundle,
    hierarchy_level_dimensions_from_snapshot,
    resolve_deployed_shape,
    resolve_live_metadata_bundle,
)


def _lq(measures=None, dims=None) -> LogicalQuery:
    return LogicalQuery(
        model_id="model-1",
        protocol="jdbc",
        raw_query="SELECT revenue FROM m",
        requested_measures=measures or ["revenue"],
        requested_dimensions=dims or [],
        filters=[],
        grain=[],
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint="fp",
    )


# ---------------------------------------------------------------------------
# Snapshot resolver hydration
# ---------------------------------------------------------------------------

def test_build_shape_hydrates_measures_and_hidden_columns():
    snapshot_resolver.invalidate()
    model_id = uuid.uuid4()
    visible_col = str(uuid.uuid4())
    hidden_col = str(uuid.uuid4())
    snapshot = {
        "measures": [
            {
                "id": str(uuid.uuid4()),
                "name": "revenue",
                "default_agg": "sum",
                "measure_type": "standard",
                "expression": None,
                "source_column_id": visible_col,
                "is_additive": True,
                "data_type": "numeric",
            }
        ],
        "dimensions": [
            {"id": str(uuid.uuid4()), "name": "region", "source_column_id": visible_col}
        ],
        "columns": [
            {"id": visible_col, "column_name": "amount", "is_hidden": False},
            {"id": hidden_col, "column_name": "secret_cost", "is_hidden": True},
        ],
        "hierarchies": [],
    }
    shape = snapshot_resolver._build_shape(model_id, snapshot)

    assert [m.name for m in shape.measures] == ["revenue"]
    assert shape.measures[0].default_agg == "sum"
    assert shape.measures[0].model_id == model_id
    assert [d.name for d in shape.dimensions] == ["region"]
    assert uuid.UUID(hidden_col) in shape.hidden_column_ids
    assert uuid.UUID(visible_col) not in shape.hidden_column_ids
    assert "amount" in shape.physical_columns_visible
    assert "secret_cost" in shape.physical_columns_all
    assert "secret_cost" not in shape.physical_columns_visible


@pytest.mark.asyncio
async def test_resolve_deployed_shape_returns_none_when_no_pointer():
    model = types.SimpleNamespace(id=uuid.uuid4(), deployed_version_id=None)
    assert await resolve_deployed_shape(model, AsyncMock()) is None


@pytest.mark.asyncio
async def test_resolve_deployed_shape_caches_per_version():
    snapshot_resolver.invalidate()
    model_id = uuid.uuid4()
    version_id = uuid.uuid4()
    model = types.SimpleNamespace(id=model_id, deployed_version_id=version_id)
    version = types.SimpleNamespace(
        snapshot_json={
            "measures": [
                {"id": str(uuid.uuid4()), "name": "revenue", "default_agg": "sum",
                 "measure_type": "standard", "data_type": "numeric", "is_additive": True}
            ],
            "dimensions": [],
            "columns": [],
            "hierarchies": [],
        }
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=version)

    first = await resolve_deployed_shape(model, db)
    second = await resolve_deployed_shape(model, db)
    assert first is second  # cached, only one db.get
    assert db.get.await_count == 1

    # Deploying a new version changes the key → fresh resolution.
    model.deployed_version_id = uuid.uuid4()
    await resolve_deployed_shape(model, db)
    assert db.get.await_count == 2


@pytest.mark.asyncio
async def test_resolve_live_metadata_bundle_caches_per_version():
    # F-003-14: the live-load fallback bundle is loaded once per
    # (model_id, deployed_version_id) and self-invalidates on re-deploy.
    snapshot_resolver.invalidate_live_metadata()
    model_id = uuid.uuid4()
    version_id = uuid.uuid4()
    model = types.SimpleNamespace(id=model_id, deployed_version_id=version_id)
    db = AsyncMock()
    db.expunge = lambda obj: None  # sync no-op (real AsyncSession.expunge is sync)

    calls = {"n": 0}

    async def _loader():
        calls["n"] += 1
        return LiveMetadataBundle(
            measures=[], dimensions=[], hierarchy_levels=[],
            hidden_column_ids=set(),
            physical_columns_visible={"a"}, physical_columns_all={"a", "b"},
        )

    first = await resolve_live_metadata_bundle(model, db, loader=_loader)
    second = await resolve_live_metadata_bundle(model, db, loader=_loader)
    assert first is second              # cache hit: same object
    assert calls["n"] == 1              # loader ran only once
    assert first.physical_columns_visible == {"a"}
    assert first.physical_columns_all == {"a", "b"}

    # Re-deploy → new version id → fresh load (no stale bundle).
    model.deployed_version_id = uuid.uuid4()
    await resolve_live_metadata_bundle(model, db, loader=_loader)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_resolve_live_metadata_bundle_none_without_deploy_pointer():
    # No deploy pointer → not cached (binder gate normally precludes this,
    # but the function must not key a cache on a None version).
    snapshot_resolver.invalidate_live_metadata()
    model = types.SimpleNamespace(id=uuid.uuid4(), deployed_version_id=None)

    async def _loader():  # pragma: no cover - must not be called
        raise AssertionError("loader must not run without a deploy pointer")

    assert await resolve_live_metadata_bundle(model, AsyncMock(), loader=_loader) is None


def test_hierarchy_levels_from_snapshot():
    shape = DeployedShape(
        measures=[], dimensions=[], hidden_column_ids=set(),
        physical_columns_all=set(), physical_columns_visible=set(),
        hierarchy_rows=[
            {
                "id": str(uuid.uuid4()),
                "name": "Geo",
                "dimension_kind": "standard",
                "levels": [
                    {
                        "id": str(uuid.uuid4()),
                        "name": "Country",
                        "ordinal": 0,
                        "key_attribute_source": "physical_column",
                        "key_attribute_id": str(uuid.uuid4()),
                    }
                ],
            }
        ],
    )
    levels = hierarchy_level_dimensions_from_snapshot(shape)
    names = {l.name for l in levels}
    # Qualified + bare alias (unique bare name).
    assert "Geo.Country" in names
    assert "Country" in names


# ---------------------------------------------------------------------------
# Binder pins to the deployed snapshot, not the live draft
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_binder_serves_deployed_snapshot_not_live_draft():
    """The concrete F-013-01 failure: a deployed model whose live measure was
    edited must still resolve the DEPLOYED measure expression."""
    from src.semantic.binder import bind_query_to_model

    model_id = uuid.uuid4()
    version_id = uuid.uuid4()
    model = types.SimpleNamespace(
        id=model_id, slug="m", display_name="M",
        deployed_version_id=version_id,
    )

    # Pinned measure: the deployed expression.
    pinned_measure = types.SimpleNamespace(
        id=uuid.uuid4(), name="revenue", default_agg="sum",
        measure_type="standard", expression="SUM(amount)", variant_kind=None,
        is_invalid=False, source_column_id=None,
        cross_model_source_model_id=None,
    )
    shape = DeployedShape(
        measures=[pinned_measure], dimensions=[], hidden_column_ids=set(),
        physical_columns_all=set(), physical_columns_visible=set(),
        hierarchy_rows=[],
    )

    # The LIVE loader would return a DIFFERENT (edited) expression — if the
    # binder ever reads it, this measure leaks into the result.
    leaked_measure = types.SimpleNamespace(
        id=uuid.uuid4(), name="revenue", default_agg="sum",
        measure_type="standard", expression="SUM(amount) - SUM(discount)",
        variant_kind=None, is_invalid=False, source_column_id=None,
        cross_model_source_model_id=None,
    )

    db = AsyncMock()
    with (
        patch("src.semantic.binder._load_model", new=AsyncMock(return_value=model)),
        patch("src.semantic.binder.resolve_deployed_shape", new=AsyncMock(return_value=shape)),
        patch("src.semantic.binder._load_measures", new=AsyncMock(return_value=[leaked_measure])),
        patch("src.semantic.binder._load_dimensions", new=AsyncMock(return_value=[])),
        patch(
            "src.semantic.binder._load_hierarchy_level_dimensions",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "src.semantic.binder._load_hidden_column_ids",
            new=AsyncMock(return_value=set()),
        ),
    ):
        bound = await bind_query_to_model(_lq(), db)
        from src.semantic import binder as _binder_mod
        # The live loader must never be consulted when a deployed shape exists.
        _binder_mod._load_measures.assert_not_awaited()

    resolved = bound.resolved_measures
    assert len(resolved) == 1
    assert resolved[0].expression == "SUM(amount)", (
        "binder must resolve the DEPLOYED measure expression, not the live "
        "draft edit"
    )
