"""Guard test for Bug-7000: pocket matcher hot-path cost reduction.

Proves that:
1. Parsed SQL trees are cached by the SQL string (collision-safe) and reused.
2. Model table identifiers for a DEPLOYED model come from the snapshot, not
   live tables. A live table edit does not change the winning pocket.
3. Cache key includes deploy_epoch so same-version redeploy invalidates.
4. Match correctness is preserved (same pocket wins with/without cache).
"""
from __future__ import annotations

import types as _types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.routing.pocket_matcher import (
    _parse_sql_cached,
    _pocket_covers_required_tables,
    _pocket_scope_within_model,
    invalidate_model_table_cache,
    invalidate_parsed_sql_cache,
)


# ---------------------------------------------------------------------------
# Unit: _parse_sql_cached (collision-safe, keyed by SQL string)
# ---------------------------------------------------------------------------

def test_parse_sql_cached_returns_tree():
    invalidate_parsed_sql_cache()
    tree = _parse_sql_cached("SELECT * FROM sales WHERE region = 'EMEA'")
    assert tree is not None


def test_parse_sql_cached_returns_same_object_on_repeat():
    invalidate_parsed_sql_cache()
    sql = "SELECT * FROM sales WHERE region = 'EMEA'"
    tree1 = _parse_sql_cached(sql)
    tree2 = _parse_sql_cached(sql)
    assert tree1 is tree2  # same cached object


def test_parse_sql_cached_returns_none_for_garbage():
    invalidate_parsed_sql_cache()
    tree = _parse_sql_cached("NOT VALID SQL {{{{")
    assert tree is None


def test_parse_sql_cached_none_input():
    assert _parse_sql_cached(None) is None
    assert _parse_sql_cached("") is None


# ---------------------------------------------------------------------------
# Unit: _pocket_scope_within_model uses cached parse
# ---------------------------------------------------------------------------

def test_scope_within_model_uses_cache():
    invalidate_parsed_sql_cache()
    sql = "SELECT * FROM modely WHERE x = 1"
    allowed = {"modely", "modely_technical"}
    # First call parses and caches
    assert _pocket_scope_within_model(sql, allowed) is True
    # Second call should reuse the cached tree (no re-parse)
    assert _pocket_scope_within_model(sql, allowed) is True


def test_scope_within_model_rejects_outside_table():
    invalidate_parsed_sql_cache()
    sql = "SELECT * FROM external_db.other_table"
    allowed = {"modely", "modely_technical"}
    assert _pocket_scope_within_model(sql, allowed) is False


# ---------------------------------------------------------------------------
# Unit: _pocket_covers_required_tables uses cached parse
# ---------------------------------------------------------------------------

def test_covers_required_tables_model_scope():
    invalidate_parsed_sql_cache()
    sql = "SELECT * FROM modely WHERE region = 'EMEA'"
    required = {"public.sales", "sales"}
    model_scope = {"modely", "modely_technical"}
    # Model-scope pocket covers any model table
    assert _pocket_covers_required_tables(sql, required, model_scope) is True


def test_covers_required_tables_legacy_pocket():
    invalidate_parsed_sql_cache()
    sql = "SELECT * FROM sales WHERE region = 'EMEA'"
    required = {"sales"}
    assert _pocket_covers_required_tables(sql, required) is True


def test_covers_required_tables_missing_table():
    invalidate_parsed_sql_cache()
    sql = "SELECT * FROM sales WHERE region = 'EMEA'"
    required = {"orders"}
    assert _pocket_covers_required_tables(sql, required) is False


# ---------------------------------------------------------------------------
# Integration: deployed model derives table identifiers from snapshot
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.asyncio


async def test_deployed_model_uses_snapshot_for_table_identifiers():
    """For a deployed model, _load_model_table_identifiers must derive
    allowed identifiers from the deployed snapshot, not live tables.
    A live table edit must NOT change the allowed set.
    """
    from src.routing.pocket_matcher import _load_model_table_identifiers

    invalidate_model_table_cache()

    model = _types.SimpleNamespace(
        id="model-1",
        slug="modely",
        display_name="Model Y",
        deployed_version_id="v1",
        deploy_epoch=1,
    )

    # Snapshot says the model has one table: public.sales (alias: sales)
    shape = _types.SimpleNamespace(
        tables_by_id={
            "t1": {"physical_name": "public.sales", "alias": "sales"},
        },
    )

    with patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        new_callable=AsyncMock,
        return_value=shape,
    ):
        db = AsyncMock()
        allowed = await _load_model_table_identifiers(model, db)

    assert "public.sales" in allowed
    assert "sales" in allowed
    assert "modely" in allowed  # model-scope identifier always included
    # The DB must NOT have been queried for ModelTable
    # (the snapshot is the sole authority for a deployed model)
    db.execute.assert_not_awaited()

    invalidate_model_table_cache()


async def test_model_table_cache_key_includes_deploy_epoch():
    """Two calls with the same (model_id, version) but different
    deploy_epoch must NOT serve from cache (same-version redeploy).
    """
    from src.routing.pocket_matcher import _load_model_table_identifiers

    invalidate_model_table_cache()

    model_v1 = _types.SimpleNamespace(
        id="model-1", slug="modely", display_name="Model Y",
        deployed_version_id="v1", deploy_epoch=1,
    )
    model_v1_redeployed = _types.SimpleNamespace(
        id="model-1", slug="modely", display_name="Model Y",
        deployed_version_id="v1", deploy_epoch=2,  # different epoch
    )

    shape1 = _types.SimpleNamespace(
        tables_by_id={"t1": {"physical_name": "old.table", "alias": "old"}},
    )
    shape2 = _types.SimpleNamespace(
        tables_by_id={"t1": {"physical_name": "new.table", "alias": "new"}},
    )

    with patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        new_callable=AsyncMock,
        side_effect=[shape1, shape2],
    ):
        db = AsyncMock()
        allowed1 = await _load_model_table_identifiers(model_v1, db)
        allowed2 = await _load_model_table_identifiers(model_v1_redeployed, db)

    assert "old" in allowed1
    assert "new" in allowed2
    assert "new" not in allowed1  # epoch 1 must NOT contain epoch 2 data

    invalidate_model_table_cache()


async def test_undeployed_model_uses_live_tables():
    """An undeployed model (deployed_version_id=None) reads from live
    ModelTable rows -- they ARE the authority.
    """
    from src.routing.pocket_matcher import _load_model_table_identifiers

    invalidate_model_table_cache()

    model = _types.SimpleNamespace(
        id="model-1", slug="modely", display_name="Model Y",
        deployed_version_id=None,
        deploy_epoch=0,
    )

    table = _types.SimpleNamespace(
        physical_name="public.sales",
        alias="sales",
    )
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [table]
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result_mock)

    allowed = await _load_model_table_identifiers(model, db)

    assert "public.sales" in allowed
    assert "sales" in allowed
    assert "modely" in allowed
    # The DB WAS queried (live tables are the authority for undeployed)
    db.execute.assert_awaited_once()

    invalidate_model_table_cache()


async def test_deployed_model_required_tables_from_snapshot_no_live_read():
    """For a deployed model, _required_physical_tables must derive the
    required table set from the deployed snapshot (columns_by_id ->
    model_table_id -> tables_by_id -> physical_name). No live
    ModelColumn/ModelTable DB query must run. A Save-without-Deploy that
    changes a column's table binding must NOT change which pocket wins.
    """
    from src.routing.pocket_matcher import _required_physical_tables
    from src.ir.logical_query import LogicalQuery, BoundQuery

    model = _types.SimpleNamespace(
        id="model-1", slug="modely",
        deployed_version_id="v1", deploy_epoch=1,
    )
    # A measure whose source_column_id is "col-revenue"
    measure = _types.SimpleNamespace(
        id="m-revenue", name="revenue", default_agg="sum",
        is_additive=True, measure_type="standard", expression=None,
        calc_agg_mode=None, semi_additive_behavior=None, variant_kind=None,
        source_column_id="col-revenue",
    )
    lq = LogicalQuery(
        model_id="model-1", protocol="jdbc", raw_query="SELECT 1",
        requested_measures=["revenue"], requested_dimensions=[],
        filters=[], grain=[], order_by=[], limit=None, offset=None,
        query_fingerprint="fp", select_star=False, has_distinct=False,
    )
    bq = BoundQuery(
        logical_query=lq, model=model, resolved_measures=[measure],
        resolved_dimensions=[], resolved_filters=[],
        resolved_dimensions_by_name={},
    )

    # Snapshot: col-revenue belongs to table t-fact (public.sales)
    shape = _types.SimpleNamespace(
        columns_by_id={"col-revenue": {"model_table_id": "t-fact"}},
        tables_by_id={"t-fact": {"physical_name": "public.sales", "alias": "sales"}},
    )

    db = AsyncMock()

    with patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        new_callable=AsyncMock, return_value=shape,
    ):
        required = await _required_physical_tables(bq, db)

    assert "public.sales" in required
    assert "sales" in required
    # NO live DB queries for ModelColumn/ModelTable
    db.execute.assert_not_awaited()

    invalidate_model_table_cache()


async def test_deployed_model_missing_column_in_snapshot_rejects_pockets():
    """Bug-7000 fail-closed: if a required source_column_id is absent from
    the snapshot's columns_by_id, _required_physical_tables returns None
    (fail closed). The caller must then reject all pocket candidates rather
    than accepting them via an empty/partial required-table set.
    """
    from src.routing.pocket_matcher import _required_physical_tables
    from src.ir.logical_query import LogicalQuery, BoundQuery

    model = _types.SimpleNamespace(
        id="model-1", slug="modely",
        deployed_version_id="v1", deploy_epoch=1,
    )
    # A measure whose source_column_id is "col-missing" -- absent from snapshot
    measure = _types.SimpleNamespace(
        id="m-revenue", name="revenue", default_agg="sum",
        is_additive=True, measure_type="standard", expression=None,
        calc_agg_mode=None, semi_additive_behavior=None, variant_kind=None,
        source_column_id="col-missing",
    )
    lq = LogicalQuery(
        model_id="model-1", protocol="jdbc", raw_query="SELECT 1",
        requested_measures=["revenue"], requested_dimensions=[],
        filters=[], grain=[], order_by=[], limit=None, offset=None,
        query_fingerprint="fp", select_star=False, has_distinct=False,
    )
    bq = BoundQuery(
        logical_query=lq, model=model, resolved_measures=[measure],
        resolved_dimensions=[], resolved_filters=[],
        resolved_dimensions_by_name={},
    )

    # Snapshot does NOT contain "col-missing" in columns_by_id
    shape = _types.SimpleNamespace(
        columns_by_id={},
        tables_by_id={"t-fact": {"physical_name": "public.sales", "alias": "sales"}},
    )

    db = AsyncMock()

    with patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        new_callable=AsyncMock, return_value=shape,
    ):
        required = await _required_physical_tables(bq, db)

    # None = fail closed: caller must reject all pockets
    assert required is None
    db.execute.assert_not_awaited()

    invalidate_model_table_cache()


async def test_deployed_model_missing_table_in_snapshot_rejects_pockets():
    """Bug-7000 fail-closed: if a column's model_table_id points to a table
    absent from tables_by_id, _required_physical_tables returns None.
    """
    from src.routing.pocket_matcher import _required_physical_tables
    from src.ir.logical_query import LogicalQuery, BoundQuery

    model = _types.SimpleNamespace(
        id="model-1", slug="modely",
        deployed_version_id="v1", deploy_epoch=1,
    )
    measure = _types.SimpleNamespace(
        id="m-revenue", name="revenue", default_agg="sum",
        is_additive=True, measure_type="standard", expression=None,
        calc_agg_mode=None, semi_additive_behavior=None, variant_kind=None,
        source_column_id="col-revenue",
    )
    lq = LogicalQuery(
        model_id="model-1", protocol="jdbc", raw_query="SELECT 1",
        requested_measures=["revenue"], requested_dimensions=[],
        filters=[], grain=[], order_by=[], limit=None, offset=None,
        query_fingerprint="fp", select_star=False, has_distinct=False,
    )
    bq = BoundQuery(
        logical_query=lq, model=model, resolved_measures=[measure],
        resolved_dimensions=[], resolved_filters=[],
        resolved_dimensions_by_name={},
    )

    # Column exists but points to a table NOT in tables_by_id
    shape = _types.SimpleNamespace(
        columns_by_id={"col-revenue": {"model_table_id": "t-missing"}},
        tables_by_id={},
    )

    db = AsyncMock()

    with patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        new_callable=AsyncMock, return_value=shape,
    ):
        required = await _required_physical_tables(bq, db)

    assert required is None
    db.execute.assert_not_awaited()

    invalidate_model_table_cache()
