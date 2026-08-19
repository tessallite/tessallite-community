"""Guard test for Bug-6977: canonical dimension equivalence must be derived
from the deployed snapshot, not from live editable tables.

Proves that for a DEPLOYED model the aggregate matcher:
1. Uses the deployed snapshot as the SOLE authority for canonical dims.
2. Returns EMPTY (fail closed) when the snapshot has no dimensions -- does
   NOT fall back to live tables (that would re-open the bug).
3. Returns EMPTY (fail closed) when snapshot resolution raises -- does NOT
   fall back to live tables.
4. Falls back to live tables ONLY for genuinely undeployed models
   (deployed_version_id is None).
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from shared.semantic.canonical_dimensions import (
    CanonicalDim,
    build_canonical_dimension_list_from_snapshot,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Unit: build_canonical_dimension_list_from_snapshot
# ---------------------------------------------------------------------------

def _dim(name, src_col_id=None, uda_id=None, is_time=False):
    return types.SimpleNamespace(
        id=str(uuid.uuid4()),
        name=name,
        source_column_id=src_col_id,
        user_defined_attribute_id=uda_id,
        is_time_dim=is_time,
    )


def test_flat_dims_merge_by_backing_key():
    col_id = uuid.uuid4()
    dims = [
        _dim("region", src_col_id=col_id),
        _dim("area", src_col_id=col_id),
    ]
    result = build_canonical_dimension_list_from_snapshot(dims, [])
    # Two dims sharing the same source_column_id collapse into one entry.
    assert len(result) == 1
    assert result[0].all_names == {"region", "area"}


def test_hierarchy_levels_merge_with_flat_dims():
    col_id = uuid.uuid4()
    dims = [_dim("month", src_col_id=col_id)]
    hierarchies = [{
        "id": str(uuid.uuid4()),
        "name": "Date",
        "dimension_kind": "time",
        "levels": [{
            "id": str(uuid.uuid4()),
            "name": "month",
            "ordinal": 2,
            "key_attribute_source": "physical_column",
            "key_attribute_id": str(col_id),
        }],
    }]
    result = build_canonical_dimension_list_from_snapshot(dims, hierarchies)
    # The hierarchy level merges with the flat dim via the same backing key.
    assert len(result) == 1
    entry = result[0]
    assert "month" in entry.all_names
    assert "Date.month" in entry.all_names
    assert entry.is_time_dim is True


def test_no_key_dims_are_preserved():
    dims = [_dim("calculated_dim")]
    result = build_canonical_dimension_list_from_snapshot(dims, [])
    assert len(result) == 1
    assert result[0].canonical_name == "calculated_dim"


def test_empty_snapshot_returns_empty_list():
    result = build_canonical_dimension_list_from_snapshot([], [])
    assert result == []


# ---------------------------------------------------------------------------
# Integration: _get_canonical_dims_cached uses snapshot (sole authority)
# ---------------------------------------------------------------------------

async def test_matcher_uses_snapshot_not_live_tables():
    """When a deployed snapshot is available, the canonical dimension list
    must be derived from it, not from live DB tables.
    """
    from conftest import make_dimension, make_measure, make_bound_query
    from src.routing.aggregate_matcher import (
        _get_canonical_dims_cached,
        invalidate_canonical_dim_cache,
    )

    invalidate_canonical_dim_cache()

    col_id = uuid.uuid4()
    snapshot_dims = [_dim("region", src_col_id=col_id)]

    shape = types.SimpleNamespace(
        dimensions=snapshot_dims,
        hierarchy_rows=[],
    )

    bq = make_bound_query([make_dimension("region")], [make_measure("revenue")])

    with patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        new_callable=AsyncMock,
        return_value=shape,
    ):
        result = await _get_canonical_dims_cached(bq, AsyncMock())

    assert len(result) == 1
    assert result[0].canonical_name == "region"

    invalidate_canonical_dim_cache()


async def test_deployed_model_empty_snapshot_does_not_read_live_tables():
    """A deployed model whose snapshot has no dimensions must return EMPTY
    (fail closed). It must NOT fall back to live tables -- that would let
    a Save-without-Deploy change routing.
    """
    from conftest import make_dimension, make_measure, make_bound_query
    from src.routing.aggregate_matcher import (
        _get_canonical_dims_cached,
        invalidate_canonical_dim_cache,
    )

    invalidate_canonical_dim_cache()

    # Snapshot with no dimensions and no hierarchies
    shape = types.SimpleNamespace(
        dimensions=[],
        hierarchy_rows=[],
    )

    bq = make_bound_query([make_dimension("region")], [make_measure("revenue")])

    with patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        new_callable=AsyncMock,
        return_value=shape,
    ), patch(
        "shared.semantic.canonical_dimensions.build_canonical_dimension_list",
        new_callable=AsyncMock,
        return_value=[CanonicalDim(
            canonical_name="region",
            backing_key="dim:live-SHOULD-NOT-BE-CALLED",
            all_names={"region"},
        )],
    ) as live_mock:
        result = await _get_canonical_dims_cached(bq, AsyncMock())

    # The live builder must NOT be called for a deployed model.
    live_mock.assert_not_awaited()
    # Empty snapshot = empty canonical dims (the deployed truth).
    assert result == []

    invalidate_canonical_dim_cache()


async def test_deployed_model_snapshot_resolution_failure_fails_closed():
    """When snapshot resolution raises on a deployed model, fail closed
    (return empty). Do NOT fall back to mutable live tables.
    """
    from conftest import make_dimension, make_measure, make_bound_query
    from src.routing.aggregate_matcher import (
        _get_canonical_dims_cached,
        invalidate_canonical_dim_cache,
    )

    invalidate_canonical_dim_cache()

    bq = make_bound_query([make_dimension("region")], [make_measure("revenue")])

    with patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        new_callable=AsyncMock,
        side_effect=RuntimeError("transient DB failure"),
    ), patch(
        "shared.semantic.canonical_dimensions.build_canonical_dimension_list",
        new_callable=AsyncMock,
        return_value=[CanonicalDim(
            canonical_name="region",
            backing_key="dim:live-SHOULD-NOT-BE-CALLED",
            all_names={"region"},
        )],
    ) as live_mock:
        result = await _get_canonical_dims_cached(bq, AsyncMock())

    # The live builder must NOT be called for a deployed model.
    live_mock.assert_not_awaited()
    # Fail closed: empty canonical dims.
    assert result == []

    invalidate_canonical_dim_cache()


async def test_undeployed_model_uses_live_tables():
    """An undeployed model (deployed_version_id=None) uses the live
    table builder -- it IS the authority for undeployed models.
    """
    from src.routing.aggregate_matcher import (
        _get_canonical_dims_cached,
        invalidate_canonical_dim_cache,
    )
    from src.ir.logical_query import LogicalQuery, BoundQuery

    invalidate_canonical_dim_cache()

    model = types.SimpleNamespace(
        id="model-1",
        slug="test_model",
        deployed_version_id=None,  # genuinely undeployed
    )
    dim = types.SimpleNamespace(id="d-region", name="region")
    measure = types.SimpleNamespace(
        id="m-revenue", name="revenue", default_agg="sum",
        is_additive=True, measure_type="standard", expression=None,
        calc_agg_mode=None, semi_additive_behavior=None, variant_kind=None,
    )
    lq = LogicalQuery(
        model_id="model-1", protocol="jdbc", raw_query="SELECT 1",
        requested_measures=["revenue"], requested_dimensions=["region"],
        filters=[], grain=["region"], order_by=[], limit=None, offset=None,
        query_fingerprint="fp", select_star=False, has_distinct=False,
    )
    bq = BoundQuery(
        logical_query=lq, model=model, resolved_measures=[measure],
        resolved_dimensions=[dim], resolved_filters=[],
        resolved_dimensions_by_name={"region": dim},
    )

    with patch(
        "shared.semantic.canonical_dimensions.build_canonical_dimension_list",
        new_callable=AsyncMock,
        return_value=[CanonicalDim(
            canonical_name="region",
            backing_key="dim:live",
            all_names={"region"},
        )],
    ) as live_mock:
        result = await _get_canonical_dims_cached(bq, AsyncMock())

    live_mock.assert_awaited_once()
    assert len(result) == 1
    assert result[0].backing_key == "dim:live"

    invalidate_canonical_dim_cache()
