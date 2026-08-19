"""Router-level integration tests for row security.

Invariants under test:
  * A principal with a matching active rule has the security predicate
    injected per-scan via ``_inject_security_where`` — AND'd into the WHERE
    clause of every SELECT that reads a physical table, not wrapped in an
    outer subquery.
  * The fixtures here route to ``route_type="source"`` because no RLS-SAFE
    accelerated candidate exists in them, NOT because an active rule
    disables acceleration. Bug-7033 / Bug-8018 removed that rule: an
    aggregate whose grain carries every security column, or a pocket whose
    ``row_manifest`` proves it materialised them, IS served under active
    row security with the same predicate injected. Those paths are covered
    by ``test_bug_8018_rls_pocket_serving.py`` and the RLS-safe aggregate
    tests; do not read a ``route_type == "source"`` assertion in this file
    as evidence that the fast paths are switched off.
  * A principal with no matching rule takes the normal routing path
    (aggregate or source) unchanged.
  * Passing ``principal=None`` is fully backwards-compatible with the
    pre-Phase-5 router — existing call sites that omit the argument see
    no behavior change.
  * Multiple active rules join with ``AND`` in the injected predicate.
  * user_mapping rules emit an IN-subquery against the mapping table.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import sqlglot
from fastapi import HTTPException
from sqlglot import exp

from src.routing.router import route_query, _inject_security_where
from src.security import CompiledPredicate, Principal

from conftest import make_aggregate, make_agg_col, make_dimension, make_measure
from test_query_flow import _bind

_PATCH_LOAD = "src.routing.aggregate_matcher.load_active_aggregates"
_PATCH_COMPILE = "src.routing.router.compile_row_security"

pytestmark = pytest.mark.integration


def _role_rule(
    path: str,
    expr: str,
    roles: list[str],
    rule_id=None,
    attribute_source: str = "jwt_role",
    attribute_claim_name: str | None = None,
):
    return types.SimpleNamespace(
        id=rule_id or uuid.uuid4(),
        rule_type="role_predicate",
        name="test-rule",
        dimension_path=path,
        predicate_expression=expr,
        applies_to_roles=roles,
        mapping_table_id=None,
        mapping_user_column=None,
        mapping_value_column=None,
        is_enabled=True,
        attribute_source=attribute_source,
        attribute_claim_name=attribute_claim_name,
    )


def _user_mapping_rule(path: str, table_id, user_col: str, value_col: str, rule_id=None):
    return types.SimpleNamespace(
        id=rule_id or uuid.uuid4(),
        rule_type="user_mapping",
        name="test-mapping-rule",
        dimension_path=path,
        predicate_expression=None,
        applies_to_roles=None,
        mapping_table_id=table_id,
        mapping_user_column=user_col,
        mapping_value_column=value_col,
        is_enabled=True,
    )


def _db_returning(rules, mapping_table=None):
    """Fake AsyncSession whose ``execute()`` dispatches by table-name
    substring of the compiled statement — same pattern as the compiler
    unit tests.
    """

    class _Result:
        def __init__(self, items):
            self._items = list(items)

        def scalars(self):
            items = self._items

            class _S:
                def all(self_inner):
                    return items

            return _S()

        def scalar_one_or_none(self):
            return self._items[0] if self._items else None

        def fetchall(self):
            return []

    db = AsyncMock()

    async def _execute(stmt):
        text = str(stmt).lower()
        if "row_security_rules" in text:
            return _Result(rules)
        if "model_tables" in text:
            return _Result([mapping_table] if mapping_table else [])
        return _Result([])

    db.execute = _execute

    # Provide a real async context manager for begin_nested() so
    # record_aggregate_miss (invoked when an aggregate is matched then rejected
    # by the percentile gate) does not leave an unawaited coroutine warning.
    class _NestedCtx:
        async def __aenter__(self_inner):
            return self_inner

        async def __aexit__(self_inner, *exc):
            return False

    db.begin_nested = lambda: _NestedCtx()
    return db


# ---------------------------------------------------------------------------
# Core invariant: active rule injects predicate; RLS-safe aggregates served
# ---------------------------------------------------------------------------


async def test_principal_with_matching_rule_serves_rls_safe_aggregate():
    """Bug-7033: when the security dimension column is in the aggregate grain,
    the aggregate IS served with the predicate injected — not bypassed."""
    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m)])

    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])

    rule = _role_rule(
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["region_manager_north"],
    )
    principal = Principal(
        user_identity="alice@x", roles=frozenset({"region_manager_north"})
    )
    db = _db_returning([rule])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        decision = await route_query(bq, db, principal=principal)

    # Bug-7033: the aggregate is RLS-safe (grain includes region_code), so
    # the route is "aggregate" with the security predicate injected.
    assert decision.route_type == "aggregate"
    assert decision.aggregate_id == str(agg.id)
    assert "\"region_code\" = 'NORTH'" in decision.rewritten_query
    assert "Row security active" in decision.reason
    assert "RLS-safe aggregate" in decision.reason
    # The aggregate loader IS called now (Bug-7033 fix).
    mock_load.assert_called_once()


async def test_rls_aggregate_rejects_approximate_percentile():
    """Bug-7772: the RLS aggregate-serving path must apply the same percentile-
    exactness gate as the normal path. Without this gate, an RLS query can be
    served from an aggregate whose quantile columns are approximate (e.g.
    BigQuery APPROX_QUANTILES), returning WRONG percentile values."""
    m = make_measure("revenue")
    d = make_dimension("region_code")
    # Aggregate with a p50 column and security column in grain (RLS-safe).
    agg_col = make_agg_col(m, stat_type="p50")
    agg = make_aggregate(["region_code"], [agg_col])

    # Query uses MEDIAN -> agg_function="p50" on the select expression.
    sql = "SELECT region_code, MEDIAN(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])
    # Manually set the select_expressions to include a percentile function
    # (the IR parser may or may not map MEDIAN -> p50; set it explicitly).
    bq.logical_query.select_expressions = [
        types.SimpleNamespace(
            raw_text="MEDIAN(revenue)", alias=None,
            classification="analytical", agg_function="p50",
            inner_column="revenue", inner_literal=None,
            composable=False, agg_functions=[], inner_aggregates=[],
        ),
    ]

    rule = _role_rule(
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["region_manager_north"],
    )
    principal = Principal(
        user_identity="alice@x", roles=frozenset({"region_manager_north"})
    )
    db = _db_returning([rule])

    # Patch source dialect to BigQuery (approximate quantiles).
    with (
        patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load,
        patch(
            "src.routing.router._resolve_aggregate_source_dialect",
            new_callable=AsyncMock,
            return_value="bigquery",
        ),
    ):
        mock_load.return_value = [agg]
        decision = await route_query(bq, db, principal=principal)

    # The aggregate MUST NOT be served (wrong numbers for percentile).
    assert decision.route_type == "source", (
        f"Bug-7772: RLS path served aggregate with approximate percentile! "
        f"route={decision.route_type}, reason={decision.reason}"
    )
    assert decision.aggregate_id is None
    assert "\"region_code\" = 'NORTH'" in decision.rewritten_query


async def test_rls_aggregate_serves_exact_percentile():
    """Bug-7772 positive path: when the source dialect is exact (postgresql),
    the RLS path SHOULD serve the aggregate for percentile queries."""
    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg_col = make_agg_col(m, stat_type="p50")
    agg = make_aggregate(["region_code"], [agg_col])

    sql = "SELECT region_code, MEDIAN(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])
    bq.logical_query.select_expressions = [
        types.SimpleNamespace(
            raw_text="MEDIAN(revenue)", alias=None,
            classification="analytical", agg_function="p50",
            inner_column="revenue", inner_literal=None,
            composable=False, agg_functions=[], inner_aggregates=[],
        ),
    ]

    rule = _role_rule(
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["region_manager_north"],
    )
    principal = Principal(
        user_identity="alice@x", roles=frozenset({"region_manager_north"})
    )
    db = _db_returning([rule])

    # Patch source dialect to PostgreSQL (exact quantiles).
    with (
        patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load,
        patch(
            "src.routing.router._resolve_aggregate_source_dialect",
            new_callable=AsyncMock,
            return_value="postgresql",
        ),
    ):
        mock_load.return_value = [agg]
        decision = await route_query(bq, db, principal=principal)

    # Exact source -> aggregate SHOULD be served.
    assert decision.route_type == "aggregate", (
        f"Bug-7772 positive: exact percentile should serve aggregate! "
        f"route={decision.route_type}, reason={decision.reason}"
    )
    assert decision.aggregate_id == str(agg.id)
    assert "\"region_code\" = 'NORTH'" in decision.rewritten_query


async def test_default_agg_quantile_rejects_approximate_percentile_normal_path():
    """Bug-7779 (normal path): a bare measure whose ``default_agg`` is p50 (NO
    explicit MEDIAN/PERCENTILE syntax in the SELECT) must still be held to the
    dialect-exactness gate. Against a BigQuery source (APPROX_QUANTILES) the
    query must route to SOURCE, not serve the approximate p50 column as exact.

    Before the fix ``_query_uses_percentile`` inspected only SELECT
    agg_function, so this bypassed the gate and served wrong numbers (the exact
    p90 of [1,100] is 90.1 but the APPROX boundary is 100)."""
    # default_agg=p50, is_additive default True: the semantic inventory must
    # still recognise this as a quantile request.
    m = make_measure("median_latency", default_agg="p50")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m, stat_type="p50")])

    sql = "SELECT region_code, median_latency FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])
    # No explicit percentile agg_function on the select expression — the
    # quantile arrives purely via the measure default_agg.
    bq.logical_query.select_expressions = [
        types.SimpleNamespace(
            raw_text="median_latency", alias=None, classification="measure",
            agg_function=None, inner_column="median_latency", inner_literal=None,
            composable=False, agg_functions=[], inner_aggregates=[],
        ),
    ]
    db = _db_returning([])

    with (
        patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load,
        patch(
            "src.routing.router._resolve_aggregate_source_dialect",
            new_callable=AsyncMock,
            return_value="bigquery",
        ),
    ):
        mock_load.return_value = [agg]
        decision = await route_query(bq, db)  # no principal (normal path)

    assert decision.route_type == "source", (
        f"Bug-7779: default_agg quantile served approximate aggregate in exact "
        f"mode! route={decision.route_type}, reason={decision.reason}"
    )
    assert decision.aggregate_id is None


async def test_default_agg_quantile_rejects_approximate_percentile_rls_path():
    """Bug-7779 (RLS path): the same default_agg-quantile bypass on the RLS
    serving path. A BigQuery-source aggregate must route to source."""
    m = make_measure("median_latency", default_agg="p50")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m, stat_type="p50")])

    sql = "SELECT region_code, median_latency FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])
    bq.logical_query.select_expressions = [
        types.SimpleNamespace(
            raw_text="median_latency", alias=None, classification="measure",
            agg_function=None, inner_column="median_latency", inner_literal=None,
            composable=False, agg_functions=[], inner_aggregates=[],
        ),
    ]

    rule = _role_rule(
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["region_manager_north"],
    )
    principal = Principal(
        user_identity="alice@x", roles=frozenset({"region_manager_north"})
    )
    db = _db_returning([rule])

    with (
        patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load,
        patch(
            "src.routing.router._resolve_aggregate_source_dialect",
            new_callable=AsyncMock,
            return_value="bigquery",
        ),
    ):
        mock_load.return_value = [agg]
        decision = await route_query(bq, db, principal=principal)

    assert decision.route_type == "source", (
        f"Bug-7779 RLS: default_agg quantile served approximate aggregate in "
        f"exact mode! route={decision.route_type}, reason={decision.reason}"
    )
    assert decision.aggregate_id is None


async def test_principal_with_rule_falls_to_source_when_agg_grain_lacks_security_col():
    """Bug-7033: when the security column is NOT in the aggregate grain,
    the aggregate is not served — source route with predicate injection."""
    m = make_measure("revenue")
    d = make_dimension("country")
    # Aggregate grain does NOT include "region_code" (the security column).
    agg = make_aggregate(["country"], [make_agg_col(m)])

    sql = "SELECT country, SUM(revenue) FROM sales GROUP BY country"
    bq = _bind(sql, [m], [d])

    rule = _role_rule(
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["region_manager_north"],
    )
    principal = Principal(
        user_identity="alice@x", roles=frozenset({"region_manager_north"})
    )
    db = _db_returning([rule])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        decision = await route_query(bq, db, principal=principal)

    # The aggregate grain lacks the security column, so it cannot be safely
    # filtered — the query falls back to source with predicate injection.
    assert decision.route_type == "source"
    assert decision.aggregate_id is None
    assert "\"region_code\" = 'NORTH'" in decision.rewritten_query
    assert "Row security active" in decision.reason


async def test_malformed_rule_predicate_fails_closed_not_unfiltered():
    # F-007-04 fail closed: if a matched rule's stored predicate is malformed
    # (e.g. it slipped past save-time validation via import or a direct DB
    # edit), route_query must raise RowSecurityCompileError — which the API
    # boundary turns into a 422 — rather than silently routing the query to
    # the source unfiltered or serving an aggregate. The query must NEVER
    # run when the security predicate cannot be compiled.
    from shared.security import RowSecurityCompileError

    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m)])

    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])

    rule = _role_rule(
        "region.region_code",
        "garbage(",  # malformed — does not compile
        ["region_manager_north"],
    )
    principal = Principal(
        user_identity="alice@x", roles=frozenset({"region_manager_north"})
    )
    db = _db_returning([rule])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        with pytest.raises(RowSecurityCompileError):
            await route_query(bq, db, principal=principal)
        # Fail-closed invariant: the aggregate loader must never be reached
        # for a caller under an (uncompilable but matching) rule.
        mock_load.assert_not_called()


async def test_principal_without_matching_rule_fails_closed_deny_all():
    """F-007-01: a model with a role_predicate rule governs an audience. A
    principal who matches NO rule (here role ``viewer`` vs the rule's
    ``region_manager_north``) is an unmatched member and must be DENIED every
    row — a deny-all predicate routed to source, NOT the unrestricted aggregate
    path the old no-op took."""
    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m)])

    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])

    rule = _role_rule(
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["region_manager_north"],
    )
    # Principal has a different role — no rule matches -> fail closed.
    principal = Principal(user_identity="bob@x", roles=frozenset({"viewer"}))
    db = _db_returning([rule])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        decision = await route_query(bq, db, principal=principal)

    # Deny-all is not aggregate-safe (no security columns) -> source route with
    # the ``0 = 1`` predicate injected. The unrestricted aggregate is NOT served.
    assert decision.route_type != "aggregate"
    assert decision.aggregate_id is None
    assert "0 = 1" in decision.rewritten_query


async def test_no_principal_is_backwards_compatible():
    """Omitting the principal parameter must behave identically to the
    pre-Phase-5 router — compile_row_security should never be invoked."""
    m = make_measure("revenue")
    d = make_dimension("country")
    agg = make_aggregate(["country"], [make_agg_col(m)])

    sql = "SELECT country, SUM(revenue) FROM sales GROUP BY country"
    bq = _bind(sql, [m], [d])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load, \
         patch(_PATCH_COMPILE, new_callable=AsyncMock) as mock_compile:
        mock_load.return_value = [agg]
        decision = await route_query(bq, AsyncMock())

    assert decision.route_type == "aggregate"
    mock_compile.assert_not_called()


# ---------------------------------------------------------------------------
# Multi-rule composition + user_mapping
# ---------------------------------------------------------------------------


async def test_multiple_matching_rules_are_joined_with_and():
    m = make_measure("revenue")
    d = make_dimension("region_code")

    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])

    r1 = _role_rule(
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["region_manager_north"],
    )
    r2 = _role_rule(
        "status.flag",
        "dimension_equals('status.flag', 'ACTIVE')",
        ["region_manager_north"],
    )
    principal = Principal(
        user_identity="alice@x", roles=frozenset({"region_manager_north"})
    )
    db = _db_returning([r1, r2])

    # Bug-7033: aggregate matcher runs but the aggregate grain (region_code)
    # does not include all security columns (region_code + flag), so the
    # result falls back to source.
    agg = make_aggregate(["region_code"], [make_agg_col(m)])
    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        decision = await route_query(bq, db, principal=principal)

    assert decision.route_type == "source"
    assert " AND " in decision.rewritten_query
    assert "\"region_code\" = 'NORTH'" in decision.rewritten_query
    assert "\"flag\" = 'ACTIVE'" in decision.rewritten_query


async def test_user_mapping_rule_emits_in_subquery_in_wrap():
    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m)])

    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])

    table = types.SimpleNamespace(
        id=uuid.uuid4(), physical_name="demo_data.user_region_map",
        source_id=uuid.uuid4(),
    )
    rule = _user_mapping_rule(
        "region.region_code", table.id, "user_id", "region_code"
    )
    principal = Principal(user_identity="alice@x", roles=frozenset())
    db = _db_returning([rule], mapping_table=table)

    # Codex R1: user-mapping predicates reference source-connection tables
    # not present on the aggregate target, so user-mapping rules force source
    # route even when the security column is in the aggregate grain.
    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        decision = await route_query(bq, db, principal=principal)

    assert decision.route_type == "source"
    assert '"region_code" IN (SELECT "region_code" FROM' in decision.rewritten_query
    assert '"demo_data"."user_region_map"' in decision.rewritten_query
    assert "'alice@x'" in decision.rewritten_query


# ---------------------------------------------------------------------------
# Disabled-rule + role-set edge cases
# ---------------------------------------------------------------------------


async def test_disabled_rule_is_ignored():
    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m)])

    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])

    rule = _role_rule(
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["region_manager_north"],
    )
    rule.is_enabled = False
    # The compiler loader filters on is_enabled at the SQL layer — emulate
    # that by simply not returning the disabled rule from our fake db.
    principal = Principal(
        user_identity="alice@x", roles=frozenset({"region_manager_north"})
    )
    db = _db_returning([])  # loader returns nothing → no wrap

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        decision = await route_query(bq, db, principal=principal)

    assert decision.route_type == "aggregate"
    assert "__ts_sec" not in decision.rewritten_query


# ---------------------------------------------------------------------------
# F-007-01 — per-scan injection: every branch that reads a table is filtered
# ---------------------------------------------------------------------------

_PRED = CompiledPredicate(
    sql_expression="\"region_code\" = 'NORTH'",
    active_rule_ids=("r1",),
    security_dimension_columns=("region_code",),
)


def _selects_scanning_tables(sql: str, dialect: str = "postgres"):
    """Return (select, scanned_table_names) pairs for every SELECT that
    directly scans at least one non-CTE table."""
    ast = sqlglot.parse_one(sql, read=dialect)
    ctes = {c.alias_or_name.lower() for c in ast.find_all(exp.CTE)}
    out = {}
    for table in ast.find_all(exp.Table):
        if table.name.lower() in ctes:
            continue
        sel = table.find_ancestor(exp.Select)
        out.setdefault(id(sel), (sel, set()))[1].add(table.name.lower())
    return [v for v in out.values()]


def _assert_every_scan_filtered(sql: str, dialect: str = "postgres"):
    """Business outcome: each SELECT scanning a physical table carries the
    security predicate in its own WHERE."""
    for sel, tables in _selects_scanning_tables(sql, dialect):
        where = sel.args.get("where")
        assert where is not None, f"unfiltered branch scanning {tables}: {sel.sql()}"
        assert "region_code" in where.sql() and "NORTH" in where.sql(), (
            f"branch scanning {tables} missing security predicate: {sel.sql()}"
        )


def test_union_all_filters_both_branches():
    sql = (
        "SELECT region_code FROM sales WHERE x = 1 "
        "UNION ALL SELECT region_code FROM archive"
    )
    out = _inject_security_where(sql, _PRED)
    _assert_every_scan_filtered(out)
    # The pre-existing branch filter is preserved alongside the predicate.
    assert "x = 1" in out


def test_union_of_same_table_filters_each_branch():
    sql = "SELECT region_code FROM sales UNION ALL SELECT region_code FROM sales"
    out = _inject_security_where(sql, _PRED)
    _assert_every_scan_filtered(out)
    assert out.count("'NORTH'") == 2


def test_scalar_subquery_scan_is_filtered():
    sql = "SELECT region_code, (SELECT MAX(x) FROM other WHERE y = 1) FROM sales"
    out = _inject_security_where(sql, _PRED)
    _assert_every_scan_filtered(out)
    # Both the outer sales scan and the scalar subquery scan are constrained.
    assert out.count("'NORTH'") == 2


def test_subquery_first_from_filters_outer_join_scan():
    sql = (
        "SELECT s.region_code FROM (SELECT * FROM dim WHERE active) AS d "
        "JOIN sales AS s ON d.k = s.k"
    )
    out = _inject_security_where(sql, _PRED)
    _assert_every_scan_filtered(out)


def test_cte_body_scan_is_filtered_and_cte_reference_is_not_double_filtered():
    sql = (
        "WITH c AS (SELECT region_code FROM sales) "
        "SELECT region_code, COUNT(*) FROM c GROUP BY region_code"
    )
    out = _inject_security_where(sql, _PRED)
    ast = sqlglot.parse_one(out, read="postgres")
    cte = next(iter(ast.find_all(exp.CTE)))
    assert "NORTH" in cte.this.sql()
    # Outer SELECT reads only the CTE — no second injection required.
    assert out.count("'NORTH'") == 1


def test_predicate_lands_before_limit():
    sql = "SELECT region_code FROM sales LIMIT 3"
    out = _inject_security_where(sql, _PRED)
    assert "WHERE" in out and "LIMIT 3" in out
    assert out.index("'NORTH'") < out.index("LIMIT 3")


def test_unparseable_sql_is_rejected_not_silently_wrapped():
    with pytest.raises(HTTPException) as exc:
        _inject_security_where("SELECT FROM WHERE LIMIT GROUP !!", _PRED)
    assert exc.value.status_code == 403
    assert exc.value.detail["error_code"] == "row_security_unsupported_shape"


def test_query_without_table_scan_is_rejected():
    with pytest.raises(HTTPException) as exc:
        _inject_security_where("SELECT 1", _PRED)
    assert exc.value.status_code == 403
    assert exc.value.detail["error_code"] == "row_security_unsupported_shape"


def test_user_mapping_predicate_subquery_is_not_reinjected():
    """The predicate's own IN-subquery must not itself be treated as a
    branch needing injection (infinite/duplicated predicates)."""
    pred = CompiledPredicate(
        sql_expression=(
            '"region_code" IN (SELECT "region_code" FROM "demo_data"."user_region_map" '
            "WHERE \"user_id\" = 'alice@x')"
        ),
        active_rule_ids=("r1",),
        security_dimension_columns=("region_code",),
    )
    sql = "SELECT region_code FROM sales"
    out = _inject_security_where(sql, pred)
    assert out.count("user_region_map") == 1


def test_bigquery_dialect_injects_instead_of_wrapping():
    """F-007-03: target-dialect predicate (backticks) must parse and inject
    under the target dialect instead of falling back to the subquery wrap."""
    pred = CompiledPredicate(
        sql_expression="`region_code` = 'NORTH'",
        active_rule_ids=("r1",),
        security_dimension_columns=("region_code",),
        # Bug-8396: a backtick-quoted expression IS BigQuery-compiled; the
        # producer must say so, otherwise the identifier cannot be read back.
        compile_connector="bigquery",
    )
    sql = "SELECT region_code FROM sales LIMIT 3"
    out = _inject_security_where(sql, pred, dialect="bigquery")
    assert "__ts_sec" not in out
    assert "WHERE" in out and "LIMIT 3" in out
    assert out.index("NORTH") < out.index("LIMIT 3")


# ---------------------------------------------------------------------------
# Bug-1070 — OR-WHERE precedence: the security predicate must constrain the
# WHOLE existing WHERE, never just the right-most OR branch
# ---------------------------------------------------------------------------


def _assert_predicate_never_under_or(out_sql: str, dialect: str = "postgres"):
    """Re-parse the emitted SQL and assert that no occurrence of the
    security predicate sits beneath an OR node — i.e. the database will
    apply it to every row, not just one OR branch."""
    ast = sqlglot.parse_one(out_sql, read=dialect)
    found = 0
    for eq in ast.find_all(exp.EQ):
        if eq.this.sql(dialect=dialect).strip('"`') != "region_code":
            continue
        if "NORTH" not in eq.sql(dialect=dialect):
            continue
        found += 1
        node = eq.parent
        while node is not None and not isinstance(node, exp.Where):
            assert not isinstance(node, exp.Or), (
                f"security predicate is absorbed into an OR branch: {out_sql}"
            )
            node = node.parent
    assert found, f"security predicate missing from output: {out_sql}"


def test_top_level_or_where_is_parenthesized_before_and():
    """The live Bug-1070 shape: WHERE A OR B must become (A OR B) AND pred,
    never A OR (B AND pred)."""
    sql = (
        "SELECT payment_status, COUNT(*) FROM sales "
        "WHERE region = 'LON' OR country = 'GB' GROUP BY payment_status"
    )
    out = _inject_security_where(sql, _PRED)
    _assert_predicate_never_under_or(out)
    # Both user branches survive inside the parenthesized group.
    assert "'LON'" in out and "'GB'" in out
    # Top-level WHERE connective is AND with the predicate as a conjunct.
    where = sqlglot.parse_one(out, read="postgres").find(exp.Where)
    assert isinstance(where.this, exp.And)


def test_nested_or_of_ands_where_is_parenthesized():
    sql = (
        "SELECT region_code FROM sales "
        "WHERE (a = 1 AND b = 2) OR (c = 3 AND d = 4)"
    )
    out = _inject_security_where(sql, _PRED)
    _assert_predicate_never_under_or(out)


def test_or_where_in_scalar_subquery_branch_is_parenthesized():
    sql = (
        "SELECT region_code, "
        "(SELECT MAX(x) FROM other WHERE y = 1 OR z = 2) FROM sales"
    )
    out = _inject_security_where(sql, _PRED)
    _assert_every_scan_filtered(out)
    _assert_predicate_never_under_or(out)
    assert out.count("'NORTH'") == 2


def test_or_where_in_union_branch_is_parenthesized():
    sql = (
        "SELECT region_code FROM sales WHERE x = 1 OR y = 2 "
        "UNION ALL SELECT region_code FROM archive WHERE z = 3"
    )
    out = _inject_security_where(sql, _PRED)
    _assert_every_scan_filtered(out)
    _assert_predicate_never_under_or(out)
    assert out.count("'NORTH'") == 2


# ---------------------------------------------------------------------------
# Bug-1071 — CTE-alias / physical-table name collision must be scope-aware
# ---------------------------------------------------------------------------


def test_cte_shadowing_physical_table_still_filters_the_body_scan():
    """A CTE named like the physical table must not exempt the physical
    scan inside its own body (bare-name exclusion bug)."""
    sql = (
        "WITH sales AS (SELECT region_code FROM sales) "
        "SELECT region_code FROM sales "
        "UNION ALL SELECT region_code FROM other_table"
    )
    out = _inject_security_where(sql, _PRED)
    # Exactly two physical scans: the CTE body's sales + other_table.
    # The outer FROM sales is a CTE reference and must not be re-filtered.
    assert out.count("'NORTH'") == 2
    ast = sqlglot.parse_one(out, read="postgres")
    cte = next(iter(ast.find_all(exp.CTE)))
    assert "NORTH" in cte.this.sql()


def test_insert_select_target_is_rejected_fail_closed():
    """Scope analysis never visits the INSERT target table — the sweep
    must reject rather than run it unfiltered."""
    with pytest.raises(HTTPException) as exc:
        _inject_security_where("INSERT INTO tgt SELECT region_code FROM sales", _PRED)
    assert exc.value.status_code == 403
    assert exc.value.detail["error_code"] == "row_security_unsupported_shape"


# ---------------------------------------------------------------------------
# F-007-02 — claim/scope attribute sources enforced at query time
# ---------------------------------------------------------------------------


async def test_saml_claim_rule_fires_for_principal_with_claim():
    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m)])

    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])

    rule = _role_rule(
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["sales-emea"],
        attribute_source="saml_claim",
        attribute_claim_name="department",
    )
    principal = Principal(
        user_identity="alice@x",
        roles=frozenset({"viewer"}),
        claims={"department": ["sales-emea", "back-office"]},
    )
    db = _db_returning([rule])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        decision = await route_query(bq, db, principal=principal)

    # Bug-7033: the aggregate is RLS-safe (grain includes region_code).
    assert decision.route_type == "aggregate"
    assert "\"region_code\" = 'NORTH'" in decision.rewritten_query


async def test_idp_group_rule_fires_for_principal_derived_from_current_user():
    # F-007-12: an idp_group-sourced row-security rule must fire for a principal
    # whose groups are derived from the current user (the SSO group path), and
    # inject the predicate into the query (aggregate or source).
    m = make_measure("revenue")
    d = make_dimension("region_code")

    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])

    rule = _role_rule(
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["finance-analysts"],
        attribute_source="idp_group",
    )
    principal = Principal.from_current_user(types.SimpleNamespace(
        email="dana@x",
        user_id="dana@x",
        role="viewer",
        groups=["finance-analysts", "all-staff"],
    ))
    db = _db_returning([rule])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = []  # no aggregates available
        decision = await route_query(bq, db, principal=principal)

    assert decision.route_type == "source"
    assert "\"region_code\" = 'NORTH'" in decision.rewritten_query


async def test_saml_claim_rule_without_claim_fails_closed():
    """F-007-01: a saml_claim rule that does not fire (the principal carries no
    matching claim) still leaves the principal an unmatched member of a governed
    audience, so RLS fails CLOSED (deny-all -> source), not the unrestricted
    aggregate path. The claim not matching is exactly the typo/drift case the
    fail-closed rule protects against."""
    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m)])

    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])

    rule = _role_rule(
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["sales-emea"],
        attribute_source="saml_claim",
        attribute_claim_name="department",
    )
    principal = Principal(
        user_identity="bob@x", roles=frozenset({"viewer"}), claims={},
    )
    db = _db_returning([rule])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        decision = await route_query(bq, db, principal=principal)

    assert decision.route_type != "aggregate"
    assert "0 = 1" in decision.rewritten_query


async def test_oidc_scope_rule_fires_for_space_delimited_scope_string():
    m = make_measure("revenue")
    d = make_dimension("region_code")

    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])

    rule = _role_rule(
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["reports:read"],
        attribute_source="oidc_scope",
        attribute_claim_name="scope",
    )
    principal = Principal(
        user_identity="carol@x",
        roles=frozenset({"viewer"}),
        claims={"scope": "openid profile reports:read"},
    )
    db = _db_returning([rule])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = []  # no aggregates available
        decision = await route_query(bq, db, principal=principal)

    assert decision.route_type == "source"
    assert "\"region_code\" = 'NORTH'" in decision.rewritten_query


def test_principal_from_current_user_carries_claims():
    user = types.SimpleNamespace(
        email="alice@x",
        user_id="alice@x",
        role="viewer",
        groups=["g1"],
        claims={"department": "sales-emea", "scope": "openid reports:read"},
    )
    p = Principal.from_current_user(user)
    assert p.claims == {"department": "sales-emea", "scope": "openid reports:read"}
    assert p.groups == frozenset({"g1"})


async def test_forced_raw_rls_complex_shape_downgrades_to_source_non_silently():
    """Bug-7029: a force_route="raw" request whose shape the raw builder cannot
    render (e.g. a UNION / complex SQL) under active RLS must downgrade to the
    source route with the predicate injected — and the downgrade must NOT be
    silent: the route is "source" and the reason explicitly records that the
    forced raw route was downgraded (and why)."""
    m = make_measure("revenue")
    d = make_dimension("region_code")

    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])
    # Simulate a shape the raw builder cannot serve (UNION / complex SQL).
    bq.logical_query.has_complex_sql = True

    rule = _role_rule(
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["region_manager_north"],
    )
    principal = Principal(
        user_identity="alice@x", roles=frozenset({"region_manager_north"})
    )
    db = _db_returning([rule])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = []
        decision = await route_query(
            bq, db, principal=principal, force_route="raw",
        )

    # Runs correctly (fail-closed): source route, predicate injected.
    assert decision.route_type == "source"
    assert "\"region_code\" = 'NORTH'" in decision.rewritten_query
    # Non-silent: the reason names the forced-raw -> source downgrade.
    assert "force_route=raw downgraded to source" in decision.reason


# ---------------------------------------------------------------------------
# Bug-8396 [CRITICAL] — cross-connector RLS predicate must not degrade to a
# constant. ``CompiledPredicate.sql_expression`` is quoted in the SOURCE
# connector's dialect, but an aggregate may be materialised into a target on a
# DIFFERENT connector (aggregates, unlike pockets, have no cross-connector
# rejection: scheduler/src/jobs/full_refresh.py builds a BigQuery/Spark target
# from a PostgreSQL source through the cross-DB path). Re-parsing
# ``NOT "region_code" = 'EMEA'`` with ``read="bigquery"`` degrades the quoted
# identifier to a STRING LITERAL, so the predicate becomes the constant-true
# ``NOT 'region_code' = 'EMEA'`` and EVERY row is served.
# ---------------------------------------------------------------------------

_PG_NEGATED_PRED = CompiledPredicate(
    sql_expression="(NOT \"region_code\" = 'EMEA')",
    active_rule_ids=("r1",),
    security_dimension_columns=("region_code",),
    compile_connector="postgresql",
)


@pytest.mark.parametrize("exec_dialect", ["bigquery", "spark"])
def test_bug8396_pg_predicate_served_on_other_dialect_still_filters(exec_dialect):
    """A PostgreSQL-compiled predicate served from a BigQuery/Spark aggregate
    target must still be a COLUMN comparison, never a constant-true literal
    comparison that admits every row."""
    sql = "SELECT region_code, SUM(amount) FROM agg_sales GROUP BY region_code"
    out = _inject_security_where(
        sql, _PG_NEGATED_PRED, dialect=exec_dialect,
    )
    ast = sqlglot.parse_one(out, read=exec_dialect)
    where = ast.find(exp.Where)
    assert where is not None, "no WHERE injected"
    cols = {c.name for c in where.find_all(exp.Column)}
    # THE bypass signature: the security column survived as an identifier.
    assert "region_code" in cols, (
        f"RLS BYPASS: security column degraded away under {exec_dialect}: {out}"
    )
    # And it must not have become a bare string literal on the left-hand side.
    literals = {
        l.this for l in where.find_all(exp.Literal) if l.is_string
    }
    assert "region_code" not in literals, (
        f"RLS BYPASS: security column became a string literal: {out}"
    )


def test_bug8396_predicate_mislabelled_compile_dialect_is_rejected():
    """Defence in depth: if the recorded compile dialect does not actually
    parse the expression into columns, refuse to serve rather than execute a
    predicate that would filter nothing."""
    bad = CompiledPredicate(
        sql_expression="(NOT \"region_code\" = 'EMEA')",
        active_rule_ids=("r1",),
        security_dimension_columns=("region_code",),
        # Wrong: the expression is PostgreSQL-quoted, not BigQuery-quoted.
        compile_connector="bigquery",
    )
    with pytest.raises(HTTPException) as exc:
        _inject_security_where(
            "SELECT region_code FROM agg_sales", bad, dialect="bigquery",
        )
    assert exc.value.status_code == 403
    assert exc.value.detail["error_code"] == "row_security_unsupported_shape"


def test_bug8396_deny_all_sentinel_still_injects_cross_dialect():
    """``0 = 1`` references no column and must remain injectable in every
    dialect — the fail-closed sentinel must not become fail-open OR error."""
    deny = CompiledPredicate(
        sql_expression="0 = 1",
        active_rule_ids=("__deny_all__",),
        security_dimension_columns=(),
        compile_connector="postgresql",
    )
    out = _inject_security_where(
        "SELECT region_code FROM agg_sales", deny, dialect="bigquery",
    )
    assert "0 = 1" in out.replace("0=1", "0 = 1")


def test_bug8396_same_dialect_predicate_is_passed_through_unchanged():
    """No gratuitous rewrite when compile and execution dialects agree."""
    out = _inject_security_where(
        "SELECT region_code FROM sales", _PG_NEGATED_PRED, dialect="postgres",
    )
    assert '"region_code"' in out
    assert "'EMEA'" in out


# --- Fable R1 deep-review, tests_to_promote (applied verbatim) --------------
# Two fail-closed branches of the Bug-8396 conversion that the lane's own
# tests did not reach: an unsupported connector token, and the SimpleNamespace
# the Bug-7033-F1 rename block hands the injector.


def test_bug8396_unknown_connector_token_requires_identical_tokens():
    """A connector token outside the supported set must never be transpiled on
    a guess: identical tokens pass through, any cross-token move rejects, and
    the injector converts the rejection into a 403 — never unfiltered SQL."""
    from src.security import RowSecurityDialectError, render_predicate_for_dialect
    pred = CompiledPredicate(
        sql_expression="(\"region_code\" = 'EMEA')",
        active_rule_ids=("r1",),
        security_dimension_columns=("region_code",),
        compile_connector="mysteryql",
    )
    assert render_predicate_for_dialect(pred, "mysteryql") == pred.sql_expression
    with pytest.raises(RowSecurityDialectError):
        render_predicate_for_dialect(pred, "bigquery")
    pg = CompiledPredicate(
        sql_expression="(\"region_code\" = 'EMEA')",
        active_rule_ids=("r1",),
        security_dimension_columns=("region_code",),
        compile_connector="postgresql",
    )
    with pytest.raises(RowSecurityDialectError):
        render_predicate_for_dialect(pg, "mysteryql")
    with pytest.raises(HTTPException) as exc:
        _inject_security_where("SELECT region_code FROM agg_sales", pred, dialect="bigquery")
    assert exc.value.status_code == 403


def test_bug8396_renamed_physical_predicate_converts_cross_dialect():
    """The Bug-7033-F1 rename block hands the injector a SimpleNamespace with
    PHYSICAL column names in the SOURCE dialect and a restamped
    compile_connector; the conversion must carry the physical column intact
    into the aggregate dialect (no coverage existed for this combination)."""
    renamed = types.SimpleNamespace(
        sql_expression="\"dim_region_region_code\" = 'NORTH'",
        active_rule_ids=("r1",),
        security_dimension_columns=("dim_region_region_code",),
        mapping_source_ids=(),
        applied_rules=(),
        compile_connector="postgresql",
    )
    out = _inject_security_where(
        "SELECT dim_region_region_code, SUM(amount) AS amt "
        "FROM agg_sales GROUP BY dim_region_region_code",
        renamed, dialect="bigquery",
    )
    ast = sqlglot.parse_one(out, read="bigquery")
    where = ast.find(exp.Where)
    cols = {c.name for c in where.find_all(exp.Column)}
    assert "dim_region_region_code" in cols, out
    literals = {l.this for l in where.find_all(exp.Literal) if l.is_string}
    assert "dim_region_region_code" not in literals, out


# ---------------------------------------------------------------------------
# R-001: RLS aggregate serving must not 403 when the SOURCE owner is populated
# ---------------------------------------------------------------------------

_PATCH_OWNERS = "shared.security.predicate_compiler._load_security_column_owners"


async def test_r001_rls_aggregate_served_when_owners_populated():
    """R-001 regression: with the security column's real SOURCE owner populated
    (the production shape ``_load_security_column_owners`` returns), an RLS-safe
    aggregate must still SERVE, not 403. The aggregate rewrite scans a single
    materialised aggregate table, never the source owner (``sales``), so the
    owner-not-scanned fail-closed guard (F-007-05/Bug-8896 — a source self-join
    disambiguation) must be suppressed on this single-scan acceleration path.

    Escape guarded: the RLS aggregate serving tests compile against a mock DB
    with empty ``security_column_owners``, so the owners-populated path here was
    never exercised."""
    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m)])

    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])

    rule = _role_rule(
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["region_manager_north"],
    )
    principal = Principal(
        user_identity="alice@x", roles=frozenset({"region_manager_north"})
    )
    db = _db_returning([rule])

    with (
        patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load,
        patch(_PATCH_OWNERS, new_callable=AsyncMock) as mock_owners,
    ):
        mock_load.return_value = [agg]
        mock_owners.return_value = (("region_code", "sales"),)
        decision = await route_query(bq, db, principal=principal)

    # Served (NOT 403): predicate injected onto the single aggregate scan.
    assert decision.route_type == "aggregate"
    assert decision.aggregate_id == str(agg.id)
    assert "\"region_code\" = 'NORTH'" in decision.rewritten_query
    assert "RLS-safe aggregate" in decision.reason
