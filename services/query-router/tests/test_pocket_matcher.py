from __future__ import annotations

import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from conftest import make_bound_query, make_dimension, make_measure
from src.ir.logical_query import LogicalFilter, PocketMatchResult
from src.routing.pocket_matcher import PocketSkipReason, find_best_pocket

pytestmark = pytest.mark.integration


def _result_with(items):
    return types.SimpleNamespace(
        scalars=lambda: types.SimpleNamespace(all=lambda: items),
    )


def _patched_flags(enabled=True, model_enabled=True, require_tenant_filter=True):
    snap = patch("src.routing.pocket_matcher.system_snapshot_get")
    getter = patch("src.routing.pocket_matcher.get_setting", new_callable=AsyncMock)
    return snap, getter, enabled, model_enabled, require_tenant_filter


async def _run(
    bq, db, *, enabled=True, model_enabled=True,
    require_tenant_filter=True, tenant_scope_from_context=False,
):
    with patch("src.routing.pocket_matcher.system_snapshot_get") as snap, patch(
        "src.routing.pocket_matcher.get_setting", new_callable=AsyncMock
    ) as get_setting:
        snap.side_effect = lambda key: {
            "pocket.enabled": enabled,
            "pocket.require_tenant_filter": require_tenant_filter,
            "pocket.tenant_scope_from_context": tenant_scope_from_context,
        }.get(key)
        get_setting.return_value = model_enabled
        return await find_best_pocket(bq, db)


async def test_returns_match_result_with_pocket_on_hit():
    bq = make_bound_query(
        [make_dimension("tenant_id"), make_dimension("country")],
        [make_measure("amount")],
        filters=[
            LogicalFilter("tenant_id", "eq", 12),
            LogicalFilter("country", "eq", "GB"),
        ],
        raw_sql="SELECT amount FROM sales WHERE tenant_id = 12 AND country = 'GB'",
    )
    bq.logical_query.query_fingerprint = "fp-1"

    pocket = types.SimpleNamespace(
        id="pocket-1",
        model_id=bq.model.id,
        status="fresh",
        query_fingerprint="fp-1",
        last_refresh_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        predicates=[
            types.SimpleNamespace(column_name="tenant_id", operator="eq", value_json={"value": 12}),
        ],
    )

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result_with([pocket]))

    result = await _run(bq, db)

    assert isinstance(result, PocketMatchResult)
    assert result.pocket is pocket
    assert result.skipped_reason is None


async def test_pocket_matcher_prefers_more_specific_predicate_subset():
    bq = make_bound_query(
        [make_dimension("tenant_id"), make_dimension("country")],
        [make_measure("amount")],
        filters=[
            LogicalFilter("tenant_id", "eq", 12),
            LogicalFilter("country", "eq", "GB"),
        ],
        raw_sql="SELECT amount FROM sales WHERE tenant_id = 12 AND country = 'GB'",
    )
    bq.logical_query.query_fingerprint = "fp-1"

    less_specific = types.SimpleNamespace(
        id="pocket-1",
        model_id=bq.model.id,
        status="fresh",
        query_fingerprint="fp-1",
        last_refresh_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        predicates=[
            types.SimpleNamespace(column_name="tenant_id", operator="eq", value_json={"value": 12}),
        ],
    )
    more_specific = types.SimpleNamespace(
        id="pocket-2",
        model_id=bq.model.id,
        status="fresh",
        query_fingerprint="fp-1",
        last_refresh_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        predicates=[
            types.SimpleNamespace(column_name="tenant_id", operator="eq", value_json={"value": 12}),
            types.SimpleNamespace(column_name="country", operator="eq", value_json={"value": "GB"}),
        ],
    )

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result_with([less_specific, more_specific]))

    result = await _run(bq, db)

    assert result.pocket is not None
    assert result.pocket.id == "pocket-2"


async def test_tenant_filter_missing_skips_with_named_reason():
    """Bug-085 observability: skip must surface reason=`no_tenant_filter`."""
    bq = make_bound_query(
        [make_dimension("country")],
        [make_measure("amount")],
        filters=[LogicalFilter("country", "eq", "GB")],
        raw_sql="SELECT amount FROM sales WHERE country = 'GB'",
    )
    bq.logical_query.query_fingerprint = "fp-1"

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result_with([]))

    result = await _run(bq, db)

    assert result.pocket is None
    assert result.skipped_reason == PocketSkipReason.NO_TENANT_FILTER


async def test_tenant_scope_from_context_allows_match_without_explicit_filter():
    """Bug-085 follow-up: when `pocket.tenant_scope_from_context=True`, the
    matcher accepts the authenticated session as tenant-scoped even when the
    query body has no explicit tenant predicate.
    """
    bq = make_bound_query(
        [make_dimension("country")],
        [make_measure("amount")],
        filters=[LogicalFilter("country", "eq", "GB")],
        raw_sql="SELECT amount FROM sales WHERE country = 'GB'",
    )
    bq.logical_query.query_fingerprint = "fp-ctx"

    pocket = types.SimpleNamespace(
        id="pocket-ctx",
        model_id=bq.model.id,
        status="fresh",
        query_fingerprint="fp-ctx",
        last_refresh_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        predicates=[],
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result_with([pocket]))

    result = await _run(bq, db, tenant_scope_from_context=True)

    assert result.pocket is pocket
    assert result.skipped_reason is None


async def test_pocket_flag_disabled_skips_with_named_reason():
    bq = make_bound_query(
        [make_dimension("tenant_id")], [make_measure("amount")],
        filters=[LogicalFilter("tenant_id", "eq", 12)],
        raw_sql="SELECT amount FROM sales WHERE tenant_id = 12",
    )
    bq.logical_query.query_fingerprint = "fp-1"
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result_with([]))

    result = await _run(bq, db, enabled=False)

    assert result.pocket is None
    assert result.skipped_reason == PocketSkipReason.FLAG_DISABLED


async def test_unresolvable_order_skips_with_named_reason():
    # F-003-01: an expression ORDER BY cannot be honoured by the pocket route
    # (it serves the cached slice without reproducing the sort), so a top-N over
    # an inexpressible sort must bail to source rather than return wrong rows.
    bq = make_bound_query(
        [make_dimension("tenant_id")], [make_measure("amount")],
        filters=[LogicalFilter("tenant_id", "eq", 12)],
        raw_sql="SELECT amount FROM sales WHERE tenant_id = 12 ORDER BY LOWER(region) DESC",
    )
    bq.logical_query.query_fingerprint = "fp-1"
    bq.logical_query.has_unresolvable_order = True
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result_with([]))

    result = await _run(bq, db)

    assert result.pocket is None
    assert result.skipped_reason == PocketSkipReason.UNRESOLVABLE_ORDER


async def test_model_disabled_skips_with_named_reason():
    bq = make_bound_query(
        [make_dimension("tenant_id")], [make_measure("amount")],
        filters=[LogicalFilter("tenant_id", "eq", 12)],
        raw_sql="SELECT amount FROM sales WHERE tenant_id = 12",
    )
    bq.logical_query.query_fingerprint = "fp-1"
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result_with([]))

    result = await _run(bq, db, model_enabled=False)

    assert result.pocket is None
    assert result.skipped_reason == PocketSkipReason.MODEL_DISABLED


async def test_no_candidates_skips_with_named_reason():
    bq = make_bound_query(
        [make_dimension("tenant_id")], [make_measure("amount")],
        filters=[LogicalFilter("tenant_id", "eq", 12)],
        raw_sql="SELECT amount FROM sales WHERE tenant_id = 12",
    )
    bq.logical_query.query_fingerprint = "fp-1"
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result_with([]))

    result = await _run(bq, db)

    assert result.pocket is None
    assert result.skipped_reason == PocketSkipReason.NO_CANDIDATES


async def test_passthrough_flag_no_longer_blocks_fingerprint_matched_pocket():
    """Bug-087: `has_passthrough_expressions=True` must NOT short-circuit
    the matcher when fingerprint + predicate-subset already match. The
    passthrough flag is for the aggregate path only.
    """
    bq = make_bound_query(
        [make_dimension("tenant_id")],
        [make_measure("amount")],
        filters=[LogicalFilter("tenant_id", "eq", 12)],
        raw_sql="SELECT COUNT(*) FROM (SELECT * FROM sales WHERE tenant_id = 12) q",
    )
    bq.logical_query.query_fingerprint = "fp-match"
    bq.has_passthrough_expressions = True

    pocket = types.SimpleNamespace(
        id="pocket-pt",
        model_id=bq.model.id,
        status="fresh",
        query_fingerprint="fp-match",
        last_refresh_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        predicates=[
            types.SimpleNamespace(column_name="tenant_id", operator="eq", value_json={"value": 12}),
        ],
    )

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result_with([pocket]))

    result = await _run(bq, db)

    assert result.pocket is pocket
    assert result.skipped_reason is None


async def test_count_over_select_star_pocket_matches_via_filter_subset():
    """`SELECT COUNT(1) FROM t WHERE base_amount > 4970` against a
    `SELECT * FROM t WHERE base_amount > 4950` pocket. The shape
    fingerprints diverge (the COUNT synthesises `__row_count`), but
    the pocket's predicate columns are a subset of the query's filter
    columns — match on the serviceable path.
    """
    bq = make_bound_query(
        [make_dimension("tenant_id"), make_dimension("base_amount")],
        [make_measure("amount")],
        filters=[
            LogicalFilter("tenant_id", "eq", 12),
            LogicalFilter("base_amount", "gt", 4970),
        ],
        raw_sql="SELECT COUNT(1) FROM modely WHERE tenant_id = 12 AND base_amount > 4970",
    )
    bq.logical_query.query_fingerprint = "fp-count-shape"

    pocket = types.SimpleNamespace(
        id="pocket-star",
        model_id=bq.model.id,
        status="fresh",
        query_fingerprint="fp-star-shape",
        last_refresh_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        predicates=[
            types.SimpleNamespace(column_name="tenant_id", operator="eq", value_json={"value": 12}),
            types.SimpleNamespace(column_name="base_amount", operator="gt", value_json={"value": 4950}),
        ],
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result_with([pocket]))

    result = await _run(bq, db)

    assert result.pocket is pocket
    assert result.skipped_reason is None


async def test_subquery_wrapped_count_matches_via_filter_subset():
    """`SELECT COUNT(1) FROM (SELECT * FROM t WHERE ...)` against the
    same-slice pocket. After the parser's identity-derived-table
    carve-out flattens the subquery, this reduces to the flat-COUNT case.
    """
    bq = make_bound_query(
        [make_dimension("tenant_id"), make_dimension("base_amount")],
        [make_measure("amount")],
        filters=[
            LogicalFilter("tenant_id", "eq", 12),
            LogicalFilter("base_amount", "gt", 4950),
        ],
        raw_sql=(
            "SELECT COUNT(1) FROM (SELECT * FROM modely "
            "WHERE tenant_id = 12 AND base_amount > 4950) q"
        ),
    )
    bq.logical_query.query_fingerprint = "fp-count-shape"
    bq.has_passthrough_expressions = True  # the old flag path

    pocket = types.SimpleNamespace(
        id="pocket-star",
        model_id=bq.model.id,
        status="fresh",
        query_fingerprint="fp-star-shape",
        last_refresh_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        predicates=[
            types.SimpleNamespace(column_name="tenant_id", operator="eq", value_json={"value": 12}),
            types.SimpleNamespace(column_name="base_amount", operator="gt", value_json={"value": 4950}),
        ],
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result_with([pocket]))

    result = await _run(bq, db)

    assert result.pocket is pocket
    assert result.skipped_reason is None


async def test_query_with_extra_filter_column_matches_pocket():
    """A query adding a filter column the pocket does not have still
    matches: pocket predicate columns are a subset of query filter
    columns, and the extra filter narrows further at rewrite time.

    Reproduction: pocket `SELECT * FROM modely WHERE base_amount > 4950`,
    query `SELECT * FROM (SELECT * FROM modely WHERE base_amount > 4950) q
    WHERE q.auth_method <> 's'`.
    """
    bq = make_bound_query(
        [make_dimension("base_amount"), make_dimension("auth_method")],
        [make_measure("amount")],
        filters=[
            LogicalFilter("base_amount", "gt", 4950),
            LogicalFilter("auth_method", "neq", "s"),
        ],
        raw_sql=(
            "SELECT * FROM (SELECT * FROM modely WHERE base_amount > 4950) q "
            "WHERE q.auth_method <> 's'"
        ),
    )
    bq.logical_query.query_fingerprint = "fp-flattened"

    pocket = types.SimpleNamespace(
        id="pocket-star",
        model_id=bq.model.id,
        status="fresh",
        query_fingerprint="fp-star-shape",
        last_refresh_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        predicates=[
            types.SimpleNamespace(column_name="base_amount", operator="gt", value_json={"value": 4950}),
        ],
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result_with([pocket]))

    result = await _run(bq, db, require_tenant_filter=False)

    assert result.pocket is pocket
    assert result.skipped_reason is None


async def test_query_missing_pocket_filter_column_is_skipped():
    """If the query lacks a filter on a column the pocket predicates,
    the query is broader than the pocket's slice — reject.
    """
    bq = make_bound_query(
        [make_dimension("base_amount")],
        [make_measure("amount")],
        filters=[
            LogicalFilter("base_amount", "gt", 4970),
        ],
        raw_sql="SELECT * FROM modely WHERE base_amount > 4970",
    )
    bq.logical_query.query_fingerprint = "fp-broader"

    pocket = types.SimpleNamespace(
        id="pocket-two-preds",
        model_id=bq.model.id,
        status="fresh",
        query_fingerprint="fp-star-shape",
        last_refresh_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        predicates=[
            types.SimpleNamespace(column_name="tenant_id", operator="eq", value_json={"value": 12}),
            types.SimpleNamespace(column_name="base_amount", operator="gt", value_json={"value": 4950}),
        ],
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result_with([pocket]))

    result = await _run(bq, db, require_tenant_filter=False)

    assert result.pocket is None
    assert result.skipped_reason == PocketSkipReason.FINGERPRINT_OR_PREDICATE_MISMATCH


async def test_baseline_select_star_still_matches_after_filter_subset_path():
    """Baseline case: `SELECT * WHERE slice ⊆ pocket slice` → shape match."""
    bq = make_bound_query(
        [make_dimension("tenant_id")],
        [make_measure("amount")],
        filters=[
            LogicalFilter("tenant_id", "eq", 12),
            LogicalFilter("base_amount", "gt", 4970),
        ],
        raw_sql="SELECT * FROM modely WHERE tenant_id = 12 AND base_amount > 4970",
    )
    bq.logical_query.query_fingerprint = "fp-star-shape"

    pocket = types.SimpleNamespace(
        id="pocket-star",
        model_id=bq.model.id,
        status="fresh",
        query_fingerprint="fp-star-shape",
        last_refresh_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        predicates=[
            types.SimpleNamespace(column_name="tenant_id", operator="eq", value_json={"value": 12}),
            types.SimpleNamespace(column_name="base_amount", operator="gt", value_json={"value": 4950}),
        ],
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result_with([pocket]))

    result = await _run(bq, db)

    assert result.pocket is pocket


async def test_pocket_from_outside_model_is_skipped():
    """Defensive match-time guard: a pocket whose defining SQL reads
    from a table not registered on the model must be skipped so it
    cannot override the model's data structure at rewrite time.
    """
    from shared.db.models import ModelTable

    bq = make_bound_query(
        [make_dimension("tenant_id")],
        [make_measure("amount")],
        filters=[LogicalFilter("tenant_id", "eq", 12)],
        raw_sql="SELECT amount FROM modely WHERE tenant_id = 12",
    )
    bq.logical_query.query_fingerprint = "fp-q"

    orphan_pocket = types.SimpleNamespace(
        id="pocket-orphan",
        model_id=bq.model.id,
        status="fresh",
        query_fingerprint="fp-q",
        last_refresh_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        defining_sql="SELECT * FROM demo_data.sales_data",
        predicates=[
            types.SimpleNamespace(column_name="tenant_id", operator="eq", value_json={"value": 12}),
        ],
    )
    model_table = types.SimpleNamespace(
        physical_name="demo_data.payment_transaction",
        alias="payment_transaction",
    )

    def _execute(stmt, *a, **kw):
        target = stmt.column_descriptions[0]["entity"] if hasattr(stmt, "column_descriptions") else None
        if target is ModelTable:
            return _result_with([model_table])
        return _result_with([orphan_pocket])

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)

    result = await _run(bq, db, require_tenant_filter=False)

    assert result.pocket is None
    assert result.skipped_reason == PocketSkipReason.FROM_OUTSIDE_MODEL


async def test_pocket_missing_required_table_is_skipped():
    """Bug-101 regression: a pocket of ``select * from one_dim_table``
    has an empty predicate set, so ``set().issubset(query_filter_cols)``
    used to succeed and the matcher would offer it for any query — even
    pivots that need columns from the fact + a different dim. The
    rewriter then swapped the FROM table and Postgres rejected the
    SELECT with ``column "auth_method" does not exist``.

    The matcher must verify the candidate pocket's defining SQL reads
    from a superset of the physical tables the query actually needs.
    """
    from shared.db.models import ModelColumn, ModelTable

    fact_col = types.SimpleNamespace(id="col-base", model_table_id="t-fact")
    auth_col = types.SimpleNamespace(id="col-auth", model_table_id="t-auth")
    chan_col = types.SimpleNamespace(id="col-chan", model_table_id="t-chan")

    auth_dim = types.SimpleNamespace(
        id="d-auth", name="auth_method", source_column_id="col-auth",
    )
    chan_dim = types.SimpleNamespace(
        id="d-chan", name="channel_code", source_column_id="col-chan",
    )
    base_meas = types.SimpleNamespace(
        id="m-base", name="base_amount", default_agg="sum",
        is_additive=True, measure_type="standard", expression=None,
        calc_agg_mode=None, source_column_id="col-base",
    )

    bq = make_bound_query(
        [auth_dim, chan_dim], [base_meas],
        raw_sql=(
            'SELECT "auth_method", "channel_code", "base_amount" '
            'FROM modely GROUP BY "auth_method", "channel_code"'
        ),
    )
    bq.logical_query.query_fingerprint = "fp-pivot"

    fact_table = types.SimpleNamespace(
        id="t-fact", physical_name="demo_data.payment_transaction",
        alias="payment_transaction",
    )
    auth_table = types.SimpleNamespace(
        id="t-auth", physical_name="demo_data.dim_auth_method",
        alias="dim_auth_method",
    )
    chan_table = types.SimpleNamespace(
        id="t-chan", physical_name="demo_data.dim_channel_code",
        alias="dim_channel_code",
    )
    all_tables = [fact_table, auth_table, chan_table]
    cols = [fact_col, auth_col, chan_col]

    pocket = types.SimpleNamespace(
        id="pocket-narrow",
        model_id=bq.model.id,
        status="fresh",
        query_fingerprint="fp-narrow",
        last_refresh_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        defining_sql="SELECT * FROM demo_data.dim_auth_method",
        predicates=[],
    )

    def _execute(stmt, *a, **kw):
        target = (
            stmt.column_descriptions[0]["entity"]
            if hasattr(stmt, "column_descriptions") else None
        )
        if target is ModelTable:
            return _result_with(all_tables)
        if target is ModelColumn:
            return _result_with(cols)
        return _result_with([pocket])

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)

    result = await _run(bq, db, require_tenant_filter=False)

    assert result.pocket is None
    assert result.skipped_reason == PocketSkipReason.POCKET_MISSING_REQUIRED_TABLES


async def test_pocket_covering_all_required_tables_still_matches():
    """The Bug-101 gate must NOT regress the happy path: a pocket whose
    defining SQL reads from the same fact + dims the query needs is still
    a valid match (predicate-subset + table-coverage both hold)."""
    from shared.db.models import ModelColumn, ModelTable

    base_col = types.SimpleNamespace(id="col-base", model_table_id="t-fact")
    base_meas = types.SimpleNamespace(
        id="m-base", name="amount", default_agg="sum", is_additive=True,
        measure_type="standard", expression=None, calc_agg_mode=None,
        source_column_id="col-base",
    )

    bq = make_bound_query(
        [], [base_meas],
        raw_sql='SELECT "amount" FROM modely',
    )
    bq.logical_query.query_fingerprint = "fp-cover"

    fact_table = types.SimpleNamespace(
        id="t-fact", physical_name="demo_data.payment_transaction",
        alias="payment_transaction",
    )

    pocket = types.SimpleNamespace(
        id="pocket-cover",
        model_id=bq.model.id,
        status="fresh",
        query_fingerprint="fp-cover",
        last_refresh_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        defining_sql="SELECT * FROM demo_data.payment_transaction",
        predicates=[],
    )

    def _execute(stmt, *a, **kw):
        target = (
            stmt.column_descriptions[0]["entity"]
            if hasattr(stmt, "column_descriptions") else None
        )
        if target is ModelTable:
            return _result_with([fact_table])
        if target is ModelColumn:
            return _result_with([base_col])
        return _result_with([pocket])

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)

    result = await _run(bq, db, require_tenant_filter=False)

    assert result.pocket is pocket
    assert result.skipped_reason is None


async def test_fingerprint_mismatch_skips_with_named_reason():
    """Pocket is skipped when shape FP differs AND its predicate columns
    are not a subset of the query's filter columns.
    """
    bq = make_bound_query(
        [make_dimension("tenant_id")], [make_measure("amount")],
        filters=[LogicalFilter("tenant_id", "eq", 12)],
        raw_sql="SELECT amount FROM sales WHERE tenant_id = 12",
    )
    bq.logical_query.query_fingerprint = "fp-query"

    candidate = types.SimpleNamespace(
        id="pocket-x",
        model_id=bq.model.id,
        status="fresh",
        query_fingerprint="fp-different",
        last_refresh_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        predicates=[
            types.SimpleNamespace(column_name="country", operator="eq", value_json={"value": "GB"}),
        ],
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result_with([candidate]))

    result = await _run(bq, db)

    assert result.pocket is None
    assert result.skipped_reason == PocketSkipReason.FINGERPRINT_OR_PREDICATE_MISMATCH
