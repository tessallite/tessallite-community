"""
Integration test suite for demo_data payment schema — query router pipeline
===========================================================================

This file contains **one test function per SQL scenario** that exercises the
full query-router pipeline (parse → bind → match → rewrite) against the
``demo_data`` payment transaction schema.

The tests use lightweight SimpleNamespace mocks for model/aggregate objects
so they run without a live database.  The semantic model mirrors what would
be set up for model "modely" in tenant "t", with all tables created from
the ``deploy/Sample-db`` schema scripts.

How to run::

    cd tessallite/services/query-router
    pytest tests/test_demo_data_integration.py -q

Sections
--------
1.  Parser — measure / dimension / grain extraction
2.  Parser — filter extraction
3.  Parser — ORDER BY / LIMIT / OFFSET
4.  Parser — COUNT variants and literal handling
5.  Parser — complex expressions and passthrough detection
6.  Parser — fingerprint determinism
7.  Aggregate matcher — grain rules
8.  Aggregate matcher — measure coverage
9.  Aggregate matcher — non-additive (Q8 exact grain) rules
10. Aggregate matcher — scoring and selection
11. Aggregate matcher — filter-only dimension grain expansion
12. Exactness validator
13. Rewriter — exact grain (no re-aggregation)
14. Rewriter — coarser grain (re-aggregation)
15. Rewriter — AVG derivation
16. Rewriter — WHERE / ORDER BY / LIMIT / OFFSET rendering
17. Rewriter — scalar wrapper preservation
18. Rewriter — table reference formatting
19. Full pipeline — route decision integration
20. Full pipeline — model / aggregation disable bypass
21. Full pipeline — passthrough and fallback
22. Multi-measure / multi-dimension queries
23. Edge cases — NULL filters, BETWEEN, LIKE, IN
"""
from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

# Import real `shared` + `shared.config` before stubbing so that later imports
# of `shared.config.bootstrap` / `shared.config.resolver` (via pocket_matcher)
# still resolve against the real package path. Also load shared.db.session up
# front so later test modules can `from shared.db.session import get_tenant_db`
# (the stub block below only inserts a fake when the real module is absent —
# importing here guarantees the real one wins).
import shared  # noqa: F401
import shared.config  # noqa: F401
import shared.db.session  # noqa: F401
import shared.schemas  # noqa: F401  (real package — prevents stub below from masking measure_formats)
import shared.schemas.pydantic_models  # noqa: F401
import shared.schemas.measure_formats  # noqa: F401

# Ensure shared.db.models stubs exist in sys.modules so that imports of
# route_query (which touches shared.db.models.Dimension etc.) succeed even
# when other test files have already injected a minimal fake module.
for _mod_name in ("shared", "shared.db", "shared.db.models",
                   "shared.db.session", "shared.schemas",
                   "shared.schemas.pydantic_models"):
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = types.ModuleType(_mod_name)
_models = sys.modules["shared.db.models"]
for _cls_name in ("AggregateColumn", "AggregateDefinition", "Measure",
                   "Dimension", "UserDefinedAttribute", "Model",
                   "HierarchyDefinition", "HierarchyLevel",
                   "DataSource", "ProjectConnection",
                   "Join", "ModelColumn", "ModelTable",
                   "PocketDefinition", "PocketPredicate", "PocketRefreshRun",
                   "SystemSetting", "SystemRestartPending",
                   "TenantSetting", "ProjectSetting", "ModelSetting",
                   "QueryLog", "QueryMissLog", "RouteLog",
                   "RowSecurityRule", "DrillThroughSet"):
    if not hasattr(_models, _cls_name):
        setattr(_models, _cls_name, type(_cls_name, (), {}))

import pytest  # noqa: E402

from src.ir.logical_query import (  # noqa: E402
    BoundQuery,
    LogicalFilter,
)
from src.parsing.sql_parser import parse_sql_to_ir  # noqa: E402
from src.rewrite.query_rewriter import rewrite_for_aggregate  # noqa: E402
from src.routing.aggregate_matcher import find_best_aggregate  # noqa: E402
from src.routing.exactness_validator import validate_aggregate_route  # noqa: E402

from conftest import (  # noqa: E402
    make_agg_col,
    make_aggregate,
    make_bound_query,
    make_dimension,
    make_measure,
)

_PATCH_LOAD = "src.routing.aggregate_matcher.load_active_aggregates"


def _ensure_model_stubs():
    """Ensure shared.db.models has all classes needed by route_query.

    test_expression_routing.py replaces shared.db.models with a minimal
    fake module at import time.  This helper patches in any missing stubs
    so that importing src.routing.router succeeds regardless of test
    collection order.
    """
    _m = sys.modules.get("shared.db.models")
    if _m is None:
        return
    for name in ("Dimension", "UserDefinedAttribute", "Model",
                 "HierarchyDefinition", "HierarchyLevel",
                 "AggregateColumn", "AggregateDefinition", "Measure",
                 "DataSource", "ProjectConnection",
                 "Join", "ModelColumn", "ModelTable"):
        if not hasattr(_m, name):
            setattr(_m, name, type(name, (), {}))
    # Also ensure sqlalchemy stubs if needed
    for mod_name in ("sqlalchemy", "sqlalchemy.ext", "sqlalchemy.ext.asyncio",
                     "sqlalchemy.orm"):
        if mod_name not in sys.modules:
            sys.modules[mod_name] = types.ModuleType(mod_name)
    sa = sys.modules["sqlalchemy"]
    if not hasattr(sa, "select"):
        sa.select = lambda *a, **kw: MagicMock()
    sa_async = sys.modules["sqlalchemy.ext.asyncio"]
    if not hasattr(sa_async, "AsyncSession"):
        sa_async.AsyncSession = type("AsyncSession", (), {})


def _import_route_query():
    """Lazily import route_query after ensuring stubs exist."""
    _ensure_model_stubs()
    from src.routing.router import route_query
    return route_query


# ---------------------------------------------------------------------------
# Demo-data semantic model factory helpers
# ---------------------------------------------------------------------------

# Measures typically derived from payment_transaction
_DEMO_MEASURES = {
    "transaction_amount": "sum",
    "fee_amount": "sum",
    "commission_amount": "sum",
    "tax_amount": "sum",
    "discount_amount": "sum",
    "refund_amount": "sum",
    "chargeback_amount": "sum",
    "settlement_amount": "sum",
    "net_amount": "sum",
    "base_amount": "sum",
    "risk_score": "avg",
    "transaction_count": "sum",
}

# Dimensions from the demo_data schema
_DEMO_DIMENSIONS = [
    "country_code",
    "region_code",
    "city_name",
    "customer_type",
    "customer_segment",
    "event_type",
    "payment_status",
    "lifecycle_stage",
    "account_type",
    "channel_code",
    "payment_method",
    "payment_scheme",
    "card_entry_mode",
    "auth_method",
    "device_type",
    "risk_decision",
    "service_type",
    "business_date",
    "source_system",
    "product_code",
    "product_name",
    "campaign_code",
    "pricing_plan",
    "merchant_category_code",
    "merchant_category",
    "transaction_currency",
    "fraud_flag",
    "aml_flag",
    "sanctions_flag",
    "dispute_flag",
    "chargeback_flag",
    "success_flag",
    "failure_flag",
    "refund_flag",
    "reversal_flag",
    "customer_id",
]


def dm(name: str) -> types.SimpleNamespace:
    """Shorthand for make_dimension with a demo_data column name."""
    return make_dimension(name)


def mm(name: str, agg: str | None = None) -> types.SimpleNamespace:
    """Shorthand for make_measure with a demo_data column name."""
    default = agg or _DEMO_MEASURES.get(name, "sum")
    additive = default not in ("min", "max", "count_distinct")
    return make_measure(name, default_agg=default, is_additive=additive)


def _make_agg(grain, columns, **kwargs):
    """Build a lightweight aggregate namespace."""
    defaults = {
        "target_schema": "t__modely",
        "physical_table_name": "agg_" + "_".join(grain[:2]) if grain else "agg_global",
    }
    defaults.update(kwargs)
    return make_aggregate(grain, columns, **defaults)


def _make_col(measure_name, stat_type, *, physical_col_name=None):
    """Build a lightweight aggregate column namespace."""
    return types.SimpleNamespace(
        physical_col_name=physical_col_name or f"{measure_name}__{stat_type}",
        stat_type=stat_type,
        measure=types.SimpleNamespace(name=measure_name) if measure_name else None,
    )


def _make_row_count_col():
    """Build the __row_count__count synthetic column."""
    return types.SimpleNamespace(
        physical_col_name="__row_count__count",
        stat_type="count",
        measure=None,
    )


def _bind_ir(
    sql: str,
    measures: list,
    dimensions: list,
    *,
    filters=None,
) -> BoundQuery:
    """Parse SQL and wrap in a BoundQuery with pre-resolved entities."""
    lq = parse_sql_to_ir(sql, "model-demo")
    model = types.SimpleNamespace(
        id="model-demo",
        slug="modely",
        status="active",
        aggregations_enabled=True,
    )
    return BoundQuery(
        logical_query=lq,
        model=model,
        resolved_measures=measures,
        resolved_dimensions=dimensions,
        resolved_filters=filters or [],
        resolved_dimensions_by_name={d.name: d for d in dimensions},
    )


# =========================================================================
# 1. Parser — measure / dimension / grain extraction
# =========================================================================


class TestParserMeasureDimensionExtraction:
    """T01-T06: Basic measure, dimension, and grain extraction from demo SQL."""

    def test_t01_sum_transaction_amount_by_country(self):
        """Single SUM measure with single GROUP BY dimension."""
        ir = parse_sql_to_ir(
            "SELECT country_code, SUM(transaction_amount) FROM payment_transaction GROUP BY country_code",
            "model-demo",
        )
        assert "transaction_amount" in ir.requested_measures
        assert "country_code" in ir.grain
        assert "country_code" in ir.requested_dimensions

    def test_t02_multiple_measures_and_dimensions(self):
        """Multiple aggregate functions with multiple GROUP BY columns."""
        ir = parse_sql_to_ir(
            "SELECT country_code, payment_status, SUM(transaction_amount), COUNT(transaction_count), AVG(fee_amount) "
            "FROM payment_transaction GROUP BY country_code, payment_status",
            "model-demo",
        )
        assert set(ir.requested_measures) == {"transaction_amount", "transaction_count", "fee_amount"}
        assert set(ir.grain) == {"country_code", "payment_status"}
        assert "country_code" in ir.requested_dimensions
        assert "payment_status" in ir.requested_dimensions

    def test_t03_no_group_by_grand_total(self):
        """Grand total query with no GROUP BY yields empty grain."""
        ir = parse_sql_to_ir(
            "SELECT SUM(transaction_amount) FROM payment_transaction",
            "model-demo",
        )
        assert ir.grain == []
        assert "transaction_amount" in ir.requested_measures

    def test_t04_bare_dimension_columns_no_aggregation(self):
        """SELECT of bare columns classifies them as dimensions."""
        ir = parse_sql_to_ir(
            "SELECT country_code, payment_status, event_type FROM payment_transaction",
            "model-demo",
        )
        assert set(ir.requested_dimensions) == {"country_code", "payment_status", "event_type"}
        assert ir.requested_measures == []

    def test_t05_aliased_measures_still_extracted(self):
        """Aliased SUM/AVG still extract the inner column as a measure."""
        ir = parse_sql_to_ir(
            "SELECT SUM(transaction_amount) AS total_amount, AVG(risk_score) AS avg_risk "
            "FROM payment_transaction",
            "model-demo",
        )
        assert "transaction_amount" in ir.requested_measures
        assert "risk_score" in ir.requested_measures

    def test_t06_mixed_aggregations_across_amount_fields(self):
        """All amount columns extracted as measures when aggregated."""
        ir = parse_sql_to_ir(
            "SELECT event_type, "
            "  SUM(transaction_amount), SUM(fee_amount), SUM(commission_amount), "
            "  SUM(tax_amount), SUM(net_amount), SUM(settlement_amount) "
            "FROM payment_transaction GROUP BY event_type",
            "model-demo",
        )
        expected = {"transaction_amount", "fee_amount", "commission_amount",
                    "tax_amount", "net_amount", "settlement_amount"}
        assert expected.issubset(set(ir.requested_measures))


# =========================================================================
# 2. Parser — filter extraction
# =========================================================================


class TestParserFilterExtraction:
    """T07-T14: WHERE clause filter parsing."""

    def test_t07_eq_filter_on_country(self):
        ir = parse_sql_to_ir(
            "SELECT SUM(transaction_amount) FROM pt WHERE country_code = 'GB' GROUP BY event_type",
            "model-demo",
        )
        f = [x for x in ir.filters if x.dimension_name == "country_code"]
        assert len(f) == 1
        assert f[0].operator == "eq"
        assert f[0].value == "GB"

    def test_t08_neq_filter(self):
        ir = parse_sql_to_ir(
            "SELECT SUM(transaction_amount) FROM pt WHERE payment_status != 'EXPIRED'",
            "model-demo",
        )
        f = [x for x in ir.filters if x.dimension_name == "payment_status"]
        assert len(f) == 1
        assert f[0].operator == "neq"

    def test_t09_gt_filter_on_amount(self):
        ir = parse_sql_to_ir(
            "SELECT SUM(transaction_amount) FROM pt WHERE transaction_amount > 1000 GROUP BY country_code",
            "model-demo",
        )
        f = [x for x in ir.filters if x.dimension_name == "transaction_amount"]
        assert len(f) == 1
        assert f[0].operator == "gt"

    def test_t10_gte_filter(self):
        ir = parse_sql_to_ir(
            "SELECT SUM(fee_amount) FROM pt WHERE risk_score >= 85",
            "model-demo",
        )
        assert any(f.operator == "gte" and f.dimension_name == "risk_score" for f in ir.filters)

    def test_t11_lt_lte_filters(self):
        ir = parse_sql_to_ir(
            "SELECT COUNT(transaction_count) FROM pt WHERE transaction_amount < 50 AND risk_score <= 10",
            "model-demo",
        )
        assert any(f.operator == "lt" and f.dimension_name == "transaction_amount" for f in ir.filters)
        assert any(f.operator == "lte" and f.dimension_name == "risk_score" for f in ir.filters)

    def test_t12_in_filter_multiple_values(self):
        ir = parse_sql_to_ir(
            "SELECT SUM(transaction_amount) FROM pt WHERE country_code IN ('GB', 'DE', 'AE', 'US') GROUP BY event_type",
            "model-demo",
        )
        f = [x for x in ir.filters if x.operator == "in"]
        assert len(f) == 1
        assert set(f[0].value) == {"GB", "DE", "AE", "US"}

    def test_t13_between_filter(self):
        ir = parse_sql_to_ir(
            "SELECT SUM(transaction_amount) FROM pt WHERE transaction_amount BETWEEN 100 AND 5000",
            "model-demo",
        )
        f = [x for x in ir.filters if x.operator == "between"]
        assert len(f) == 1
        assert f[0].value == (100, 5000)

    def test_t14_like_filter(self):
        ir = parse_sql_to_ir(
            "SELECT SUM(transaction_amount) FROM pt WHERE merchant_category LIKE '%Grocery%'",
            "model-demo",
        )
        f = [x for x in ir.filters if x.operator == "like"]
        assert len(f) == 1
        assert f[0].dimension_name == "merchant_category"

    def test_t14b_is_null_filter(self):
        ir = parse_sql_to_ir(
            "SELECT SUM(transaction_amount) FROM pt WHERE settlement_amount IS NULL",
            "model-demo",
        )
        assert any(f.operator == "is_null" and f.dimension_name == "settlement_amount" for f in ir.filters)


# =========================================================================
# 3. Parser — ORDER BY / LIMIT / OFFSET
# =========================================================================


class TestParserOrderLimitOffset:
    """T15-T18: Clause extraction."""

    def test_t15_order_by_desc(self):
        ir = parse_sql_to_ir(
            "SELECT country_code, SUM(transaction_amount) AS total "
            "FROM pt GROUP BY country_code ORDER BY total DESC",
            "model-demo",
        )
        assert len(ir.order_by) == 1
        assert ir.order_by[0][1] == "desc"

    def test_t16_order_by_multiple(self):
        ir = parse_sql_to_ir(
            "SELECT country_code, event_type, SUM(transaction_amount) "
            "FROM pt GROUP BY country_code, event_type ORDER BY country_code ASC, event_type DESC",
            "model-demo",
        )
        assert len(ir.order_by) == 2
        assert ir.order_by[0] == ("country_code", "asc")
        assert ir.order_by[1] == ("event_type", "desc")

    def test_t17_limit(self):
        ir = parse_sql_to_ir(
            "SELECT country_code FROM pt LIMIT 10",
            "model-demo",
        )
        assert ir.limit == 10
        assert ir.offset is None

    def test_t18_limit_offset(self):
        ir = parse_sql_to_ir(
            "SELECT country_code, SUM(transaction_amount) FROM pt GROUP BY country_code "
            "ORDER BY country_code LIMIT 25 OFFSET 50",
            "model-demo",
        )
        assert ir.limit == 25
        assert ir.offset == 50


# =========================================================================
# 4. Parser — COUNT variants and literal handling
# =========================================================================


class TestParserCountVariants:
    """T19-T25: COUNT(1), COUNT(*), COUNT(DISTINCT), COUNT(literal)."""

    def test_t19_count_star_maps_to_row_count(self):
        ir = parse_sql_to_ir(
            "SELECT country_code, COUNT(*) FROM pt GROUP BY country_code",
            "model-demo",
        )
        assert "__row_count" in ir.requested_measures
        literals = [e for e in ir.select_expressions if e.classification == "literal"]
        assert len(literals) == 1
        assert literals[0].inner_literal == "*"

    def test_t20_count_1_maps_to_row_count(self):
        ir = parse_sql_to_ir(
            "SELECT payment_status, COUNT(1) AS cnt FROM pt GROUP BY payment_status",
            "model-demo",
        )
        assert "__row_count" in ir.requested_measures
        literals = [e for e in ir.select_expressions if e.classification == "literal"]
        assert literals[0].inner_literal == "1"

    def test_t21_count_distinct_classified_as_analytical(self):
        ir = parse_sql_to_ir(
            "SELECT country_code, COUNT(DISTINCT customer_id) FROM pt GROUP BY country_code",
            "model-demo",
        )
        assert "customer_id" in ir.requested_measures
        analytics = [e for e in ir.select_expressions if e.classification == "analytical"]
        cd = [e for e in analytics if e.agg_function == "count_distinct"]
        assert len(cd) == 1
        assert cd[0].inner_column == "customer_id"

    def test_t22_count_column_classified_as_analytical(self):
        ir = parse_sql_to_ir(
            "SELECT event_type, COUNT(transaction_count) FROM pt GROUP BY event_type",
            "model-demo",
        )
        assert "transaction_count" in ir.requested_measures

    @pytest.mark.parametrize("literal,expected", [
        ("100", "100"),
        ("42", "42"),
        ("0", "0"),
        ("TRUE", None),
        ("-1", None),
    ])
    def test_t23_count_numeric_and_boolean_literals(self, literal, expected):
        ir = parse_sql_to_ir(
            f"SELECT country_code, COUNT({literal}) FROM pt GROUP BY country_code",
            "model-demo",
        )
        assert "__row_count" in ir.requested_measures

    def test_t24_count_string_literal(self):
        ir = parse_sql_to_ir(
            "SELECT COUNT('x') FROM pt",
            "model-demo",
        )
        assert "__row_count" in ir.requested_measures

    def test_t25_mixed_count_and_sum(self):
        """COUNT(*) + SUM in same query both extracted correctly."""
        ir = parse_sql_to_ir(
            "SELECT country_code, COUNT(*), SUM(transaction_amount) FROM pt GROUP BY country_code",
            "model-demo",
        )
        assert "__row_count" in ir.requested_measures
        assert "transaction_amount" in ir.requested_measures
        assert len(ir.select_expressions) == 3


# =========================================================================
# 5. Parser — complex expressions and passthrough detection
# =========================================================================


class TestParserPassthrough:
    """T26-T30: Complex aggregates, scalar wrappers, passthrough."""

    def test_t26_sum_complex_expr_classified_as_passthrough(self):
        """SUM(transaction_amount * fx_rate) is a passthrough expression."""
        ir = parse_sql_to_ir(
            "SELECT SUM(transaction_amount * fx_rate) FROM pt",
            "model-demo",
        )
        pts = [e for e in ir.select_expressions if e.classification == "passthrough"]
        assert len(pts) >= 1
        assert any(e.agg_function is not None for e in pts)

    def test_t27_round_sum_preserves_inner_measure(self):
        """ROUND(SUM(transaction_amount), 2) still extracts transaction_amount."""
        ir = parse_sql_to_ir(
            "SELECT ROUND(SUM(transaction_amount), 2) AS rounded_total FROM pt",
            "model-demo",
        )
        assert "transaction_amount" in ir.requested_measures
        analytics = [e for e in ir.select_expressions if e.classification == "analytical"]
        assert len(analytics) == 1
        assert analytics[0].inner_column == "transaction_amount"
        assert analytics[0].alias == "rounded_total"

    def test_t28_coalesce_sum_preserves_inner_measure(self):
        """COALESCE(SUM(fee_amount), 0) still extracts fee_amount."""
        ir = parse_sql_to_ir(
            "SELECT COALESCE(SUM(fee_amount), 0) AS safe_fee FROM pt",
            "model-demo",
        )
        assert "fee_amount" in ir.requested_measures

    def test_t29_stddev_classified_as_analytical(self):
        """Phase E: STDDEV_POP is a routable analytical expression. Its sqlglot
        key 'stddevpop' (no underscore) is normalised to the canonical
        'stddev_pop' (exact-grain materialised stat column)."""
        ir = parse_sql_to_ir(
            "SELECT STDDEV_POP(transaction_amount) FROM pt",
            "model-demo",
        )
        analytics = [e for e in ir.select_expressions if e.classification == "analytical"]
        assert len(analytics) == 1
        assert analytics[0].agg_function == "stddev_pop"
        assert analytics[0].inner_column == "transaction_amount"

    def test_t30_bare_dimensions_do_not_trigger_passthrough(self):
        ir = parse_sql_to_ir(
            "SELECT country_code, SUM(transaction_amount) FROM pt GROUP BY country_code",
            "model-demo",
        )
        has_passthrough = any(
            e.classification == "passthrough" and e.inner_column is None
            for e in ir.select_expressions
        )
        assert not has_passthrough


# =========================================================================
# 6. Parser — fingerprint determinism
# =========================================================================


class TestParserFingerprint:
    """T31-T33: Fingerprint consistency."""

    def test_t31_same_query_same_fingerprint(self):
        sql = "SELECT country_code, SUM(transaction_amount) FROM pt GROUP BY country_code"
        ir1 = parse_sql_to_ir(sql, "model-demo")
        ir2 = parse_sql_to_ir(sql, "model-demo")
        assert ir1.query_fingerprint == ir2.query_fingerprint

    def test_t32_different_measures_different_fingerprint(self):
        ir1 = parse_sql_to_ir("SELECT SUM(transaction_amount) FROM pt GROUP BY country_code", "m1")
        ir2 = parse_sql_to_ir("SELECT SUM(fee_amount) FROM pt GROUP BY country_code", "m1")
        assert ir1.query_fingerprint != ir2.query_fingerprint

    def test_t33_different_grain_different_fingerprint(self):
        ir1 = parse_sql_to_ir("SELECT SUM(transaction_amount) FROM pt GROUP BY country_code", "m1")
        ir2 = parse_sql_to_ir("SELECT SUM(transaction_amount) FROM pt GROUP BY event_type", "m1")
        assert ir1.query_fingerprint != ir2.query_fingerprint


# =========================================================================
# 7. Aggregate matcher — grain rules
# =========================================================================


class TestMatcherGrainRules:
    """T34-T39: Grain superset, subset, exact match, missing grain."""

    async def test_t34_exact_grain_match(self):
        m = mm("transaction_amount")
        agg = _make_agg(["country_code"], [make_agg_col(m)])
        bq = make_bound_query([dm("country_code")], [m])

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is agg

    async def test_t35_superset_grain_accepted(self):
        """Aggregate with [country_code, region_code] covers query on [country_code]."""
        m = mm("transaction_amount")
        agg = _make_agg(["country_code", "region_code"], [make_agg_col(m)])
        bq = make_bound_query([dm("country_code")], [m])

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is agg

    async def test_t36_subset_grain_rejected(self):
        """Aggregate with [country_code] cannot serve query on [country_code, region_code]."""
        m = mm("transaction_amount")
        agg = _make_agg(["country_code"], [make_agg_col(m)])
        bq = make_bound_query([dm("country_code"), dm("region_code")], [m])

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is None

    async def test_t37_disjoint_grain_rejected(self):
        """Aggregate on event_type cannot serve query on country_code."""
        m = mm("transaction_amount")
        agg = _make_agg(["event_type"], [make_agg_col(m)])
        bq = make_bound_query([dm("country_code")], [m])

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is None

    async def test_t38_empty_grain_matches_any_aggregate(self):
        """Grand total query (no grain) can use any aggregate."""
        m = mm("transaction_amount")
        agg = _make_agg(["country_code", "event_type"], [make_agg_col(m)])
        bq = make_bound_query([], [m])

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is agg

    async def test_t39_multi_dim_grain_all_must_match(self):
        """All query dimensions must be in aggregate grain."""
        m = mm("transaction_amount")
        agg = _make_agg(["country_code", "payment_status"], [make_agg_col(m)])
        bq = make_bound_query(
            [dm("country_code"), dm("payment_status"), dm("event_type")], [m]
        )

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is None  # missing event_type


# =========================================================================
# 8. Aggregate matcher — measure coverage
# =========================================================================


class TestMatcherMeasureCoverage:
    """T40-T43: Measures must exist in the aggregate."""

    async def test_t40_measure_present_in_aggregate(self):
        m = mm("transaction_amount")
        agg = _make_agg(["country_code"], [make_agg_col(m)])
        bq = make_bound_query([dm("country_code")], [m])

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is agg

    async def test_t41_measure_missing_from_aggregate(self):
        m_query = mm("transaction_amount")
        m_agg = mm("fee_amount")
        agg = _make_agg(["country_code"], [make_agg_col(m_agg)])
        bq = make_bound_query([dm("country_code")], [m_query])

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is None

    async def test_t42_multiple_measures_all_must_be_present(self):
        m1 = mm("transaction_amount")
        m2 = mm("fee_amount")
        agg = _make_agg(["country_code"], [make_agg_col(m1)])  # only m1
        bq = make_bound_query([dm("country_code")], [m1, m2])

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is None

    async def test_t43_row_count_synthetic_measure(self):
        """__row_count measure matches if aggregate has __row_count__count column."""
        m_rc = make_measure("__row_count", "count")
        agg = _make_agg(["country_code"], [_make_row_count_col()])
        bq = make_bound_query([dm("country_code")], [m_rc])

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is agg


# =========================================================================
# 9. Aggregate matcher — non-additive (Q8) rules
# =========================================================================


class TestMatcherNonAdditive:
    """T44-T47: Non-additive measures require exact grain match."""

    async def test_t44_count_distinct_exact_grain_passes(self):
        m = mm("customer_id", "count_distinct")
        m.is_additive = False
        agg = _make_agg(["country_code"], [make_agg_col(m, "count_distinct")])
        bq = make_bound_query([dm("country_code")], [m])

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is agg

    async def test_t45_count_distinct_extra_grain_rejected(self):
        m = mm("customer_id", "count_distinct")
        m.is_additive = False
        agg = _make_agg(["country_code", "region_code"], [make_agg_col(m, "count_distinct")])
        bq = make_bound_query([dm("country_code")], [m])

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is None

    async def test_t46_min_exact_grain_via_registry(self):
        """An explicitly non-additive (is_additive=False) MIN measure routes at
        exact grain. (MIN/MAX are mappable now — see
        TestMinMaxCoarserGrainRouting for the additive case that rolls up.)"""
        m = mm("transaction_amount", "min")
        agg = _make_agg(["country_code"], [make_agg_col(m, "min")])
        bq = make_bound_query([dm("country_code")], [m])

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is agg

    async def test_t47_max_extra_grain_rejected(self):
        m = mm("transaction_amount", "max")
        agg = _make_agg(["country_code", "event_type"], [make_agg_col(m, "max")])
        bq = make_bound_query([dm("country_code")], [m])

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is None


# =========================================================================
# 10. Aggregate matcher — scoring and selection
# =========================================================================


class TestMatcherScoring:
    """T48-T50: Picks tightest grain, breaks ties by freshness."""

    async def test_t48_picks_tightest_grain(self):
        m = mm("transaction_amount")
        agg_tight = _make_agg(["country_code"], [make_agg_col(m)], agg_id="tight")
        agg_loose = _make_agg(
            ["country_code", "region_code", "city_name"],
            [make_agg_col(m)], agg_id="loose",
        )
        bq = make_bound_query([dm("country_code")], [m])

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg_loose, agg_tight]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate.id == "tight"

    async def test_t49_tie_picks_most_recent_refresh(self):
        m = mm("transaction_amount")
        agg_old = _make_agg(["country_code"], [make_agg_col(m)], agg_id="old", age_hours=48)
        agg_new = _make_agg(["country_code"], [make_agg_col(m)], agg_id="new", age_hours=1)
        bq = make_bound_query([dm("country_code")], [m])

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg_old, agg_new]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate.id == "new"

    async def test_t50_inactive_aggregate_skipped(self):
        m = mm("transaction_amount")
        agg = _make_agg(["country_code"], [make_agg_col(m)], status="retired")
        bq = make_bound_query([dm("country_code")], [m])

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is None


# =========================================================================
# 11. Aggregate matcher — filter-only dimension grain expansion
# =========================================================================


class TestMatcherFilterGrainExpansion:
    """T51-T53: Filter dimensions expand the required grain."""

    async def test_t51_filter_dimension_not_in_grain_rejected(self):
        """WHERE payment_status='SUCCESS' requires payment_status in aggregate grain."""
        m = mm("transaction_amount")
        agg = _make_agg(["country_code"], [make_agg_col(m)])
        filters = [LogicalFilter("payment_status", "eq", "SUCCESS")]
        bq = make_bound_query([dm("country_code")], [m], filters=filters)
        bq.resolved_dimensions_by_name = {
            "country_code": dm("country_code"),
            "payment_status": dm("payment_status"),
        }

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is None

    async def test_t52_filter_dimension_in_grain_accepted(self):
        m = mm("transaction_amount")
        agg = _make_agg(["country_code", "payment_status"], [make_agg_col(m)])
        filters = [LogicalFilter("payment_status", "eq", "SUCCESS")]
        bq = make_bound_query([dm("country_code")], [m], filters=filters)
        bq.resolved_dimensions_by_name = {
            "country_code": dm("country_code"),
            "payment_status": dm("payment_status"),
        }

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is agg

    async def test_t53_multiple_filter_dims_expand_grain(self):
        m = mm("transaction_amount")
        agg = _make_agg(
            ["country_code", "payment_status", "event_type"],
            [make_agg_col(m)],
        )
        filters = [
            LogicalFilter("payment_status", "eq", "SUCCESS"),
            LogicalFilter("event_type", "in", ["SALE", "CAPTURE"]),
        ]
        bq = make_bound_query([dm("country_code")], [m], filters=filters)
        bq.resolved_dimensions_by_name = {
            "country_code": dm("country_code"),
            "payment_status": dm("payment_status"),
            "event_type": dm("event_type"),
        }

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is agg


# =========================================================================
# 12. Exactness validator
# =========================================================================


class TestExactnessValidator:
    """T54-T56: Belt-and-suspenders validation before committing route."""

    def test_t54_additive_superset_grain_valid(self):
        m = mm("transaction_amount")
        agg = _make_agg(["country_code", "region_code"], [make_agg_col(m)])
        bq = make_bound_query([dm("country_code")], [m])
        valid, reason = validate_aggregate_route(bq, agg)
        assert valid

    def test_t55_non_additive_superset_grain_invalid(self):
        m = mm("customer_id", "count_distinct")
        m.is_additive = False
        agg = _make_agg(["country_code", "region_code"], [make_agg_col(m, "count_distinct")])
        bq = make_bound_query([dm("country_code")], [m])
        valid, reason = validate_aggregate_route(bq, agg)
        assert not valid
        assert "exact grain" in reason.lower()

    def test_t56_filter_column_missing_from_aggregate_invalid(self):
        m = mm("transaction_amount")
        agg = _make_agg(["country_code"], [make_agg_col(m)])
        filters = [LogicalFilter("payment_status", "eq", "SUCCESS")]
        bq = make_bound_query([dm("country_code")], [m], filters=filters)
        valid, reason = validate_aggregate_route(bq, agg)
        assert not valid
        assert "payment_status" in reason


# =========================================================================
# 13. Rewriter — exact grain (no re-aggregation)
# =========================================================================


class TestRewriterExactGrain:
    """T57-T61: Exact grain rewrites read pre-computed columns directly."""

    def test_t57_sum_exact_grain_direct_column(self):
        m = mm("transaction_amount")
        d = dm("country_code")
        agg = _make_agg(["country_code"], [make_agg_col(m)])
        bq = _bind_ir(
            "SELECT country_code, SUM(transaction_amount) FROM pt GROUP BY country_code",
            [m], [d],
        )
        sql = rewrite_for_aggregate(bq, agg)
        assert '"transaction_amount__sum"' in sql
        assert "SUM(" not in sql
        assert "GROUP BY" not in sql

    def test_t58_count_star_exact_grain_direct_column(self):
        m_rc = make_measure("__row_count", "count")
        d = dm("country_code")
        agg = _make_agg(["country_code"], [_make_row_count_col()])
        bq = _bind_ir(
            "SELECT country_code, COUNT(*) FROM pt GROUP BY country_code",
            [m_rc], [d],
        )
        sql = rewrite_for_aggregate(bq, agg)
        assert '"__row_count__count"' in sql
        assert "SUM(" not in sql

    def test_t59_multiple_measures_exact_grain(self):
        m1 = mm("transaction_amount")
        m2 = mm("fee_amount")
        d = dm("event_type")
        agg = _make_agg(["event_type"], [make_agg_col(m1), make_agg_col(m2)])
        bq = _bind_ir(
            "SELECT event_type, SUM(transaction_amount), SUM(fee_amount) FROM pt GROUP BY event_type",
            [m1, m2], [d],
        )
        sql = rewrite_for_aggregate(bq, agg)
        assert '"transaction_amount__sum"' in sql
        assert '"fee_amount__sum"' in sql
        assert "GROUP BY" not in sql

    def test_t60_no_re_aggregation_for_exact_match(self):
        m = mm("net_amount")
        d1 = dm("country_code")
        d2 = dm("payment_status")
        agg = _make_agg(["country_code", "payment_status"], [make_agg_col(m)])
        bq = _bind_ir(
            "SELECT country_code, payment_status, SUM(net_amount) FROM pt "
            "GROUP BY country_code, payment_status",
            [m], [d1, d2],
        )
        sql = rewrite_for_aggregate(bq, agg)
        assert "SUM(" not in sql
        assert "GROUP BY" not in sql

    def test_t61_min_exact_grain_direct(self):
        m = mm("transaction_amount", "min")
        d = dm("country_code")
        agg = _make_agg(["country_code"], [_make_col("transaction_amount", "min")])
        bq = _bind_ir(
            "SELECT country_code, MIN(transaction_amount) FROM pt GROUP BY country_code",
            [m], [d],
        )
        sql = rewrite_for_aggregate(bq, agg)
        assert '"transaction_amount__min"' in sql


# =========================================================================
# 14. Rewriter — coarser grain (re-aggregation)
# =========================================================================


class TestRewriterCoarserGrain:
    """T62-T67: Coarser-grain rewrites re-aggregate using SUM/MIN/MAX."""

    def test_t62_sum_coarser_grain_re_sums(self):
        m = mm("transaction_amount")
        d = dm("country_code")
        agg = _make_agg(["country_code", "event_type"], [make_agg_col(m)])
        bq = _bind_ir(
            "SELECT country_code, SUM(transaction_amount) FROM pt GROUP BY country_code",
            [m], [d],
        )
        sql = rewrite_for_aggregate(bq, agg)
        assert 'SUM("transaction_amount__sum")' in sql
        assert "GROUP BY" in sql

    def test_t63_count_star_coarser_grain_sums_row_count(self):
        m_rc = make_measure("__row_count", "count")
        agg = _make_agg(["country_code"], [_make_row_count_col()])
        bq = _bind_ir("SELECT COUNT(*) FROM pt", [m_rc], [])
        sql = rewrite_for_aggregate(bq, agg)
        assert 'SUM("__row_count__count")' in sql

    def test_t64_grand_total_no_group_by(self):
        m = mm("transaction_amount")
        agg = _make_agg(["country_code", "event_type"], [make_agg_col(m)])
        bq = _bind_ir("SELECT SUM(transaction_amount) FROM pt", [m], [])
        sql = rewrite_for_aggregate(bq, agg)
        assert "GROUP BY" not in sql

    def test_t65_min_coarser_grain_uses_min(self):
        m = mm("transaction_amount", "min")
        agg = _make_agg(["country_code", "event_type"], [_make_col("transaction_amount", "min")])
        bq = _bind_ir("SELECT MIN(transaction_amount) FROM pt", [m], [])
        sql = rewrite_for_aggregate(bq, agg)
        assert 'MIN("transaction_amount__min")' in sql

    def test_t66_max_coarser_grain_uses_max(self):
        m = mm("transaction_amount", "max")
        agg = _make_agg(["country_code", "event_type"], [_make_col("transaction_amount", "max")])
        bq = _bind_ir("SELECT MAX(transaction_amount) FROM pt", [m], [])
        sql = rewrite_for_aggregate(bq, agg)
        assert 'MAX("transaction_amount__max")' in sql

    def test_t67_count_coarser_grain_sums_count_col(self):
        """COUNT(measure) at coarser grain -> SUM(measure__count)."""
        m = mm("transaction_count", "count")
        d = dm("country_code")
        agg = _make_agg(
            ["country_code", "event_type"],
            [_make_col("transaction_count", "count")],
        )
        bq = _bind_ir(
            "SELECT country_code, COUNT(transaction_count) FROM pt GROUP BY country_code",
            [m], [d],
        )
        sql = rewrite_for_aggregate(bq, agg)
        assert 'SUM("transaction_count__count")' in sql


# =========================================================================
# 15. Rewriter — AVG derivation
# =========================================================================


class TestRewriterAvgDerivation:
    """T68-T69: AVG is derived from SUM/COUNT columns."""

    def test_t68_avg_exact_grain_division(self):
        m = mm("risk_score", "avg")
        d = dm("country_code")
        agg = _make_agg(
            ["country_code"],
            [_make_col("risk_score", "sum"), _make_col("risk_score", "count")],
        )
        bq = _bind_ir(
            "SELECT country_code, AVG(risk_score) FROM pt GROUP BY country_code",
            [m], [d],
        )
        sql = rewrite_for_aggregate(bq, agg)
        assert '"risk_score__sum"' in sql
        assert '"risk_score__count"' in sql
        assert "NULLIF" in sql

    def test_t69_avg_coarser_grain_re_aggregation(self):
        m = mm("risk_score", "avg")
        agg = _make_agg(
            ["country_code", "event_type"],
            [_make_col("risk_score", "sum"), _make_col("risk_score", "count")],
        )
        bq = _bind_ir("SELECT AVG(risk_score) FROM pt", [m], [])
        sql = rewrite_for_aggregate(bq, agg)
        assert 'SUM("risk_score__sum")' in sql
        assert 'SUM("risk_score__count")' in sql
        assert "NULLIF" in sql


# =========================================================================
# 16. Rewriter — WHERE / ORDER BY / LIMIT / OFFSET rendering
# =========================================================================


class TestRewriterClauses:
    """T70-T75: Clause rendering in rewritten SQL."""

    def test_t70_where_eq_rendered(self):
        m = mm("transaction_amount")
        d = dm("country_code")
        f = LogicalFilter("country_code", "eq", "GB")
        agg = _make_agg(["country_code"], [make_agg_col(m)])
        bq = _bind_ir(
            "SELECT country_code, SUM(transaction_amount) FROM pt WHERE country_code = 'GB' GROUP BY country_code",
            [m], [d], filters=[f],
        )
        sql = rewrite_for_aggregate(bq, agg)
        assert "WHERE" in sql
        assert "'GB'" in sql

    def test_t71_where_in_rendered(self):
        m = mm("transaction_amount")
        d = dm("event_type")
        f = LogicalFilter("event_type", "in", ["SALE", "CAPTURE", "REFUND"])
        agg = _make_agg(["event_type"], [make_agg_col(m)])
        bq = _bind_ir(
            "SELECT event_type, SUM(transaction_amount) FROM pt "
            "WHERE event_type IN ('SALE','CAPTURE','REFUND') GROUP BY event_type",
            [m], [d], filters=[f],
        )
        sql = rewrite_for_aggregate(bq, agg)
        assert "IN (" in sql
        assert "'SALE'" in sql

    def test_t72_where_between_rendered(self):
        m = mm("transaction_amount")
        d = dm("country_code")
        f = LogicalFilter("transaction_amount", "between", (100, 5000))
        agg = _make_agg(["country_code"], [make_agg_col(m)])
        bq = _bind_ir(
            "SELECT country_code, SUM(transaction_amount) FROM pt "
            "WHERE transaction_amount BETWEEN 100 AND 5000 GROUP BY country_code",
            [m], [d], filters=[f],
        )
        sql = rewrite_for_aggregate(bq, agg)
        assert "BETWEEN 100 AND 5000" in sql

    def test_t73_order_by_rendered(self):
        m = mm("transaction_amount")
        d = dm("country_code")
        agg = _make_agg(["country_code"], [make_agg_col(m)])
        bq = _bind_ir(
            "SELECT country_code, SUM(transaction_amount) FROM pt "
            "GROUP BY country_code ORDER BY country_code DESC",
            [m], [d],
        )
        sql = rewrite_for_aggregate(bq, agg)
        assert "ORDER BY" in sql
        assert "DESC" in sql

    def test_t74_limit_rendered(self):
        m = mm("transaction_amount")
        d = dm("country_code")
        agg = _make_agg(["country_code"], [make_agg_col(m)])
        bq = _bind_ir(
            "SELECT country_code, SUM(transaction_amount) FROM pt "
            "GROUP BY country_code LIMIT 20",
            [m], [d],
        )
        sql = rewrite_for_aggregate(bq, agg)
        assert "LIMIT 20" in sql

    def test_t75_offset_rendered(self):
        m = mm("transaction_amount")
        d = dm("country_code")
        agg = _make_agg(["country_code"], [make_agg_col(m)])
        bq = _bind_ir(
            "SELECT country_code, SUM(transaction_amount) FROM pt "
            "GROUP BY country_code LIMIT 10 OFFSET 5",
            [m], [d],
        )
        sql = rewrite_for_aggregate(bq, agg)
        assert "LIMIT 10" in sql
        assert "OFFSET 5" in sql


# =========================================================================
# 17. Rewriter — scalar wrapper preservation
# =========================================================================


class TestRewriterScalarWrappers:
    """T76-T77: Scalar functions wrapping aggregates are preserved."""

    def test_t76_round_sum_preserved(self):
        m = mm("transaction_amount")
        d = dm("country_code")
        agg = _make_agg(["country_code"], [make_agg_col(m)])
        bq = _bind_ir(
            "SELECT country_code, ROUND(SUM(transaction_amount), 2) AS rounded "
            "FROM pt GROUP BY country_code",
            [m], [d],
        )
        sql = rewrite_for_aggregate(bq, agg)
        assert "ROUND(" in sql.upper()

    def test_t77_coalesce_sum_preserved(self):
        m = mm("fee_amount")
        agg = _make_agg(["country_code"], [make_agg_col(m)])
        bq = _bind_ir(
            "SELECT COALESCE(SUM(fee_amount), 0) AS safe_fee FROM pt",
            [m], [],
        )
        sql = rewrite_for_aggregate(bq, agg)
        assert "COALESCE(" in sql.upper()


# =========================================================================
# 18. Rewriter — table reference formatting
# =========================================================================


class TestRewriterTableRef:
    """T78-T79: Schema-qualified vs bare table references."""

    def test_t78_schema_qualified_table(self):
        m = mm("transaction_amount")
        d = dm("country_code")
        agg = _make_agg(
            ["country_code"], [make_agg_col(m)],
            target_schema="t__modely",
            physical_table_name="agg_country_txn",
        )
        bq = _bind_ir(
            "SELECT country_code, SUM(transaction_amount) FROM pt GROUP BY country_code",
            [m], [d],
        )
        sql = rewrite_for_aggregate(bq, agg)
        assert '"t__modely"."agg_country_txn"' in sql

    def test_t79_no_schema_just_table(self):
        m = mm("transaction_amount")
        d = dm("country_code")
        agg = _make_agg(
            ["country_code"], [make_agg_col(m)],
            target_schema="",
            physical_table_name="agg_country_only",
        )
        bq = _bind_ir(
            "SELECT country_code, SUM(transaction_amount) FROM pt GROUP BY country_code",
            [m], [d],
        )
        sql = rewrite_for_aggregate(bq, agg)
        assert '"agg_country_only"' in sql
        assert '""."agg_country_only"' not in sql


# =========================================================================
# 19. Full pipeline — route decision integration
# =========================================================================


class TestFullPipelineRouting:
    """T80-T84: End-to-end parse → bind → route → rewrite."""

    async def test_t80_aggregate_route_for_country_sum(self):
        m = mm("transaction_amount")
        d = dm("country_code")
        agg = _make_agg(["country_code"], [make_agg_col(m)])
        sql = "SELECT country_code, SUM(transaction_amount) FROM pt GROUP BY country_code"
        bq = _bind_ir(sql, [m], [d])

        route_query = _import_route_query()
        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            decision = await route_query(bq, AsyncMock())
        assert decision.route_type == "aggregate"
        assert '"transaction_amount__sum"' in decision.rewritten_query

    async def test_t81_source_fallback_when_no_aggregate(self):
        m = mm("transaction_amount")
        d = dm("country_code")
        sql = "SELECT country_code, SUM(transaction_amount) FROM pt GROUP BY country_code"
        bq = _bind_ir(sql, [m], [d])

        route_query = _import_route_query()
        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = []
            decision = await route_query(bq, AsyncMock())
        assert decision.route_type == "source"
        assert decision.aggregate_id is None

    async def test_t82_aggregate_route_with_filter(self):
        m = mm("transaction_amount")
        d = dm("country_code")
        f = LogicalFilter("payment_status", "eq", "SUCCESS")
        agg = _make_agg(["country_code", "payment_status"], [make_agg_col(m)])
        sql = "SELECT country_code, SUM(transaction_amount) FROM pt WHERE payment_status = 'SUCCESS' GROUP BY country_code"
        bq = _bind_ir(sql, [m], [d], filters=[f])
        bq.resolved_dimensions_by_name["payment_status"] = dm("payment_status")

        route_query = _import_route_query()
        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            decision = await route_query(bq, AsyncMock())
        assert decision.route_type == "aggregate"
        assert "'SUCCESS'" in decision.rewritten_query

    async def test_t83_aggregate_route_preserves_limit(self):
        m = mm("transaction_amount")
        d = dm("country_code")
        agg = _make_agg(["country_code"], [make_agg_col(m)])
        sql = "SELECT country_code, SUM(transaction_amount) FROM pt GROUP BY country_code ORDER BY country_code LIMIT 5"
        bq = _bind_ir(sql, [m], [d])

        route_query = _import_route_query()
        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            decision = await route_query(bq, AsyncMock())
        assert decision.route_type == "aggregate"
        assert "LIMIT 5" in decision.rewritten_query

    async def test_t84_grain_mismatch_falls_back_to_source(self):
        m = mm("transaction_amount")
        d = dm("country_code")
        agg = _make_agg(["event_type"], [make_agg_col(m)])  # wrong grain
        sql = "SELECT country_code, SUM(transaction_amount) FROM pt GROUP BY country_code"
        bq = _bind_ir(sql, [m], [d])

        route_query = _import_route_query()
        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            decision = await route_query(bq, AsyncMock())
        assert decision.route_type == "source"


# =========================================================================
# 20. Full pipeline — model / aggregation disable bypass
# =========================================================================


class TestPipelineDisableBypasses:
    """T85-T86: Disabled model or aggregations bypass aggregate lookup."""

    async def test_t85_disabled_model_forces_source(self):
        m = mm("transaction_amount")
        d = dm("country_code")
        sql = "SELECT country_code, SUM(transaction_amount) FROM pt GROUP BY country_code"
        bq = _bind_ir(sql, [m], [d])
        bq.model.status = "disabled"

        route_query = _import_route_query()
        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            decision = await route_query(bq, AsyncMock())
        assert decision.route_type == "source"
        assert "disabled" in decision.reason.lower()
        load.assert_not_called()

    async def test_t86_aggregations_disabled_forces_source(self):
        m = mm("transaction_amount")
        d = dm("country_code")
        sql = "SELECT country_code, SUM(transaction_amount) FROM pt GROUP BY country_code"
        bq = _bind_ir(sql, [m], [d])
        bq.model.aggregations_enabled = False

        route_query = _import_route_query()
        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            decision = await route_query(bq, AsyncMock())
        assert decision.route_type == "source"
        assert "disabled" in decision.reason.lower()
        load.assert_not_called()


# =========================================================================
# 21. Full pipeline — passthrough and fallback
# =========================================================================


class TestPipelinePassthrough:
    """T87-T89: Passthrough expressions bypass aggregate matching."""

    async def test_t87_passthrough_expression_routes_to_source(self):
        """SUM(price * qty) should bypass aggregate matching."""
        m = mm("transaction_amount")
        agg = _make_agg(["country_code"], [make_agg_col(m)])
        sql = "SELECT SUM(transaction_amount * fee_amount) FROM pt"
        bq = _bind_ir(sql, [m], [])
        bq.has_passthrough_expressions = True

        route_query = _import_route_query()
        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load, patch(
            "src.routing.router.rewrite_for_source",
            new=AsyncMock(return_value=sql),
        ):
            load.return_value = [agg]
            decision = await route_query(bq, AsyncMock())
        assert decision.route_type == "source"

    async def test_t88_source_fallback_returns_raw_sql(self):
        m = mm("transaction_amount")
        d = dm("country_code")
        sql = "SELECT country_code, SUM(transaction_amount) FROM pt GROUP BY country_code"
        bq = _bind_ir(sql, [m], [d])

        route_query = _import_route_query()
        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = []
            decision = await route_query(bq, AsyncMock())
        assert decision.rewritten_query == sql

    def test_t89_stddev_routed_as_analytical_stat(self):
        """Phase E: STDDEV_POP is a routable analytical stat (exact-grain),
        normalised to canonical 'stddev_pop' — not passthrough."""
        sql = "SELECT STDDEV_POP(transaction_amount) FROM pt"
        ir = parse_sql_to_ir(sql, "model-demo")
        analytics = [e for e in ir.select_expressions if e.classification == "analytical"]
        assert len(analytics) == 1
        assert analytics[0].agg_function == "stddev_pop"


# =========================================================================
# 22. Multi-measure / multi-dimension queries
# =========================================================================


class TestMultiMeasureDimension:
    """T90-T94: Complex queries with multiple measures and dimensions."""

    def test_t90_three_dimensions_two_measures_parse(self):
        ir = parse_sql_to_ir(
            "SELECT country_code, payment_method, channel_code, "
            "  SUM(transaction_amount), SUM(fee_amount) "
            "FROM pt GROUP BY country_code, payment_method, channel_code",
            "model-demo",
        )
        assert set(ir.grain) == {"country_code", "payment_method", "channel_code"}
        assert set(ir.requested_measures) == {"transaction_amount", "fee_amount"}

    def test_t91_five_dimensions_rewrite(self):
        dims = ["country_code", "event_type", "payment_status", "channel_code", "payment_method"]
        m = mm("transaction_amount")
        ds = [dm(d) for d in dims]
        agg = _make_agg(dims, [make_agg_col(m)])
        sql = (
            f"SELECT {', '.join(dims)}, SUM(transaction_amount) FROM pt "
            f"GROUP BY {', '.join(dims)}"
        )
        bq = _bind_ir(sql, [m], ds)
        result = rewrite_for_aggregate(bq, agg)
        for d in dims:
            assert f'"{d}"' in result

    async def test_t92_four_measures_aggregate_match(self):
        m1 = mm("transaction_amount")
        m2 = mm("fee_amount")
        m3 = mm("net_amount")
        m4 = mm("settlement_amount")
        d = dm("country_code")
        cols = [make_agg_col(m1), make_agg_col(m2), make_agg_col(m3), make_agg_col(m4)]
        agg = _make_agg(["country_code"], cols)
        bq = make_bound_query([d], [m1, m2, m3, m4])

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is agg

    async def test_t93_partial_measure_coverage_rejected(self):
        m1 = mm("transaction_amount")
        m2 = mm("fee_amount")
        m3 = mm("chargeback_amount")
        d = dm("country_code")
        agg = _make_agg(["country_code"], [make_agg_col(m1), make_agg_col(m2)])
        bq = make_bound_query([d], [m1, m2, m3])

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is None

    def test_t94_count_star_with_sum_and_avg(self):
        """Mixed COUNT(*) + SUM + AVG all parsed correctly."""
        ir = parse_sql_to_ir(
            "SELECT country_code, COUNT(*), SUM(transaction_amount), AVG(risk_score) "
            "FROM pt GROUP BY country_code",
            "model-demo",
        )
        assert "__row_count" in ir.requested_measures
        assert "transaction_amount" in ir.requested_measures
        assert "risk_score" in ir.requested_measures
        assert len(ir.select_expressions) == 4


# =========================================================================
# 23. Edge cases
# =========================================================================


class TestEdgeCases:
    """T95-T100: Edge cases and corner scenarios."""

    def test_t95_select_star_sets_flag(self):
        ir = parse_sql_to_ir("SELECT * FROM pt", "model-demo")
        assert ir.select_star is True

    def test_t96_qualified_table_columns_extract_name_only(self):
        ir = parse_sql_to_ir(
            "SELECT pt.country_code, SUM(pt.transaction_amount) FROM pt GROUP BY pt.country_code",
            "model-demo",
        )
        assert "country_code" in ir.grain
        assert "transaction_amount" in ir.requested_measures

    def test_t97_subquery_in_from_does_not_pollute_measures(self):
        """Parser should not recurse into subqueries in FROM clause."""
        ir = parse_sql_to_ir(
            "SELECT country_code, SUM(transaction_amount) FROM "
            "(SELECT country_code, transaction_amount, fee_amount FROM pt) sub "
            "GROUP BY country_code",
            "model-demo",
        )
        assert "transaction_amount" in ir.requested_measures
        # fee_amount is in the subquery but NOT in the outer SELECT
        assert "fee_amount" not in ir.requested_measures

    def test_t98_where_is_null_produces_filter(self):
        ir = parse_sql_to_ir(
            "SELECT SUM(transaction_amount) FROM pt WHERE customer_id IS NULL",
            "model-demo",
        )
        assert any(f.operator == "is_null" and f.dimension_name == "customer_id" for f in ir.filters)

    async def test_t99_null_last_refreshed_at_skipped(self):
        m = mm("transaction_amount")
        agg = _make_agg(["country_code"], [make_agg_col(m)])
        agg.last_refreshed_at = None
        bq = make_bound_query([dm("country_code")], [m])

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is None

    def test_t100_model_id_and_protocol_preserved(self):
        ir = parse_sql_to_ir(
            "SELECT SUM(transaction_amount) FROM pt GROUP BY country_code",
            "model-demo",
            protocol="dax",
        )
        assert ir.model_id == "model-demo"
        assert ir.protocol == "dax"

    def test_t101_dimension_deduplication(self):
        """Duplicate dimension names in GROUP BY are deduplicated."""
        ir = parse_sql_to_ir(
            "SELECT country_code, SUM(transaction_amount) FROM pt GROUP BY country_code, country_code",
            "model-demo",
        )
        assert ir.grain.count("country_code") == 1

    def test_t102_all_demo_dimensions_parseable(self):
        """All demo_data dimension names are correctly extracted."""
        dims = ["country_code", "event_type", "payment_status", "channel_code"]
        select = ", ".join(dims)
        group = ", ".join(dims)
        ir = parse_sql_to_ir(
            f"SELECT {select}, SUM(transaction_amount) FROM pt GROUP BY {group}",
            "model-demo",
        )
        for d in dims:
            assert d in ir.grain

    def test_t103_all_demo_amount_measures_parseable(self):
        """All demo_data amount columns are correctly extracted as measures."""
        amounts = [
            "transaction_amount", "fee_amount", "commission_amount",
            "tax_amount", "discount_amount", "refund_amount",
            "chargeback_amount", "settlement_amount", "net_amount", "base_amount",
        ]
        sums = ", ".join(f"SUM({a})" for a in amounts)
        ir = parse_sql_to_ir(f"SELECT {sums} FROM pt", "model-demo")
        for a in amounts:
            assert a in ir.requested_measures, f"{a} should be extracted as a measure"

    async def test_t104_best_of_three_aggregates(self):
        """Given 3 candidates, matcher picks the tightest fresh one."""
        m = mm("transaction_amount")
        d = dm("country_code")

        agg_exact = _make_agg(["country_code"], [make_agg_col(m)], agg_id="exact", age_hours=2)
        agg_plus1 = _make_agg(["country_code", "event_type"], [make_agg_col(m)], agg_id="plus1", age_hours=1)
        agg_plus2 = _make_agg(
            ["country_code", "event_type", "payment_status"],
            [make_agg_col(m)], agg_id="plus2", age_hours=0,
        )
        bq = make_bound_query([d], [m])

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg_plus2, agg_plus1, agg_exact]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate.id == "exact"

    def test_t105_rewrite_multiple_filters_rendered(self):
        m = mm("transaction_amount")
        d = dm("country_code")
        filters = [
            LogicalFilter("country_code", "eq", "GB"),
            LogicalFilter("payment_status", "eq", "SUCCESS"),
        ]
        agg = _make_agg(["country_code", "payment_status"], [make_agg_col(m)])
        bq = _bind_ir(
            "SELECT country_code, SUM(transaction_amount) FROM pt "
            "WHERE country_code = 'GB' AND payment_status = 'SUCCESS' GROUP BY country_code",
            [m], [d], filters=filters,
        )
        sql = rewrite_for_aggregate(bq, agg)
        assert "'GB'" in sql
        assert "'SUCCESS'" in sql
        assert " AND " in sql


# =========================================================================
# Cause B — composable-expression matcher gating
# =========================================================================


class TestComposableMatcherGating:
    """SUM/SUM ratios re-aggregate at coarser grain; an explicit MAX in a
    composite forces exact grain even when the measures are sum-additive."""

    async def test_sum_ratio_routes_at_coarser_grain(self):
        bq = _bind_ir('SELECT SUM(a)/SUM(b) AS v FROM t', [mm("a", "sum"), mm("b", "sum")], [])
        agg = _make_agg(["country_code"], [_make_col("a", "sum"), _make_col("b", "sum"), _make_row_count_col()])
        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is agg

    async def test_max_ratio_blocked_at_coarser_grain(self):
        bq = _bind_ir('SELECT MAX(a)/MAX(b) AS v FROM t', [mm("a", "max"), mm("b", "max")], [])
        agg = _make_agg(["country_code"], [_make_col("a", "max"), _make_col("b", "max"), _make_row_count_col()])
        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is None

    async def test_max_ratio_routes_at_exact_grain(self):
        bq = _bind_ir('SELECT country_code, MAX(a)/MAX(b) AS v FROM t GROUP BY country_code',
                      [mm("a", "max"), mm("b", "max")], [dm("country_code")])
        agg = _make_agg(["country_code"], [_make_col("a", "max"), _make_col("b", "max"), _make_row_count_col()])
        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is agg

    async def test_max_ratio_over_sum_measures_blocked_when_only_sum_columns(self):
        # HIGH-1 regression: composable explicit MAX over SUM-default (additive)
        # measures must require (measure, max) coverage, not (measure, sum). An
        # aggregate holding only sum columns must NOT match — otherwise the
        # rewriter emits MAX(a__sum)/MAX(b__sum), a ratio of grouped sums.
        bq = _bind_ir('SELECT MAX(a)/MAX(b) AS v FROM t', [mm("a", "sum"), mm("b", "sum")], [])
        agg = _make_agg(["country_code"], [_make_col("a", "sum"), _make_col("b", "sum"), _make_row_count_col()])
        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is None

    async def test_max_ratio_over_sum_measures_routes_when_max_columns_present(self):
        # The positive mirror: with the correct max columns present, the same
        # MAX(a)/MAX(b) over additive measures routes (MAX re-aggregates).
        bq = _bind_ir('SELECT MAX(a)/MAX(b) AS v FROM t', [mm("a", "sum"), mm("b", "sum")], [])
        agg = _make_agg(["country_code"], [
            _make_col("a", "max"), _make_col("b", "max"),
            _make_col("a", "sum"), _make_col("b", "sum"), _make_row_count_col(),
        ])
        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is agg

    async def test_mixed_sum_and_max_composite_requires_both_stats(self):
        # SUM(a)/MAX(b) over additive measures: needs (a,sum) AND (b,max). An
        # aggregate with a__sum but only b__sum (no b__max) must be rejected.
        bq = _bind_ir('SELECT SUM(a)/MAX(b) AS v FROM t', [mm("a", "sum"), mm("b", "sum")], [])
        agg = _make_agg(["country_code"], [_make_col("a", "sum"), _make_col("b", "sum"), _make_row_count_col()])
        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is None

    async def test_count_only_composite_routes_via_row_count(self):
        # MEDIUM-1 regression: a pure count-only composable expression
        # (COUNT(*)/COUNT(*)) registers no resolved measure, yet it is exactly
        # serviceable by a row-count aggregate: SUM(__row_count) / SUM(__row_count).
        # The early "no measures, no grain" bail must NOT strand it on the
        # source path when an aggregate carries the row-count column.
        bq = _bind_ir('SELECT COUNT(*)/COUNT(*) AS v FROM t', [], [])
        agg = _make_agg(["country_code"], [_make_col("a", "sum"), _make_row_count_col()])
        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is agg

    async def test_count_only_composite_blocked_without_row_count(self):
        # The negative mirror: an aggregate with no row-count column cannot
        # serve a count-only composite and must fall back to source.
        bq = _bind_ir('SELECT COUNT(*)/COUNT(*) AS v FROM t', [], [])
        agg = _make_agg(["country_code"], [_make_col("a", "sum")])
        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is None


# =========================================================================
# Cause C — MIN/MAX re-aggregate at coarser grain (no longer exact_grain).
# Q8 exact-grain was only for COUNT DISTINCT / median.
# =========================================================================


class TestMinMaxCoarserGrainRouting:

    async def test_max_additive_measure_routes_at_coarser_grain(self):
        m = make_measure("a", default_agg="max", is_additive=True)
        bq = _bind_ir('SELECT MAX(a) AS v FROM t', [m], [])
        agg = _make_agg(["country_code"], [_make_col("a", "max"), _make_row_count_col()])
        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is agg

    async def test_min_additive_measure_routes_at_coarser_grain(self):
        m = make_measure("a", default_agg="min", is_additive=True)
        bq = _bind_ir('SELECT MIN(a) AS v FROM t', [m], [])
        agg = _make_agg(["country_code"], [_make_col("a", "min"), _make_row_count_col()])
        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is agg

    async def test_count_distinct_still_exact_grain_only(self):
        # Q8: COUNT(DISTINCT) is NOT re-aggregatable — must stay exact grain.
        m = make_measure("a", default_agg="count_distinct", is_additive=False)
        bq = _bind_ir('SELECT COUNT(DISTINCT a) AS v FROM t', [m], [])
        agg = _make_agg(["country_code"], [_make_col("a", "count_distinct"), _make_row_count_col()])
        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())
        assert result.aggregate is None


# =========================================================================
# Phase D — percentile matcher gating (exact grain + pNN column required)
# =========================================================================


class TestPercentileMatcherGating:

    async def test_median_blocked_at_coarser_grain(self):
        m = make_measure("amount", default_agg="sum", is_additive=True)
        agg = _make_agg(["country_code"], [_make_col("amount", "p50"), _make_col("amount", "sum"), _make_row_count_col()])
        bq = _bind_ir('SELECT MEDIAN(amount) AS m FROM t', [m], [])
        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            r = await find_best_aggregate(bq, AsyncMock())
        assert r.aggregate is None

    async def test_median_routes_at_exact_grain(self):
        m = make_measure("amount", default_agg="sum", is_additive=True)
        agg = _make_agg(["country_code"], [_make_col("amount", "p50"), _make_col("amount", "sum"), _make_row_count_col()])
        bq = _bind_ir('SELECT country_code, MEDIAN(amount) AS m FROM t GROUP BY country_code', [m], [dm("country_code")])
        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            r = await find_best_aggregate(bq, AsyncMock())
        assert r.aggregate is agg

    async def test_median_without_quantile_column_falls_to_source(self):
        m = make_measure("amount", default_agg="sum", is_additive=True)
        agg = _make_agg(["country_code"], [_make_col("amount", "sum"), _make_row_count_col()])
        bq = _bind_ir('SELECT country_code, MEDIAN(amount) AS m FROM t GROUP BY country_code', [m], [dm("country_code")])
        with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
            load.return_value = [agg]
            r = await find_best_aggregate(bq, AsyncMock())
        assert r.aggregate is None
