"""
Tests for Fable advisory deep-review fixes (2026-07-13):
  - Bug-7178-F1: calc-measure aggregate expansion uses correct stat
  - Bug-7178-F2: bare calc measure in passthrough branch uses expansion
  - Bug-7033-F1: RLS-safe aggregate serving uses physical grain columns
  - Bug-7033-F1 fail-closed: collision-renamed RLS aggregate rename failure
    falls back to source
  - Bug-7039-F2: cross-source mapping validation with empty fact sources

Run from tessallite/services/query-router/:
    pytest tests/test_fable_qr_agg_fixes.py -v
"""
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from src.routing.aggregate_matcher import (
    AggregateMatchResult,
    find_best_aggregate,
)
from src.routing.router import route_query
from src.rewrite.aggregate import rewrite_for_aggregate, _build_col_lookup
from src.ir.logical_query import LogicalQuery, BoundQuery, SelectExpression
from src.security import Principal

from conftest import (
    make_measure,
    make_dimension,
    make_agg_col,
    make_aggregate,
    make_bound_query,
)
from test_query_flow import _bind

_PATCH = "src.routing.aggregate_matcher.load_active_aggregates"
_PATCH_INACTIVE = "src.routing.aggregate_matcher.load_inactive_aggregates"


# ---------------------------------------------------------------------------
# Bug-7178-F1: calc-measure expansion reads the CORRECT stat column
# ---------------------------------------------------------------------------

class TestBug7178F1CalcMeasureCorrectStat:
    """When a calculated measure is expanded from its base measures'
    aggregate columns, the rewriter must use the EXACT stat_type verified
    by the matcher (the base measure's default_agg), not an arbitrary
    first-stored stat via the (name, None) fuzzy fallback.

    Scenario: profit_margin = safe_div(measure("gm"), measure("revenue"))
    where gm.default_agg="sum" and revenue.default_agg="sum".  The
    aggregate stores both __sum and __count for each base measure.  The
    expansion must read gm__sum / revenue__sum, NOT gm__count or
    revenue__count (which the fuzzy fallback could pick if count happened
    to be stored first).
    """

    async def test_matcher_carries_stat_type_pairs(self):
        """The matcher's calc_expandable_measures must carry
        (base_name, stat_type) pairs, not just names."""
        gm = make_measure("gm", default_agg="sum")
        revenue = make_measure("revenue", default_agg="sum")
        profit_margin = make_measure(
            "profit_margin",
            measure_type="calculated",
            expression='safe_div(measure("gm"), measure("revenue"))',
            calc_agg_mode="expression_as_written",
            is_additive=False,
        )

        # Aggregate has both sum and count for each base measure
        agg = make_aggregate(
            ["country"],
            [
                make_agg_col(gm, "sum"),
                make_agg_col(gm, "count"),
                make_agg_col(revenue, "sum"),
                make_agg_col(revenue, "count"),
            ],
        )

        bq = make_bound_query(
            [make_dimension("country")],
            [profit_margin],
        )

        # Bug-7784: base measures resolve from the DEPLOYED SNAPSHOT (the
        # authority the binder/exactness gate use), not the live draft ORM.
        # make_bound_query stamps a deployed model, so mock the snapshot.
        mock_db = AsyncMock()
        _ref_result = MagicMock()
        _ref_result.scalars.return_value.all.return_value = [gm, revenue]
        mock_db.execute = AsyncMock(return_value=_ref_result)
        deployed_shape = types.SimpleNamespace(measures=[profit_margin, gm, revenue])

        with (
            patch(_PATCH, new_callable=AsyncMock) as mock_load,
            patch(
                "src.semantic.snapshot_resolver.resolve_deployed_shape",
                AsyncMock(return_value=deployed_shape),
            ),
        ):
            mock_load.return_value = [agg]
            result = await find_best_aggregate(bq, mock_db)

        assert result.aggregate is not None
        assert "profit_margin" in result.calc_expandable_measures
        pairs = result.calc_expandable_measures["profit_margin"]
        # Must be (name, stat_type) tuples
        assert all(isinstance(p, tuple) and len(p) == 2 for p in pairs)
        # Must carry "sum" as the stat_type for both bases
        pair_dict = {name: stat for name, stat in pairs}
        assert pair_dict.get("gm") == "sum"
        assert pair_dict.get("revenue") == "sum"

    def test_rewriter_uses_verified_stat(self):
        """The rewriter must use the matcher-verified stat, not the fuzzy
        (name, None) fallback.  With both __sum and __count stored, the
        expansion must read the __sum columns (since default_agg="sum")."""
        gm = make_measure("gm", default_agg="sum")
        revenue = make_measure("revenue", default_agg="sum")
        profit_margin = make_measure(
            "profit_margin",
            measure_type="calculated",
            expression='safe_div(measure("gm"), measure("revenue"))',
            calc_agg_mode="expression_as_written",
            is_additive=False,
        )

        agg = make_aggregate(
            ["country"],
            [
                make_agg_col(gm, "sum"),
                make_agg_col(gm, "count"),
                make_agg_col(revenue, "sum"),
                make_agg_col(revenue, "count"),
            ],
        )
        agg.grain_physical_cols = ["country"]

        bq = make_bound_query(
            [make_dimension("country")],
            [profit_margin],
        )
        bq.logical_query.select_expressions = [
            SelectExpression(
                raw_text="profit_margin",
                alias="profit_margin",
                classification="analytical",
                inner_column="profit_margin",
                agg_function=None,
                inner_literal=None,
            ),
        ]
        bq.resolved_dimensions_by_name = {"country": make_dimension("country")}

        # calc_expandable_measures carries (name, stat) pairs
        sql = rewrite_for_aggregate(
            bq, agg, "postgres",
            calc_expandable_measures={
                "profit_margin": [("gm", "sum"), ("revenue", "sum")],
            },
        )

        # Must reference gm__sum and revenue__sum, not __count
        assert "gm__sum" in sql
        assert "revenue__sum" in sql
        assert "gm__count" not in sql
        assert "revenue__count" not in sql


# ---------------------------------------------------------------------------
# Bug-7178-F2: bare calc measure in the passthrough SELECT branch
# ---------------------------------------------------------------------------

class TestBug7178F2BareCalcMeasure:
    """SELECT region, profit_margin FROM model GROUP BY region — a bare
    calc measure in the passthrough SELECT branch must be expanded from
    base measure columns, not emit a reference to a nonexistent aggregate
    column (which causes a hard DB error)."""

    def test_bare_calc_measure_expanded_in_passthrough(self):
        """A bare calc measure reference in the else/passthrough SELECT
        branch should be expanded via _calc_expanded_sql."""
        gm = make_measure("gm", default_agg="sum")
        revenue = make_measure("revenue", default_agg="sum")
        profit_margin = make_measure(
            "profit_margin",
            measure_type="calculated",
            expression='safe_div(measure("gm"), measure("revenue"))',
            calc_agg_mode="expression_as_written",
            is_additive=False,
        )

        agg = make_aggregate(
            ["region"],
            [
                make_agg_col(gm, "sum"),
                make_agg_col(revenue, "sum"),
            ],
        )
        agg.grain_physical_cols = ["region"]

        bq = make_bound_query(
            [make_dimension("region")],
            [profit_margin],
            grain=["region"],
        )
        # Bare column select: classification is not "analytical"
        bq.logical_query.select_expressions = [
            SelectExpression(
                raw_text="region",
                alias=None,
                classification="passthrough",
                inner_column="region",
                agg_function=None,
                inner_literal=None,
            ),
            SelectExpression(
                raw_text="profit_margin",
                alias=None,
                classification="passthrough",
                inner_column="profit_margin",
                agg_function=None,
                inner_literal=None,
            ),
        ]
        bq.resolved_dimensions_by_name = {"region": make_dimension("region")}

        sql = rewrite_for_aggregate(
            bq, agg, "postgres",
            calc_expandable_measures={
                "profit_margin": [("gm", "sum"), ("revenue", "sum")],
            },
        )

        # Must contain the expanded expression (CASE WHEN from safe_div)
        # and reference gm__sum / revenue__sum, NOT a nonexistent
        # "profit_margin" column on the aggregate table.
        assert "gm__sum" in sql or "CASE WHEN" in sql.upper()
        assert "revenue__sum" in sql
        # "profit_margin" must appear ONLY as an alias (AS "profit_margin"),
        # never as a source column reference.  Split the SQL on the alias
        # marker and verify no non-alias reference to the name remains.
        import re
        _pm_occurrences = [m.start() for m in re.finditer(r'"profit_margin"', sql)]
        for pos in _pm_occurrences:
            # Each occurrence must be preceded by "AS " (case-insensitive).
            _preceding = sql[max(0, pos - 3):pos].strip()
            assert _preceding.upper().endswith("AS"), (
                f'"profit_margin" at position {pos} is not an alias — '
                f'it appears as a source column reference in: {sql}'
            )


# ---------------------------------------------------------------------------
# Bug-7033-F1: RLS-safe aggregate uses PHYSICAL grain columns
# ---------------------------------------------------------------------------

class TestBug7033F1RlsPhysicalGrainColumn:
    """When an aggregate has collision-renamed grain columns, the RLS
    safety check and predicate injection must use the PHYSICAL column
    names, not the LOGICAL aliases.  A model with dim_region.region_code
    that collides with another dimension's region_code gets stored as
    dim_region_region_code in the aggregate table."""

    def test_rls_safe_check_passes_with_physical_grain(self):
        """_aggregate_is_rls_safe returns True when the security column
        exists in the logical grain, even if the physical name differs."""
        from src.routing.router import _aggregate_is_rls_safe

        # Aggregate with colliding grain: logical "region_code" stored
        # physically as "dim_region_region_code"
        agg = types.SimpleNamespace(
            grain=["region_code"],
            grain_physical_cols=["dim_region_region_code"],
        )

        compiled = types.SimpleNamespace(
            security_dimension_columns=["region_code"],
            mapping_source_ids=[],
        )

        assert _aggregate_is_rls_safe(agg, compiled) is True

    def test_rls_safe_check_fails_missing_column(self):
        """_aggregate_is_rls_safe returns False when the security column
        is not in the aggregate grain at all."""
        from src.routing.router import _aggregate_is_rls_safe

        agg = types.SimpleNamespace(
            grain=["country"],
            grain_physical_cols=["country"],
        )

        compiled = types.SimpleNamespace(
            security_dimension_columns=["region_code"],
            mapping_source_ids=[],
        )

        assert _aggregate_is_rls_safe(agg, compiled) is False

    def test_physical_column_map_resolves_collision(self):
        """_rls_security_col_physical_map builds the correct
        logical->physical mapping for collision-renamed columns."""
        from src.routing.router import _rls_security_col_physical_map

        agg = types.SimpleNamespace(
            grain=["region_code", "country"],
            grain_physical_cols=["dim_region_region_code", "country"],
        )

        compiled = types.SimpleNamespace(
            security_dimension_columns=["region_code"],
            mapping_source_ids=[],
        )

        phys_map = _rls_security_col_physical_map(agg, compiled)
        assert phys_map["region_code"] == "dim_region_region_code"

    def test_no_collision_physical_equals_logical(self):
        """When no collision, physical == logical."""
        from src.routing.router import _rls_security_col_physical_map

        agg = types.SimpleNamespace(
            grain=["region_code"],
            grain_physical_cols=["region_code"],
        )

        compiled = types.SimpleNamespace(
            security_dimension_columns=["region_code"],
            mapping_source_ids=[],
        )

        phys_map = _rls_security_col_physical_map(agg, compiled)
        assert phys_map["region_code"] == "region_code"


# ---------------------------------------------------------------------------
# Bug-7033-F1 FAIL-CLOSED: collision-renamed RLS aggregate whose sqlglot
# rename leaves a logical security column UNREPLACED must fall back to the
# RLS-protected SOURCE route — never serve the aggregate with a partial or
# incomplete security predicate.
# ---------------------------------------------------------------------------

class TestBug7033F1RenameFailClosed:
    """Fable-R2-security: when an RLS-safe aggregate has collision-renamed
    grain columns and the sqlglot transform fails to replace the logical
    security column name with the physical name, the router must fall back
    to source with the full security predicate, not serve the aggregate
    with a broken (potentially always-true) predicate.

    The fail-closed contract is at router.py ~886-925: the ``except
    Exception`` clause sets ``_injection_compiled = None``, which skips the
    aggregate route and falls through to the RLS-protected source route.
    """

    _PATCH_LOAD = "src.routing.aggregate_matcher.load_active_aggregates"
    _PATCH_INACTIVE = "src.routing.aggregate_matcher.load_inactive_aggregates"

    async def test_rename_failure_falls_to_source_not_aggregate(self):
        """When the sqlglot predicate rename raises, the route must be
        'source' (never 'aggregate') and the security predicate must still
        be injected into the source query.

        Strategy: use a valid predicate expression (so _inject_security_where
        can parse and inject it on the source fallback), but patch
        sqlglot.parse_one to raise on its FIRST invocation inside the
        rename block while allowing the source-path injection calls to
        succeed normally.  The first call to parse_one with 'SELECT 1 WHERE'
        inside _route_with_row_security is the rename attempt; subsequent
        calls are _inject_security_where.
        """
        from src.security import CompiledPredicate, Principal
        import sqlglot as _sg

        m = make_measure("revenue")
        d = make_dimension("region_code")

        # Aggregate with collision-renamed grain: logical "region_code"
        # stored physically as "dim_region_region_code".
        agg = make_aggregate(["region_code"], [make_agg_col(m)])
        agg.grain_physical_cols = ["dim_region_region_code"]

        sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
        bq = _bind(sql, [m], [d])

        # Valid predicate that sqlglot CAN parse.
        compiled = CompiledPredicate(
            sql_expression='"region_code" = \'NORTH\'',
            active_rule_ids=("r-fail-closed",),
            security_dimension_columns=("region_code",),
            applied_rules=({"id": "r-fail-closed", "name": "test"},),
        )

        principal = Principal(
            user_identity="test@x", roles=frozenset({"viewer"}),
        )

        # Patch: make the FIRST parse_one call with "SELECT 1 WHERE"
        # raise, simulating a rename-block parse failure.  The rename
        # block is executed BEFORE _inject_security_where, so the first
        # such call is the rename; subsequent calls are the source
        # injection path that must succeed.
        _original_parse_one = _sg.parse_one
        _rename_call_count = {"n": 0}

        def _parse_one_fail_first_rename(sql_text, **kw):
            if sql_text.startswith("SELECT 1 WHERE"):
                _rename_call_count["n"] += 1
                if _rename_call_count["n"] == 1:
                    raise ValueError(
                        "Simulated rename parse failure for fail-closed test"
                    )
            return _original_parse_one(sql_text, **kw)

        with (
            patch(self._PATCH_LOAD, new_callable=AsyncMock) as mock_load,
            patch(self._PATCH_INACTIVE, new_callable=AsyncMock) as mock_inactive,
            patch("sqlglot.parse_one", side_effect=_parse_one_fail_first_rename),
        ):
            mock_load.return_value = [agg]
            mock_inactive.return_value = []
            decision = await route_query(
                bq, AsyncMock(), principal=principal,
                row_security=compiled,
            )

        # CRITICAL: the route must be "source", not "aggregate".
        # If it were "aggregate", the broken predicate would reference a
        # nonexistent column on the aggregate table, either causing a DB
        # error or (worse) being silently dropped — serving unfiltered
        # rows that violate the security policy.
        assert decision.route_type == "source", (
            f"Expected source fallback but got route_type={decision.route_type!r}; "
            f"a rename failure must never serve the aggregate"
        )
        assert decision.aggregate_id is None
        # The security rules must still be recorded on the source route.
        assert "Row security active" in decision.reason
        # Verify the predicate WAS still injected in the source query
        # (the security filter is applied, just on source instead of
        # the aggregate).
        assert "region_code" in decision.rewritten_query

    async def test_successful_rename_serves_aggregate(self):
        """Control test: when the rename SUCCEEDS (no parse error), the
        collision-renamed aggregate IS served with the corrected predicate.
        This confirms the fail-closed test above is not a false positive
        from some other cause."""
        from src.security import CompiledPredicate, Principal

        m = make_measure("revenue")
        d = make_dimension("region_code")

        agg = make_aggregate(["region_code"], [make_agg_col(m)])
        agg.grain_physical_cols = ["dim_region_region_code"]

        sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
        bq = _bind(sql, [m], [d])

        # Valid predicate that sqlglot CAN parse and rename.
        compiled = CompiledPredicate(
            sql_expression='"region_code" = \'NORTH\'',
            active_rule_ids=("r-ok",),
            security_dimension_columns=("region_code",),
            applied_rules=({"id": "r-ok", "name": "test-ok"},),
        )

        principal = Principal(
            user_identity="test@x", roles=frozenset({"viewer"}),
        )

        with (
            patch(self._PATCH_LOAD, new_callable=AsyncMock) as mock_load,
            patch(self._PATCH_INACTIVE, new_callable=AsyncMock) as mock_inactive,
        ):
            mock_load.return_value = [agg]
            mock_inactive.return_value = []
            decision = await route_query(
                bq, AsyncMock(), principal=principal,
                row_security=compiled,
            )

        # With a valid rename, the aggregate IS served.
        assert decision.route_type == "aggregate", (
            f"Expected aggregate route but got {decision.route_type!r}"
        )
        assert decision.aggregate_id == str(agg.id)
        # The renamed physical column must appear in the injected predicate.
        assert "dim_region_region_code" in decision.rewritten_query
