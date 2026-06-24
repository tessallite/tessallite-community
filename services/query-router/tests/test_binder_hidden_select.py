"""Bug-5381: explicit SELECT of curation-hidden columns must resolve.

Hidden (``is_hidden=True``) columns are curation, not access control.
``SELECT *`` hides them (business view), but an explicit
``SELECT hidden_col FROM model`` must bind — the persona scope gate
enforces actual access downstream.

These tests exercise the fallback maps added in the binder's explicit
SELECT resolution path (Bug-5381 / Bug-3592).
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

from src.ir.logical_query import LogicalQuery, SemanticBindingError
from src.semantic.binder import bind_query_to_model

import pytest


def _query(**overrides) -> LogicalQuery:
    base = dict(
        model_id="model-1",
        protocol="jdbc",
        raw_query="SELECT channel_code FROM modely",
        requested_measures=[],
        requested_dimensions=["channel_code"],
        filters=[],
        grain=[],
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint="fp",
        select_star=False,
    )
    base.update(overrides)
    return LogicalQuery(**base)


def _model():
    return types.SimpleNamespace(
        id="model-1", slug="modely", display_name="Model Y",
        deployed_version_id="v1", project=None,
    )


def _patches(*, visible_dims, hidden_dims, visible_measures, hidden_measures,
             hidden_col_ids):
    """Return a list of mock patches for the binder's internal loaders."""
    model = _model()
    all_dims = visible_dims + hidden_dims
    all_measures = visible_measures + hidden_measures
    return [
        patch("src.semantic.binder._load_model",
              new=AsyncMock(return_value=model)),
        patch("src.semantic.binder.resolve_deployed_shape",
              new=AsyncMock(return_value=None)),
        patch("src.semantic.binder.resolve_live_metadata_bundle",
              new=AsyncMock(return_value=None)),
        patch("src.semantic.binder._load_measures",
              new=AsyncMock(return_value=all_measures)),
        patch("src.semantic.binder._load_dimensions",
              new=AsyncMock(return_value=all_dims)),
        patch("src.semantic.binder._load_hierarchy_level_dimensions",
              new=AsyncMock(return_value=[])),
        patch("src.semantic.binder._load_hidden_column_ids",
              new=AsyncMock(return_value=hidden_col_ids)),
        patch("src.semantic.binder._load_physical_column_names",
              new=AsyncMock(return_value=set())),
    ]


# ---- Fixtures ----

VISIBLE_COL_ID = "col-visible"
HIDDEN_COL_ID = "col-hidden"
HIDDEN_MEASURE_COL_ID = "col-hidden-measure"

VISIBLE_DIM = types.SimpleNamespace(name="region", source_column_id=VISIBLE_COL_ID)
HIDDEN_DIM = types.SimpleNamespace(name="channel_code", source_column_id=HIDDEN_COL_ID)
VISIBLE_MEASURE = types.SimpleNamespace(
    name="revenue", default_agg="sum", is_additive=True,
    source_column_id=VISIBLE_COL_ID,
)
HIDDEN_MEASURE = types.SimpleNamespace(
    name="debug_cost", default_agg="sum", is_additive=True,
    source_column_id=HIDDEN_MEASURE_COL_ID,
)

HIDDEN_IDS = {HIDDEN_COL_ID, HIDDEN_MEASURE_COL_ID}


# ---- Tests ----

async def test_explicit_select_hidden_dimension_resolves_business_view():
    """Bug-5381: SELECT channel_code must resolve even with include_hidden=False."""
    patches = _patches(
        visible_dims=[VISIBLE_DIM], hidden_dims=[HIDDEN_DIM],
        visible_measures=[VISIBLE_MEASURE], hidden_measures=[],
        hidden_col_ids=HIDDEN_IDS,
    )
    db = AsyncMock()
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         patches[5], patches[6], patches[7]:
        bound = await bind_query_to_model(
            _query(requested_dimensions=["channel_code"],
                   requested_measures=[]),
            db, include_hidden=False,
        )
    dim_names = {d.name for d in bound.resolved_dimensions}
    assert "channel_code" in dim_names


async def test_explicit_select_hidden_measure_resolves_business_view():
    """Bug-5381: SELECT SUM(debug_cost) must resolve even with include_hidden=False."""
    patches = _patches(
        visible_dims=[VISIBLE_DIM], hidden_dims=[],
        visible_measures=[VISIBLE_MEASURE], hidden_measures=[HIDDEN_MEASURE],
        hidden_col_ids=HIDDEN_IDS,
    )
    db = AsyncMock()
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         patches[5], patches[6], patches[7]:
        bound = await bind_query_to_model(
            _query(requested_dimensions=[],
                   requested_measures=["debug_cost"],
                   raw_query="SELECT SUM(debug_cost) FROM modely"),
            db, include_hidden=False,
        )
    measure_names = {m.name for m in bound.resolved_measures}
    assert "debug_cost" in measure_names


async def test_select_star_still_hides_hidden_columns():
    """SELECT * must NOT include hidden columns when include_hidden=False."""
    patches = _patches(
        visible_dims=[VISIBLE_DIM], hidden_dims=[HIDDEN_DIM],
        visible_measures=[VISIBLE_MEASURE], hidden_measures=[HIDDEN_MEASURE],
        hidden_col_ids=HIDDEN_IDS,
    )
    db = AsyncMock()
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         patches[5], patches[6], patches[7]:
        bound = await bind_query_to_model(
            _query(select_star=True, requested_dimensions=[],
                   requested_measures=[], raw_query="SELECT * FROM modely"),
            db, include_hidden=False,
        )
    dim_names = {d.name for d in bound.resolved_dimensions}
    measure_names = {m.name for m in bound.resolved_measures}
    assert "channel_code" not in dim_names
    assert "debug_cost" not in measure_names
    assert "region" in dim_names
    assert "revenue" in measure_names


async def test_technical_view_resolves_hidden_columns_directly():
    """With include_hidden=True, hidden columns resolve from the primary maps."""
    patches = _patches(
        visible_dims=[VISIBLE_DIM], hidden_dims=[HIDDEN_DIM],
        visible_measures=[VISIBLE_MEASURE], hidden_measures=[HIDDEN_MEASURE],
        hidden_col_ids=HIDDEN_IDS,
    )
    db = AsyncMock()
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         patches[5], patches[6], patches[7]:
        bound = await bind_query_to_model(
            _query(requested_dimensions=["channel_code"],
                   requested_measures=["debug_cost"],
                   raw_query="SELECT channel_code, SUM(debug_cost) FROM modely"),
            db, include_hidden=True,
        )
    dim_names = {d.name for d in bound.resolved_dimensions}
    measure_names = {m.name for m in bound.resolved_measures}
    assert "channel_code" in dim_names
    assert "debug_cost" in measure_names


async def test_case_insensitive_hidden_dimension_resolves():
    """Bug-5381: case-insensitive fallback for hidden columns in SELECT."""
    patches = _patches(
        visible_dims=[VISIBLE_DIM], hidden_dims=[HIDDEN_DIM],
        visible_measures=[VISIBLE_MEASURE], hidden_measures=[],
        hidden_col_ids=HIDDEN_IDS,
    )
    db = AsyncMock()
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         patches[5], patches[6], patches[7]:
        bound = await bind_query_to_model(
            _query(requested_dimensions=["CHANNEL_CODE"],
                   requested_measures=[],
                   raw_query="SELECT CHANNEL_CODE FROM modely"),
            db, include_hidden=False,
        )
    dim_names = {d.name for d in bound.resolved_dimensions}
    assert "channel_code" in dim_names


async def test_truly_unknown_column_still_raises():
    """A column that is neither visible nor hidden must still raise."""
    patches = _patches(
        visible_dims=[VISIBLE_DIM], hidden_dims=[HIDDEN_DIM],
        visible_measures=[VISIBLE_MEASURE], hidden_measures=[],
        hidden_col_ids=HIDDEN_IDS,
    )
    db = AsyncMock()
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         patches[5], patches[6], patches[7]:
        with pytest.raises(SemanticBindingError, match="Unknown column"):
            await bind_query_to_model(
                _query(requested_dimensions=["does_not_exist"],
                       requested_measures=[],
                       raw_query="SELECT does_not_exist FROM modely"),
                db, include_hidden=False,
            )


async def test_hidden_dim_as_count_distinct_measure_resolves():
    """Bug-5381: COUNT(DISTINCT hidden_dim) must resolve as a synthetic measure."""
    patches = _patches(
        visible_dims=[VISIBLE_DIM], hidden_dims=[HIDDEN_DIM],
        visible_measures=[VISIBLE_MEASURE], hidden_measures=[],
        hidden_col_ids=HIDDEN_IDS,
    )
    db = AsyncMock()
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         patches[5], patches[6], patches[7]:
        bound = await bind_query_to_model(
            _query(requested_dimensions=[],
                   requested_measures=["channel_code"],
                   raw_query="SELECT COUNT(DISTINCT channel_code) FROM modely"),
            db, include_hidden=False,
        )
    measure_names = {m.name for m in bound.resolved_measures}
    assert "channel_code" in measure_names
