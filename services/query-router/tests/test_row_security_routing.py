"""Router-level integration tests for row security.

Invariants under test:
  * A principal with a matching active rule forces ``route_type="source"``
    with the security predicate injected per-scan via
    ``_inject_security_where`` — the predicate is AND'd into the WHERE
    clause of every SELECT that reads a physical table, not wrapped in an
    outer subquery — and the aggregate matcher is **not** invoked.
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
    return db


# ---------------------------------------------------------------------------
# Core invariant: active rule forces source + injects predicate + bypasses aggregate
# ---------------------------------------------------------------------------


async def test_principal_with_matching_rule_forces_source_and_injects():
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

    # Bug-915: security predicate is injected into the inner WHERE (before LIMIT)
    # rather than wrapped in a subquery. __ts_sec alias is not used.
    assert decision.route_type == "source"
    assert decision.aggregate_id is None
    assert decision.pocket_id is None
    assert "\"region_code\" = 'NORTH'" in decision.rewritten_query
    assert "Row security active" in decision.reason
    # Bypass invariant: aggregate loader must not be called when rules are active.
    mock_load.assert_not_called()


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


async def test_principal_without_matching_rule_takes_normal_aggregate_path():
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
    # Principal has a different role — no rule should match.
    principal = Principal(user_identity="bob@x", roles=frozenset({"viewer"}))
    db = _db_returning([rule])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        decision = await route_query(bq, db, principal=principal)

    assert decision.route_type == "aggregate"
    assert decision.aggregate_id == str(agg.id)
    assert "__ts_sec" not in decision.rewritten_query


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

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        decision = await route_query(bq, db, principal=principal)

    assert decision.route_type == "source"
    assert " AND " in decision.rewritten_query
    assert "\"region_code\" = 'NORTH'" in decision.rewritten_query
    assert "\"flag\" = 'ACTIVE'" in decision.rewritten_query
    mock_load.assert_not_called()


async def test_user_mapping_rule_emits_in_subquery_in_wrap():
    m = make_measure("revenue")
    d = make_dimension("region_code")

    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])

    table = types.SimpleNamespace(
        id=uuid.uuid4(), physical_name="demo_data.user_region_map"
    )
    rule = _user_mapping_rule(
        "region.region_code", table.id, "user_id", "region_code"
    )
    principal = Principal(user_identity="alice@x", roles=frozenset())
    db = _db_returning([rule], mapping_table=table)

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        decision = await route_query(bq, db, principal=principal)

    assert decision.route_type == "source"
    assert '"region_code" IN (SELECT "region_code" FROM' in decision.rewritten_query
    assert '"demo_data"."user_region_map"' in decision.rewritten_query
    assert "'alice@x'" in decision.rewritten_query
    mock_load.assert_not_called()


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
        decision = await route_query(bq, db, principal=principal)

    assert decision.route_type == "source"
    assert "\"region_code\" = 'NORTH'" in decision.rewritten_query
    mock_load.assert_not_called()


async def test_idp_group_rule_fires_for_principal_derived_from_current_user():
    # F-007-12: an idp_group-sourced row-security rule must fire for a principal
    # whose groups are derived from the current user (the SSO group path), and
    # inject the predicate into the source query.
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
        decision = await route_query(bq, db, principal=principal)

    assert decision.route_type == "source"
    assert "\"region_code\" = 'NORTH'" in decision.rewritten_query
    mock_load.assert_not_called()


async def test_saml_claim_rule_does_not_fire_without_claim():
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

    assert decision.route_type == "aggregate"


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
        decision = await route_query(bq, db, principal=principal)

    assert decision.route_type == "source"
    assert "\"region_code\" = 'NORTH'" in decision.rewritten_query
    mock_load.assert_not_called()


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
