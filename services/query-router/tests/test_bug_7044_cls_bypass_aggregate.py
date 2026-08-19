"""Bug-7044 + Fable security review gap coverage.

Three test groups:

1. **CLS + bypass_row_security + aggregate routing** (Bug-7044 / CF-008-DS-F008REV02):
   A persona with CLS restrictions and ``bypass_row_security=true`` should
   bypass RLS predicate injection but STILL be blocked from CLS-restricted
   columns.  When the query requests only unrestricted columns, it should
   route to an aggregate (bypass allows aggregate matching).

2. **RLS-safe aggregate with collision-renamed security column** (Bug-7033-F1):
   An aggregate whose grain column is collision-renamed (logical "region_code"
   stored physically as "dim_region_region_code") must inject the PHYSICAL
   column name into the security predicate.  Assert the physical column appears
   in the rewritten SQL, the route is "aggregate", and the predicate references
   the collision-resolved name.

3. **RLS-safe aggregate + CLS persona** (Fable review gap):
   When active RLS rules are present AND the persona has CLS restrictions, both
   protections must compose: CLS blocks restricted columns (403) BEFORE the
   RLS-safe aggregate path executes, and an unrestricted query under RLS is
   served from the RLS-safe aggregate with the predicate injected.

All tests assert KNOWN VALUES (route decisions, physical column names, error
codes, predicate SQL fragments) per CLAUDE.md correctness-test rules.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import src.routing.router as router_mod
from src.routing.router import route_query, _aggregate_is_rls_safe
from src.security import CompiledPredicate, Principal
from src.ir.logical_query import BoundQuery, LogicalQuery, RouteDecision

from conftest import make_aggregate, make_agg_col, make_dimension, make_measure
from test_query_flow import _bind
from test_row_security_routing import _db_returning, _role_rule

_PATCH_LOAD = "src.routing.aggregate_matcher.load_active_aggregates"

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cls_persona(
    *,
    bypass: bool,
    persona_id: uuid.UUID | None = None,
    name: str = "cls-test-persona",
):
    """Persona with column-level security restrictions and optional RLS bypass."""
    return types.SimpleNamespace(
        id=persona_id or uuid.uuid4(),
        name=name,
        bypass_row_security=bypass,
    )


def _scalar_result(items: list) -> MagicMock:
    """Mock a SQLAlchemy result with scalars().all() returning items."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = items
    return result


def _cls_db_returning(
    *,
    rls_rules: list | None = None,
    tag_ids: list | None = None,
    restricted_col_ids: list | None = None,
):
    """Build a mock db that serves both RLS and CLS query results.

    The router calls db.execute in a specific order:
    1. PersonaTagRestriction query (returns tag_ids for the persona)
    2. data_tag_columns query (returns restricted model_column_ids)
    3+. Row security rule queries (for RLS compilation)
    4+. Other queries (model_tables, etc.)

    When CLS is active (tag_ids is not empty), the first two db.execute calls
    are for CLS; subsequent calls are for RLS or other lookups.
    """
    rls_rules = rls_rules or []
    tag_ids = tag_ids or []
    restricted_col_ids = restricted_col_ids or []

    call_count = 0

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
        nonlocal call_count
        call_count += 1
        text = str(stmt).lower()

        # CLS queries (persona tag restrictions)
        if "personatagrestriction" in text or "persona_tag_restriction" in text:
            return _Result(tag_ids)
        if "data_tag_column" in text:
            return _Result(restricted_col_ids)

        # RLS queries
        if "row_security_rules" in text:
            return _Result(rls_rules)
        if "model_tables" in text:
            return _Result([])

        return _Result([])

    db.execute = _execute
    return db


# =========================================================================
# GROUP 1: CLS + bypass_row_security + aggregate routing (Bug-7044)
# =========================================================================

class TestBug7044ClsBypassAggregate:
    """Bug-7044: a persona with CLS restrictions and bypass_row_security=true
    must still enforce column-level restrictions even though RLS is bypassed.
    When the query requests only unrestricted columns, it routes to the
    aggregate (bypass enables aggregate matching)."""

    async def test_cls_blocks_restricted_column_even_with_rls_bypass(self):
        """Core Bug-7044 scenario: bypass_row_security does NOT bypass CLS.

        The persona has a CLS restriction on the 'salary' column. A query
        requesting that column must be 403'd regardless of RLS bypass.
        """
        restricted_col_id = uuid.uuid4()
        salary = make_measure("salary")
        salary.source_column_id = restricted_col_id
        region = make_dimension("region_code")
        region.source_column_id = uuid.uuid4()
        agg = make_aggregate(["region_code"], [make_agg_col(salary)])
        agg.grain_physical_cols = ["region_code"]

        sql = "SELECT region_code, SUM(salary) FROM sales GROUP BY region_code"
        bq = _bind(sql, [salary], [region])

        rule = _role_rule(
            "region.region_code",
            "dimension_equals('region.region_code', 'NORTH')",
            ["analyst"],
        )
        principal = Principal(
            user_identity="alice@x", roles=frozenset({"analyst"})
        )
        persona = _cls_persona(bypass=True)

        tag_id = uuid.uuid4()
        db = _cls_db_returning(
            rls_rules=[rule],
            tag_ids=[tag_id],
            restricted_col_ids=[restricted_col_id],
        )

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load, \
                patch("src.routing.router.audit", new_callable=AsyncMock):
            mock_load.return_value = [agg]
            with pytest.raises(HTTPException) as exc:
                await route_query(
                    bq, db, principal=principal, persona=persona,
                )

        # CLS blocks the salary column -- 403 COLUMN_RESTRICTED
        assert exc.value.status_code == 403
        # F-008-02: non-disclosing 403 — the restricted column name must NOT
        # leak in the client payload (the block itself still fires).
        assert exc.value.detail["error_code"] == "OBJECT_NOT_AVAILABLE"
        assert "columns" not in exc.value.detail
        assert "salary" not in exc.value.detail.get("message", "")
        # The aggregate loader must NOT have been called -- CLS fires
        # before aggregate matching.
        mock_load.assert_not_called()

    async def test_unrestricted_query_under_cls_bypass_routes_to_aggregate(self):
        """When a persona has CLS restrictions but the query only references
        unrestricted columns, and bypass_row_security=true, the query routes
        to the aggregate (with RLS bypassed the aggregate matcher runs
        unconditionally, rather than only for a candidate that can prove it
        carries the security predicate)."""
        restricted_col_id = uuid.uuid4()
        revenue = make_measure("revenue")
        revenue.source_column_id = uuid.uuid4()  # not restricted
        region = make_dimension("region_code")
        region.source_column_id = uuid.uuid4()  # not restricted
        agg = make_aggregate(["region_code"], [make_agg_col(revenue)])
        agg.grain_physical_cols = ["region_code"]

        sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
        bq = _bind(sql, [revenue], [region])

        rule = _role_rule(
            "region.region_code",
            "dimension_equals('region.region_code', 'NORTH')",
            ["analyst"],
        )
        principal = Principal(
            user_identity="alice@x", roles=frozenset({"analyst"})
        )
        persona = _cls_persona(bypass=True)

        tag_id = uuid.uuid4()
        db = _cls_db_returning(
            rls_rules=[rule],
            tag_ids=[tag_id],
            restricted_col_ids=[restricted_col_id],
        )

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load, \
                patch("src.routing.router.audit", new_callable=AsyncMock):
            mock_load.return_value = [agg]
            decision = await route_query(
                bq, db, principal=principal, persona=persona,
            )

        # With bypass_row_security=true and no CLS violation,
        # the query routes to the aggregate (bypass re-enables matching).
        assert decision.route_type == "aggregate"
        assert decision.aggregate_id == str(agg.id)
        # The RLS predicate must NOT be injected (bypass is active).
        assert "NORTH" not in decision.rewritten_query
        assert '"region_code"' in decision.rewritten_query
        assert '"revenue__sum"' in decision.rewritten_query
        mock_load.assert_called()

    async def test_cls_blocks_dimension_even_with_rls_bypass(self):
        """CLS restriction on a dimension (not a measure) must also block
        queries under bypass_row_security=true."""
        restricted_col_id = uuid.uuid4()
        revenue = make_measure("revenue")
        revenue.source_column_id = uuid.uuid4()
        secret_region = make_dimension("secret_region")
        secret_region.source_column_id = restricted_col_id  # restricted

        sql = "SELECT secret_region, SUM(revenue) FROM sales GROUP BY secret_region"
        bq = _bind(sql, [revenue], [secret_region])

        rule = _role_rule(
            "region.region_code",
            "dimension_equals('region.region_code', 'NORTH')",
            ["analyst"],
        )
        principal = Principal(
            user_identity="alice@x", roles=frozenset({"analyst"})
        )
        persona = _cls_persona(bypass=True)

        tag_id = uuid.uuid4()
        db = _cls_db_returning(
            rls_rules=[rule],
            tag_ids=[tag_id],
            restricted_col_ids=[restricted_col_id],
        )

        with patch("src.routing.router.audit", new_callable=AsyncMock):
            with pytest.raises(HTTPException) as exc:
                await route_query(
                    bq, db, principal=principal, persona=persona,
                )

        assert exc.value.status_code == 403
        # F-008-02: non-disclosing 403.
        assert exc.value.detail["error_code"] == "OBJECT_NOT_AVAILABLE"
        assert "columns" not in exc.value.detail
        assert "secret_region" not in exc.value.detail.get("message", "")

    async def test_no_cls_restrictions_bypass_routes_aggregate_no_predicate(self):
        """When the persona has bypass_row_security=true but no CLS
        restrictions (no persona tag restrictions exist), the query routes
        to the aggregate without any security predicate."""
        revenue = make_measure("revenue")
        revenue.source_column_id = uuid.uuid4()
        region = make_dimension("region_code")
        region.source_column_id = uuid.uuid4()
        agg = make_aggregate(["region_code"], [make_agg_col(revenue)])
        agg.grain_physical_cols = ["region_code"]

        sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
        bq = _bind(sql, [revenue], [region])

        rule = _role_rule(
            "region.region_code",
            "dimension_equals('region.region_code', 'NORTH')",
            ["analyst"],
        )
        principal = Principal(
            user_identity="alice@x", roles=frozenset({"analyst"})
        )
        persona = _cls_persona(bypass=True)

        # No CLS restrictions -- empty tag list
        db = _cls_db_returning(
            rls_rules=[rule],
            tag_ids=[],
            restricted_col_ids=[],
        )

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load, \
                patch("src.routing.router.audit", new_callable=AsyncMock):
            mock_load.return_value = [agg]
            decision = await route_query(
                bq, db, principal=principal, persona=persona,
            )

        assert decision.route_type == "aggregate"
        assert decision.aggregate_id == str(agg.id)
        assert "NORTH" not in decision.rewritten_query


# =========================================================================
# GROUP 2: RLS-safe aggregate with collision-renamed security column
#           (Bug-7033-F1 — integration-level)
# =========================================================================

class TestRlsSafeAggCollisionRenamed:
    """Bug-7033-F1: when an aggregate has a collision-renamed grain column
    (e.g. logical "region_code" stored physically as
    "dim_region_region_code"), the router must:

    * Route to the aggregate (not fall back to source)
    * Inject the PHYSICAL column name ("dim_region_region_code") in the
      security predicate, not the logical name ("region_code")
    * Never produce a 502 or a data leak

    These are integration-level tests through route_query, complementing
    the unit-level tests in test_fable_qr_agg_fixes.py.
    """

    async def test_collision_renamed_column_routes_to_aggregate_with_physical_predicate(self):
        """Full route_query integration: RLS-safe aggregate with
        collision-renamed grain column injects the PHYSICAL column name
        into the security predicate."""
        revenue = make_measure("revenue")
        region = make_dimension("region_code")
        # Aggregate with collision-renamed grain:
        # logical "region_code" -> physical "dim_region_region_code"
        agg = make_aggregate(
            ["region_code"],
            [make_agg_col(revenue)],
        )
        agg.grain_physical_cols = ["dim_region_region_code"]

        sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
        bq = _bind(sql, [revenue], [region])

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

        # Route to the RLS-safe aggregate, NOT source fallback, NOT 502
        assert decision.route_type == "aggregate"
        assert decision.aggregate_id == str(agg.id)
        # The physical column name must appear in the injected predicate
        assert "dim_region_region_code" in decision.rewritten_query
        # The predicate value must be present
        assert "'NORTH'" in decision.rewritten_query
        # "Row security active" + "RLS-safe aggregate" must appear in reason
        assert "Row security active" in decision.reason
        assert "RLS-safe aggregate" in decision.reason
        mock_load.assert_called_once()

    async def test_collision_renamed_column_falls_to_source_when_missing_from_grain(self):
        """If the security column is NOT in the aggregate grain at all,
        the collision-rename path cannot help -- fall to source with
        predicate injection."""
        revenue = make_measure("revenue")
        country = make_dimension("country")
        # Aggregate grain does NOT include "region_code"
        agg = make_aggregate(
            ["country"],
            [make_agg_col(revenue)],
        )
        agg.grain_physical_cols = ["country"]

        sql = "SELECT country, SUM(revenue) FROM sales GROUP BY country"
        bq = _bind(sql, [revenue], [country])

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

        # Falls to source because aggregate grain lacks "region_code"
        assert decision.route_type == "source"
        assert decision.aggregate_id is None
        assert "\"region_code\" = 'NORTH'" in decision.rewritten_query
        assert "Row security active" in decision.reason

    async def test_multi_grain_with_one_collision_renamed_column(self):
        """Aggregate with two grain columns where only one is collision-
        renamed. Both security columns must resolve to their physical
        names for the aggregate to be served."""
        revenue = make_measure("revenue")
        region = make_dimension("region_code")
        status = make_dimension("status_flag")
        # Two-column grain: region_code is collision-renamed,
        # status_flag keeps its logical name
        agg = make_aggregate(
            ["region_code", "status_flag"],
            [make_agg_col(revenue)],
        )
        agg.grain_physical_cols = ["dim_region_region_code", "status_flag"]

        sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
        bq = _bind(sql, [revenue], [region])

        # Security rule only on region_code
        rule = _role_rule(
            "region.region_code",
            "dimension_equals('region.region_code', 'EAST')",
            ["region_east"],
        )
        principal = Principal(
            user_identity="bob@x", roles=frozenset({"region_east"})
        )
        db = _db_returning([rule])

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
            mock_load.return_value = [agg]
            decision = await route_query(bq, db, principal=principal)

        # Aggregate serves because region_code is in the grain
        assert decision.route_type == "aggregate"
        assert decision.aggregate_id == str(agg.id)
        # Physical column name injected
        assert "dim_region_region_code" in decision.rewritten_query
        assert "'EAST'" in decision.rewritten_query


# =========================================================================
# GROUP 3: RLS-safe aggregate + CLS persona (Fable review gap)
# =========================================================================

class TestRlsSafeAggregatePlusCls:
    """Fable review gap: RLS-safe aggregate serving combined with a
    CLS / data-tag-narrowed persona.

    When both protections are active, CLS must block restricted columns
    (403) BEFORE the RLS-safe aggregate path runs. When the query
    references only unrestricted columns, the RLS-safe aggregate serves
    with the predicate injected.
    """

    async def test_cls_blocks_before_rls_safe_aggregate_serves(self):
        """Active RLS + CLS persona: a restricted column in the query
        must 403 before the aggregate path executes."""
        restricted_col_id = uuid.uuid4()
        salary = make_measure("salary")
        salary.source_column_id = restricted_col_id  # restricted by CLS
        region = make_dimension("region_code")
        region.source_column_id = uuid.uuid4()  # not restricted

        agg = make_aggregate(
            ["region_code"],
            [make_agg_col(salary)],
        )
        agg.grain_physical_cols = ["region_code"]

        sql = "SELECT region_code, SUM(salary) FROM sales GROUP BY region_code"
        bq = _bind(sql, [salary], [region])

        rule = _role_rule(
            "region.region_code",
            "dimension_equals('region.region_code', 'NORTH')",
            ["analyst"],
        )
        principal = Principal(
            user_identity="alice@x", roles=frozenset({"analyst"})
        )
        # Persona has CLS restrictions but does NOT bypass RLS
        persona = _cls_persona(bypass=False)

        tag_id = uuid.uuid4()
        db = _cls_db_returning(
            rls_rules=[rule],
            tag_ids=[tag_id],
            restricted_col_ids=[restricted_col_id],
        )

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
            mock_load.return_value = [agg]
            with pytest.raises(HTTPException) as exc:
                await route_query(
                    bq, db, principal=principal, persona=persona,
                )

        assert exc.value.status_code == 403
        # F-008-02: non-disclosing 403 — the restricted column name must NOT
        # leak in the client payload (the block itself still fires).
        assert exc.value.detail["error_code"] == "OBJECT_NOT_AVAILABLE"
        assert "columns" not in exc.value.detail
        assert "salary" not in exc.value.detail.get("message", "")
        # CLS fires before aggregate matching -- loader not called
        mock_load.assert_not_called()

    async def test_unrestricted_query_under_rls_and_cls_routes_to_rls_safe_aggregate(self):
        """Active RLS + CLS persona: when the query references only
        unrestricted columns and the aggregate grain includes the security
        column, the query routes to the RLS-safe aggregate with the
        predicate injected."""
        restricted_col_id = uuid.uuid4()
        revenue = make_measure("revenue")
        revenue.source_column_id = uuid.uuid4()  # not restricted
        region = make_dimension("region_code")
        region.source_column_id = uuid.uuid4()  # not restricted

        agg = make_aggregate(
            ["region_code"],
            [make_agg_col(revenue)],
        )
        agg.grain_physical_cols = ["region_code"]

        sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
        bq = _bind(sql, [revenue], [region])

        rule = _role_rule(
            "region.region_code",
            "dimension_equals('region.region_code', 'NORTH')",
            ["analyst"],
        )
        principal = Principal(
            user_identity="alice@x", roles=frozenset({"analyst"})
        )
        # Persona has CLS restrictions but they do NOT block revenue or region_code
        persona = _cls_persona(bypass=False)

        tag_id = uuid.uuid4()
        db = _cls_db_returning(
            rls_rules=[rule],
            tag_ids=[tag_id],
            restricted_col_ids=[restricted_col_id],
        )

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
            mock_load.return_value = [agg]
            decision = await route_query(
                bq, db, principal=principal, persona=persona,
            )

        # RLS-safe aggregate served with predicate injection
        assert decision.route_type == "aggregate"
        assert decision.aggregate_id == str(agg.id)
        # Predicate is injected (RLS active, not bypassed)
        assert "'NORTH'" in decision.rewritten_query
        assert "region_code" in decision.rewritten_query
        assert "Row security active" in decision.reason
        assert "RLS-safe aggregate" in decision.reason

    async def test_cls_blocks_with_collision_renamed_rls_safe_aggregate(self):
        """CLS restriction + collision-renamed RLS-safe aggregate: even
        though the aggregate could serve the RLS predicate with the
        physical column, CLS must block the restricted measure first."""
        restricted_col_id = uuid.uuid4()
        salary = make_measure("salary")
        salary.source_column_id = restricted_col_id  # restricted
        region = make_dimension("region_code")
        region.source_column_id = uuid.uuid4()

        agg = make_aggregate(
            ["region_code"],
            [make_agg_col(salary)],
        )
        agg.grain_physical_cols = ["dim_region_region_code"]

        sql = "SELECT region_code, SUM(salary) FROM sales GROUP BY region_code"
        bq = _bind(sql, [salary], [region])

        rule = _role_rule(
            "region.region_code",
            "dimension_equals('region.region_code', 'NORTH')",
            ["analyst"],
        )
        principal = Principal(
            user_identity="alice@x", roles=frozenset({"analyst"})
        )
        persona = _cls_persona(bypass=False)

        tag_id = uuid.uuid4()
        db = _cls_db_returning(
            rls_rules=[rule],
            tag_ids=[tag_id],
            restricted_col_ids=[restricted_col_id],
        )

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
            mock_load.return_value = [agg]
            with pytest.raises(HTTPException) as exc:
                await route_query(
                    bq, db, principal=principal, persona=persona,
                )

        assert exc.value.status_code == 403
        # F-008-02: non-disclosing 403 — the restricted column name must NOT
        # leak in the client payload (the block itself still fires).
        assert exc.value.detail["error_code"] == "OBJECT_NOT_AVAILABLE"
        assert "columns" not in exc.value.detail
        assert "salary" not in exc.value.detail.get("message", "")

    async def test_unrestricted_query_with_collision_renamed_rls_safe_aggregate(self):
        """CLS persona (with restrictions that do NOT block the queried
        columns) + collision-renamed aggregate: RLS predicate is injected
        with the physical column name, aggregate is served."""
        restricted_col_id = uuid.uuid4()
        revenue = make_measure("revenue")
        revenue.source_column_id = uuid.uuid4()  # not restricted
        region = make_dimension("region_code")
        region.source_column_id = uuid.uuid4()  # not restricted

        agg = make_aggregate(
            ["region_code"],
            [make_agg_col(revenue)],
        )
        agg.grain_physical_cols = ["dim_region_region_code"]

        sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
        bq = _bind(sql, [revenue], [region])

        rule = _role_rule(
            "region.region_code",
            "dimension_equals('region.region_code', 'NORTH')",
            ["analyst"],
        )
        principal = Principal(
            user_identity="alice@x", roles=frozenset({"analyst"})
        )
        persona = _cls_persona(bypass=False)

        tag_id = uuid.uuid4()
        db = _cls_db_returning(
            rls_rules=[rule],
            tag_ids=[tag_id],
            restricted_col_ids=[restricted_col_id],
        )

        with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
            mock_load.return_value = [agg]
            decision = await route_query(
                bq, db, principal=principal, persona=persona,
            )

        # RLS-safe aggregate served with physical column predicate
        assert decision.route_type == "aggregate"
        assert decision.aggregate_id == str(agg.id)
        # Physical column name in the predicate
        assert "dim_region_region_code" in decision.rewritten_query
        assert "'NORTH'" in decision.rewritten_query
        assert "RLS-safe aggregate" in decision.reason
