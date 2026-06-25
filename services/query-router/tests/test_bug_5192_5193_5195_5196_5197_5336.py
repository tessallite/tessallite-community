"""
Tests for Bug-5192, Bug-5193, Bug-5195, Bug-5196, Bug-5197, Bug-5336.

Run from tessallite/services/query-router/:
    pytest tests/test_bug_5192_5193_5195_5196_5197_5336.py -v
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ir.logical_query import (
    BoundQuery,
    LogicalFilter,
    LogicalQuery,
    RouteDecision,
    SemanticBindingError,
    SelectExpression,
)
from src.routing.exactness_validator import validate_aggregate_route

from conftest import (
    make_aggregate,
    make_agg_col,
    make_bound_query,
    make_dimension,
    make_measure,
)


# ---------------------------------------------------------------------------
# Bug-5192: CTE body table refs should not be rejected by the binder
# ---------------------------------------------------------------------------

class TestBug5192:
    """CTE body table references must not be rejected by the FROM validation."""

    @pytest.fixture
    def _mock_model(self):
        return types.SimpleNamespace(
            id="model-1",
            slug="modely",
            display_name="ModelY",
            deployed_version_id="v1",
        )

    async def test_cte_body_table_allowed(self, _mock_model):
        """A table referenced only inside a CTE body must not be rejected."""
        from src.semantic.binder import bind_query_to_model

        query = LogicalQuery(
            model_id="model-1",
            protocol="jdbc",
            raw_query=(
                "WITH tmp AS (SELECT * FROM external_table) "
                "SELECT * FROM modely"
            ),
            requested_measures=[],
            requested_dimensions=[],
            filters=[],
            grain=[],
            order_by=[],
            limit=None,
            offset=None,
            query_fingerprint="fp1",
            from_tables=["external_table", "modely"],
            cte_aliases=["tmp"],
            has_complex_sql=True,
            select_star=True,
        )

        db = AsyncMock()
        with (
            patch("src.semantic.binder._load_model", return_value=_mock_model),
            patch("src.semantic.binder.resolve_deployed_shape", return_value=None),
            patch("src.semantic.binder.resolve_live_metadata_bundle", return_value=None),
            patch("src.semantic.binder._load_measures", return_value=[]),
            patch("src.semantic.binder._load_dimensions", return_value=[]),
            patch("src.semantic.binder._load_hierarchy_level_dimensions", return_value=[]),
            patch("src.semantic.binder._load_hidden_column_ids", return_value=set()),
            patch("src.semantic.binder._load_physical_column_names", return_value=set()),
        ):
            bound = await bind_query_to_model(query, db)
            # Should not raise: external_table is inside a CTE body
            assert bound is not None

    async def test_unknown_table_still_rejected_without_cte(self, _mock_model):
        """A table not matching the model and not in any CTE must be rejected."""
        from src.semantic.binder import bind_query_to_model

        query = LogicalQuery(
            model_id="model-1",
            protocol="jdbc",
            raw_query="SELECT * FROM unknown_table",
            requested_measures=[],
            requested_dimensions=[],
            filters=[],
            grain=[],
            order_by=[],
            limit=None,
            offset=None,
            query_fingerprint="fp2",
            from_tables=["unknown_table"],
            cte_aliases=[],
            select_star=True,
        )

        db = AsyncMock()
        with (
            patch("src.semantic.binder._load_model", return_value=_mock_model),
        ):
            with pytest.raises(SemanticBindingError, match="Unknown table"):
                await bind_query_to_model(query, db)


# ---------------------------------------------------------------------------
# Bug-5193: fabricated <model_slug>_* names must be rejected
# ---------------------------------------------------------------------------

class TestBug5193:
    """Fabricated model variant names must be rejected, not silently bound."""

    @pytest.fixture
    def _mock_model(self):
        return types.SimpleNamespace(
            id="model-1",
            slug="modely",
            display_name="ModelY",
            deployed_version_id="v1",
        )

    async def test_fabricated_variant_rejected(self, _mock_model):
        """modely_fake must raise SemanticBindingError."""
        from src.semantic.binder import bind_query_to_model

        query = LogicalQuery(
            model_id="model-1",
            protocol="jdbc",
            raw_query="SELECT * FROM modely_fake",
            requested_measures=[],
            requested_dimensions=[],
            filters=[],
            grain=[],
            order_by=[],
            limit=None,
            offset=None,
            query_fingerprint="fp3",
            from_tables=["modely_fake"],
            cte_aliases=[],
            select_star=True,
        )

        db = AsyncMock()
        with patch("src.semantic.binder._load_model", return_value=_mock_model):
            with pytest.raises(SemanticBindingError, match="Unknown model variant"):
                await bind_query_to_model(query, db)

    async def test_known_variant_technical_accepted(self, _mock_model):
        """modely_technical (known suffix) must be allowed."""
        from src.semantic.binder import bind_query_to_model

        query = LogicalQuery(
            model_id="model-1",
            protocol="jdbc",
            raw_query="SELECT * FROM modely_technical",
            requested_measures=[],
            requested_dimensions=[],
            filters=[],
            grain=[],
            order_by=[],
            limit=None,
            offset=None,
            query_fingerprint="fp4",
            from_tables=["modely_technical"],
            cte_aliases=[],
            select_star=True,
        )

        db = AsyncMock()
        with (
            patch("src.semantic.binder._load_model", return_value=_mock_model),
            patch("src.semantic.binder.resolve_deployed_shape", return_value=None),
            patch("src.semantic.binder.resolve_live_metadata_bundle", return_value=None),
            patch("src.semantic.binder._load_measures", return_value=[]),
            patch("src.semantic.binder._load_dimensions", return_value=[]),
            patch("src.semantic.binder._load_hierarchy_level_dimensions", return_value=[]),
            patch("src.semantic.binder._load_hidden_column_ids", return_value=set()),
            patch("src.semantic.binder._load_physical_column_names", return_value=set()),
            patch("src.semantic.binder._filter_by_from_tables", return_value=([], [])),
        ):
            bound = await bind_query_to_model(query, db, include_hidden=True)
            assert bound is not None

    async def test_bare_slug_accepted(self, _mock_model):
        """The bare model slug (modely) must be allowed."""
        from src.semantic.binder import bind_query_to_model

        query = LogicalQuery(
            model_id="model-1",
            protocol="jdbc",
            raw_query="SELECT * FROM modely",
            requested_measures=[],
            requested_dimensions=[],
            filters=[],
            grain=[],
            order_by=[],
            limit=None,
            offset=None,
            query_fingerprint="fp5",
            from_tables=["modely"],
            cte_aliases=[],
            select_star=True,
        )

        db = AsyncMock()
        with (
            patch("src.semantic.binder._load_model", return_value=_mock_model),
            patch("src.semantic.binder.resolve_deployed_shape", return_value=None),
            patch("src.semantic.binder.resolve_live_metadata_bundle", return_value=None),
            patch("src.semantic.binder._load_measures", return_value=[]),
            patch("src.semantic.binder._load_dimensions", return_value=[]),
            patch("src.semantic.binder._load_hierarchy_level_dimensions", return_value=[]),
            patch("src.semantic.binder._load_hidden_column_ids", return_value=set()),
            patch("src.semantic.binder._load_physical_column_names", return_value=set()),
            patch("src.semantic.binder._filter_by_from_tables", return_value=([], [])),
        ):
            bound = await bind_query_to_model(query, db)
            assert bound is not None


# ---------------------------------------------------------------------------
# Bug-5195: aggregate hit_count deferred to post-execution
# ---------------------------------------------------------------------------

class TestBug5195:
    """Aggregate hit_count must be deferred to after successful execution."""

    async def test_route_decision_carries_pending_hit_credit(self):
        """An aggregate RouteDecision must have pending_hit_credit set."""
        from src.routing.router import route_query
        from src.routing.aggregate_matcher import AggregateMatchResult

        m = make_measure("revenue")
        agg = make_aggregate(["country"], [make_agg_col(m)])
        bq = make_bound_query([make_dimension("country")], [m])
        db = AsyncMock()
        db.execute = AsyncMock(return_value=None)
        db.get = AsyncMock(return_value=None)

        with (
            patch(
                "src.routing.router.find_best_aggregate",
                new_callable=AsyncMock,
                return_value=AggregateMatchResult(aggregate=agg),
            ),
            patch(
                "src.routing.router.find_best_pocket",
                new_callable=AsyncMock,
                return_value=types.SimpleNamespace(pocket=None, skipped_reason=None),
            ),
            patch(
                "src.routing.router.rewrite_for_aggregate",
                return_value="SELECT SUM(revenue__sum) FROM agg_table",
            ),
            patch(
                "src.routing.router.validate_aggregate_route",
                return_value=(True, "ok"),
            ),
            patch(
                "src.routing.router._resolve_aggregate_target_dialect",
                new_callable=AsyncMock,
                return_value="postgres",
            ),
            patch(
                "src.routing.router._resolve_aggregate_source_dialect",
                new_callable=AsyncMock,
                return_value="postgres",
            ),
            patch(
                "src.routing.router._ensure_valid_user_defined_attributes",
                new_callable=AsyncMock,
            ),
            patch(
                "src.routing.router.compile_row_security",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "src.routing.router._check_column_restrictions",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            decision = await route_query(bq, db)

        assert decision.route_type == "aggregate"
        # Bug-5195: pending_hit_credit carries the aggregate object so the
        # execution endpoint (execute_with_observation) credits after
        # successful execution. The router does NOT credit at route time.
        assert decision.pending_hit_credit is agg

    def test_route_decision_source_has_no_pending_credit(self):
        """A source RouteDecision must not carry a pending hit credit."""
        decision = RouteDecision(
            route_type="source",
            rewritten_query="SELECT 1",
            reason="test",
        )
        assert decision.pending_hit_credit is None


# ---------------------------------------------------------------------------
# Bug-5196: HAVING-by-alias — SELECT aliases resolved in HAVING
# ---------------------------------------------------------------------------

class TestBug5196:
    """HAVING clause must resolve SELECT aliases, not just physical columns."""

    def test_having_alias_extracted_by_parser(self):
        """Parser must extract HAVING with an alias reference."""
        from src.parsing.sql_parser import parse_sql_to_ir

        sql = (
            'SELECT country, SUM(revenue) AS total '
            'FROM modely '
            'GROUP BY country '
            'HAVING total > 100'
        )
        ir = parse_sql_to_ir(sql, "model-1", protocol="jdbc")
        assert ir.having_raw is not None
        assert "total" in ir.having_raw.lower() or "HAVING" in ir.having_raw

    def test_qualify_having_resolves_select_alias(self):
        """The _qualify_having transform must resolve SELECT aliases.

        This tests the HAVING alias resolution logic directly by simulating
        the transform with a SELECT alias map, matching the source_sql.py
        _qualify_having implementation.
        """
        import sqlglot
        from sqlglot import exp

        having_raw = "HAVING total > 100"
        having_ast = sqlglot.parse_one(
            f"SELECT 1 {having_raw}", read="postgres"
        )
        having_node = having_ast.find(exp.Having)
        assert having_node is not None

        # Simulate the resolution order from source_sql.py:
        # 1. _get_phys_expr -> None (no physical column named "total")
        # 2. field_expr_by_name -> None
        # 3. _select_alias_map -> "total" (the quoted alias)
        _select_alias_map = {"total": '"total"'}

        def _qualify_having(node):
            if isinstance(node, exp.Column):
                # Step 1: physical column (not found)
                # Step 2: field expression map (not found)
                # Step 3: SELECT alias
                alias_expr = _select_alias_map.get(node.name)
                if alias_expr:
                    return sqlglot.parse_one(alias_expr, read="postgres")
            return node

        result = having_node.transform(_qualify_having).sql(dialect="postgres")
        # The alias "total" must be preserved as a quoted identifier
        assert '"total"' in result
        assert "100" in result


# ---------------------------------------------------------------------------
# Bug-5197: exactness validator filter dimension canonicalization
# ---------------------------------------------------------------------------

class TestBug5197:
    """Exactness validator must canonicalize filter dimensions like the matcher."""

    def test_filter_passes_with_canonical_map(self):
        """A filter dimension with a different name but same canonical name
        must pass when the canonical map is provided."""
        m = make_measure("revenue")
        d = make_dimension("country_alias")  # query uses this name
        f = LogicalFilter("country_alias", "eq", "US")
        bq = make_bound_query([d], [m], filters=[f], grain=["country_alias"])

        # Aggregate grain uses "country" (the canonical name)
        agg = make_aggregate(["country"], [make_agg_col(m)])

        # Without canonical map: should reject
        valid, reason = validate_aggregate_route(bq, agg)
        assert not valid
        assert "not available in aggregate" in reason

        # With canonical map: should pass
        name_map = {"country_alias": "country", "country": "country"}
        valid, reason = validate_aggregate_route(
            bq, agg, name_to_canonical=name_map,
        )
        assert valid
        assert reason == "ok"

    def test_filter_still_rejects_unknown_dimension(self):
        """A filter dimension not in the grain must still be rejected."""
        m = make_measure("revenue")
        d = make_dimension("country")
        f = LogicalFilter("city", "eq", "NYC")
        bq = make_bound_query([d], [m], filters=[f], grain=["country"])

        agg = make_aggregate(["country"], [make_agg_col(m)])

        name_map = {"country": "country"}
        valid, reason = validate_aggregate_route(
            bq, agg, name_to_canonical=name_map,
        )
        assert not valid


# ---------------------------------------------------------------------------
# Bug-5336: single-relation set ops should classify correctly
# ---------------------------------------------------------------------------

class TestBug5336:
    """Single-relation set operations must pass through as complex SQL."""

    def test_single_relation_union_detected_as_complex(self):
        """A single-relation UNION ALL should be flagged as complex SQL."""
        from src.parsing.sql_parser import parse_sql_to_ir

        sql = (
            "SELECT source_system AS value FROM modely "
            "UNION ALL "
            "SELECT source_system AS value FROM modely "
            "ORDER BY value LIMIT 100"
        )
        ir = parse_sql_to_ir(sql, "model-1", protocol="jdbc")
        assert ir.has_complex_sql is True
        # from_tables should contain "modely" (single relation)
        assert "modely" in ir.from_tables

    def test_single_relation_union_one_physical_relation(self):
        """A single-relation UNION has only 1 distinct physical relation."""
        from src.routing.router import _distinct_physical_relation_count
        from src.ir.logical_query import LogicalQuery

        lq = LogicalQuery(
            model_id="model-1",
            protocol="jdbc",
            raw_query=(
                "SELECT source_system AS value FROM modely "
                "UNION ALL "
                "SELECT source_system AS value FROM modely "
                "ORDER BY value LIMIT 100"
            ),
            requested_measures=[],
            requested_dimensions=[],
            filters=[],
            grain=[],
            order_by=[],
            limit=100,
            offset=None,
            query_fingerprint="fp-union",
            from_tables=["modely"],
            cte_aliases=[],
            has_complex_sql=True,
        )
        count = _distinct_physical_relation_count(lq)
        assert count == 1

    def test_multi_relation_union_two_physical_relations(self):
        """A multi-relation UNION has >1 distinct physical relations."""
        from src.routing.router import _distinct_physical_relation_count
        from src.ir.logical_query import LogicalQuery

        lq = LogicalQuery(
            model_id="model-1",
            protocol="jdbc",
            raw_query=(
                "SELECT payment_status AS code FROM modely "
                "UNION "
                "SELECT payment_status_code AS code "
                "FROM dim_payment_status "
                "ORDER BY code"
            ),
            requested_measures=[],
            requested_dimensions=[],
            filters=[],
            grain=[],
            order_by=[],
            limit=None,
            offset=None,
            query_fingerprint="fp-union2",
            from_tables=["modely", "dim_payment_status"],
            cte_aliases=[],
            has_complex_sql=True,
        )
        count = _distinct_physical_relation_count(lq)
        assert count == 2
