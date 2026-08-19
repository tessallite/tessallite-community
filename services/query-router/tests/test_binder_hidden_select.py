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
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

from src.ir.logical_query import (
    CrossModelNotResolvedError,
    LogicalFilter,
    LogicalQuery,
    SemanticBindingError,
)
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
             hidden_col_ids, hierarchy_levels=None, columns_by_id=None):
    """Return an ExitStack context manager that applies mock patches.

    Bug-7979: the model is deployed (deployed_version_id="v1"), so
    resolve_deployed_shape must return a real DeployedShape to exercise the
    fail-closed deployed path. The shape is built from the test's fixture
    data (measures, dimensions, hidden_column_ids).
    """
    from src.semantic.snapshot_resolver import DeployedShape

    model = _model()
    all_dims = visible_dims + hidden_dims
    all_measures = visible_measures + hidden_measures

    # Build columns_by_id from all semantic objects' source_column_ids so the
    # F-003-05 type-map path can resolve data types from the deployed snapshot.
    _cols: dict[str, dict] = dict(columns_by_id or {})
    for obj in (*all_dims, *all_measures):
        scid = getattr(obj, "source_column_id", None)
        if scid and str(scid) not in _cols:
            _cols[str(scid)] = {"id": str(scid), "column_name": obj.name, "data_type": "text"}

    # Build hierarchy_rows from hierarchy_levels so
    # hierarchy_level_dimensions_from_snapshot can produce virtual dimensions.
    h_rows = []
    if hierarchy_levels:
        # Wrap hierarchy_levels into a single hierarchy row with levels.
        h_rows = [{
            "id": "h1", "name": "TestHierarchy", "dimension_kind": None,
            "levels": [
                {
                    "id": f"hlevel-{i}",
                    "name": getattr(hl, "name", f"level_{i}"),
                    "ordinal": i,
                    "key_attribute_source": "physical_column",
                    "key_attribute_id": str(getattr(hl, "source_column_id", "")),
                }
                for i, hl in enumerate(hierarchy_levels)
            ],
        }]
        # Ensure hierarchy-level columns are in columns_by_id too.
        for hl in hierarchy_levels:
            scid = getattr(hl, "source_column_id", None)
            if scid and str(scid) not in _cols:
                _cols[str(scid)] = {
                    "id": str(scid), "column_name": getattr(hl, "name", ""),
                    "data_type": "text",
                }

    shape = DeployedShape(
        measures=list(all_measures),
        dimensions=list(all_dims),
        hidden_column_ids=set(hidden_col_ids),
        physical_columns_all=set(),
        physical_columns_visible=set(),
        hierarchy_rows=h_rows,
        columns_by_id=_cols,
    )
    stack = ExitStack()
    stack.enter_context(patch("src.semantic.binder._load_model",
                              new=AsyncMock(return_value=model)))
    stack.enter_context(patch("src.semantic.binder.resolve_deployed_shape",
                              new=AsyncMock(return_value=shape)))
    return stack


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
    with patches:
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
    with patches:
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
    with patches:
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
    with patches:
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
    with patches:
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
    with patches:
        with pytest.raises(SemanticBindingError, match="Unknown column"):
            await bind_query_to_model(
                _query(requested_dimensions=["does_not_exist"],
                       requested_measures=[],
                       raw_query="SELECT does_not_exist FROM modely"),
                db, include_hidden=False,
            )


async def test_hidden_measure_in_where_resolves():
    """Bug-6087: a hidden measure referenced in a WHERE predicate must bind.

    Visible measures and hidden dimensions are already allowed in WHERE;
    ``is_hidden`` is curation, not access control, so a hidden measure must
    not be rejected as an unknown filter column."""
    patches = _patches(
        visible_dims=[VISIBLE_DIM], hidden_dims=[],
        visible_measures=[VISIBLE_MEASURE], hidden_measures=[HIDDEN_MEASURE],
        hidden_col_ids=HIDDEN_IDS,
    )
    db = AsyncMock()
    with patches:
        bound = await bind_query_to_model(
            _query(
                requested_dimensions=["region"],
                requested_measures=["revenue"],
                filters=[LogicalFilter("debug_cost", "gt", 100)],
                raw_query="SELECT region, SUM(revenue) FROM modely "
                          "WHERE debug_cost > 100 GROUP BY region",
            ),
            db, include_hidden=False,
        )
    filter_names = {f.dimension_name for f in bound.resolved_filters}
    assert "debug_cost" in filter_names


async def test_hidden_dim_where_literal_is_typed():
    """Bug-6088: a hidden dimension used in WHERE must have its source-column
    data type resolved into ``dim_type_by_name`` so the aggregate route can
    type the literal (previously only visible dims were typed, so a hidden-dim
    filter literal defaulted to string quoting and lost the aggregate match).

    F-003-05: type resolution now reads from deployed_shape.columns_by_id
    instead of live ModelColumn rows."""
    patches = _patches(
        visible_dims=[VISIBLE_DIM], hidden_dims=[HIDDEN_DIM],
        visible_measures=[VISIBLE_MEASURE], hidden_measures=[],
        hidden_col_ids=HIDDEN_IDS,
        columns_by_id={
            HIDDEN_COL_ID: {"id": HIDDEN_COL_ID, "column_name": "channel_code", "data_type": "integer"},
        },
    )
    db = AsyncMock()
    with patches:
        bound = await bind_query_to_model(
            _query(
                requested_dimensions=["region"],
                requested_measures=["revenue"],
                filters=[LogicalFilter("channel_code", "eq", 7)],
                raw_query="SELECT region, SUM(revenue) FROM modely "
                          "WHERE channel_code = 7 GROUP BY region",
            ),
            db, include_hidden=False,
        )
    assert bound.dim_type_by_name.get("channel_code") == "integer"


async def test_hierarchy_level_dim_where_literal_is_typed():
    """Bug-6088 (regression guard): a hierarchy-level dimension used in WHERE
    must keep its data type in ``dim_type_by_name``. Hierarchy levels are
    merged into ``dimension_map`` but are NOT in ``_all_dim_map_for_filter``,
    so typing must iterate both maps — using only the latter would drop
    hierarchy-level typing and regress aggregate-route literal typing."""
    HLEVEL_COL_ID = "col-hlevel"
    hlevel = types.SimpleNamespace(
        name="fiscal_quarter", source_column_id=HLEVEL_COL_ID,
    )
    patches = _patches(
        visible_dims=[VISIBLE_DIM], hidden_dims=[],
        visible_measures=[VISIBLE_MEASURE], hidden_measures=[],
        hidden_col_ids=set(), hierarchy_levels=[hlevel],
        columns_by_id={
            HLEVEL_COL_ID: {"id": HLEVEL_COL_ID, "column_name": "fiscal_quarter", "data_type": "integer"},
        },
    )
    db = AsyncMock()
    with patches:
        bound = await bind_query_to_model(
            _query(
                requested_dimensions=["region"],
                requested_measures=["revenue"],
                filters=[LogicalFilter("fiscal_quarter", "eq", 3)],
                raw_query="SELECT region, SUM(revenue) FROM modely "
                          "WHERE fiscal_quarter = 3 GROUP BY region",
            ),
            db, include_hidden=False,
        )
    assert bound.dim_type_by_name.get("fiscal_quarter") == "integer"


async def test_cross_model_measure_id_only_raises():
    """Bug-6237: a measure carrying only ``cross_model_source_measure_id``
    (no model id) must fail loud as unresolved cross-model, not silently bind
    as a local measure (it has no local source column to render)."""
    cross_measure = types.SimpleNamespace(
        name="external_kpi", default_agg="sum", is_additive=True,
        source_column_id=None,
        cross_model_source_model_id=None,
        cross_model_source_measure_id="meas-in-other-model",
    )
    patches = _patches(
        visible_dims=[VISIBLE_DIM], hidden_dims=[],
        visible_measures=[VISIBLE_MEASURE, cross_measure], hidden_measures=[],
        hidden_col_ids=set(),
    )
    db = AsyncMock()
    with patches:
        with pytest.raises(CrossModelNotResolvedError):
            await bind_query_to_model(
                _query(
                    requested_dimensions=[],
                    requested_measures=["external_kpi"],
                    raw_query="SELECT SUM(external_kpi) FROM modely",
                ),
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
    with patches:
        bound = await bind_query_to_model(
            _query(requested_dimensions=[],
                   requested_measures=["channel_code"],
                   raw_query="SELECT COUNT(DISTINCT channel_code) FROM modely"),
            db, include_hidden=False,
        )
    measure_names = {m.name for m in bound.resolved_measures}
    assert "channel_code" in measure_names
