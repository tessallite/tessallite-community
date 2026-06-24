"""Unit tests for src.security.predicate_compiler (Phase 5.1.B).

Covers:
  * DSL compilation — dimension_equals, in, and/or/not, nested composition,
    rejection of unknown functions and unquoted values.
  * Principal matching — role_predicate hits when any role overlaps;
    user_mapping hits whenever a user_identity is present.
  * user_mapping compilation — emits IN-subquery against the mapping table.
  * compile_row_security end-to-end — returns None when no rule matches,
    joins multiple matches with AND, tracks active_rule_ids +
    security_dimension_columns.
  * has_active_rules — router bypass gate.

The retired subquery-wrap pass (``wrap_with_row_security``) and the
projection-compatibility guard (``is_query_compatible``) were removed in
the F-007-01 / F-007-05 fixes; enforcement is per-scan WHERE injection in
``router._inject_security_where`` (covered by ``test_row_security_routing``).
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock

import pytest

from shared.security.predicate_compiler import (
    CompiledPredicate,
    Principal,
    RowSecurityCompileError,
    _compile_dsl_expression,
    compile_row_security,
    has_active_rules,
)


# ---------------------------------------------------------------------------
# DSL compilation
# ---------------------------------------------------------------------------


class TestDslCompilation:
    def test_dimension_equals_emits_quoted_column_and_literal(self):
        out = _compile_dsl_expression(
            "dimension_equals('region.region_code', 'NORTH')"
        )
        assert out == '"region_code" = \'NORTH\''

    def test_connector_drives_identifier_quoting(self):
        # F-007-07: the compiler quotes the column for the target connector,
        # so the simulate preview (which now passes the model's connector)
        # is byte-identical to the runtime predicate. BigQuery uses
        # backticks, not double-quotes.
        pg = _compile_dsl_expression(
            "dimension_equals('region.region_code', 'NORTH')",
            connector="postgresql",
        )
        bq = _compile_dsl_expression(
            "dimension_equals('region.region_code', 'NORTH')",
            connector="bigquery",
        )
        assert pg == '"region_code" = \'NORTH\''
        assert bq == "`region_code` = 'NORTH'"

    def test_in_with_multiple_values(self):
        out = _compile_dsl_expression(
            "in('region.region_code', 'NORTH', 'SOUTH')"
        )
        assert out == "\"region_code\" IN ('NORTH', 'SOUTH')"

    def test_and_composition(self):
        out = _compile_dsl_expression(
            "and(dimension_equals('region.region_code', 'NORTH'),"
            " dimension_equals('product.category', 'A'))"
        )
        assert out == (
            "(\"region_code\" = 'NORTH' AND \"category\" = 'A')"
        )

    def test_or_composition(self):
        out = _compile_dsl_expression(
            "or(dimension_equals('region.region_code', 'N'),"
            " dimension_equals('region.region_code', 'S'))"
        )
        assert '"region_code" = \'N\'' in out
        assert " OR " in out

    def test_not_composition(self):
        out = _compile_dsl_expression(
            "not(dimension_equals('region.region_code', 'X'))"
        )
        assert out.startswith("(NOT ")
        assert '"region_code" = \'X\'' in out

    def test_nested_composition(self):
        out = _compile_dsl_expression(
            "and("
            "  or(dimension_equals('region.region_code', 'N'),"
            "     dimension_equals('region.region_code', 'S')),"
            "  not(dimension_equals('status.name', 'disabled'))"
            ")"
        )
        assert out.count("OR") == 1
        assert out.count("NOT") == 1
        assert out.count("AND") == 1

    def test_rejects_unknown_function(self):
        with pytest.raises(RowSecurityCompileError):
            _compile_dsl_expression("rm_rf('/', '/')")

    def test_rejects_unquoted_value(self):
        with pytest.raises(RowSecurityCompileError):
            _compile_dsl_expression(
                "dimension_equals('region.region_code', NORTH)"
            )

    def test_rejects_unquoted_path(self):
        with pytest.raises(RowSecurityCompileError):
            _compile_dsl_expression("dimension_equals(region.region_code, 'X')")

    def test_rejects_invalid_path_identifier(self):
        with pytest.raises(RowSecurityCompileError):
            _compile_dsl_expression(
                "dimension_equals('region; DROP TABLE foo', 'X')"
            )

    def test_escapes_single_quote_in_literal(self):
        out = _compile_dsl_expression(
            "dimension_equals('customer.name', 'O''Brien')"
        )
        assert out == "\"name\" = 'O''Brien'"


# ---------------------------------------------------------------------------
# Principal adaptation
# ---------------------------------------------------------------------------


class TestPrincipalAdapter:
    def test_from_current_user_single_role(self):
        cu = types.SimpleNamespace(
            email="alice@example.com",
            user_id="alice@example.com",
            role="region_manager_north",
        )
        p = Principal.from_current_user(cu)
        assert p.user_identity == "alice@example.com"
        assert p.roles == frozenset({"region_manager_north"})

    def test_from_current_user_no_role(self):
        cu = types.SimpleNamespace(
            email="alice@example.com", user_id="alice@example.com", role=None
        )
        p = Principal.from_current_user(cu)
        assert p.roles == frozenset()


# ---------------------------------------------------------------------------
# compile_row_security end-to-end
# ---------------------------------------------------------------------------


def _make_rule_row_predicate(name, path, expr, roles, rule_id=None):
    return types.SimpleNamespace(
        id=rule_id or uuid.uuid4(),
        rule_type="role_predicate",
        name=name,
        dimension_path=path,
        predicate_expression=expr,
        applies_to_roles=roles,
        mapping_table_id=None,
        mapping_user_column=None,
        mapping_value_column=None,
        is_enabled=True,
    )


def _make_rule_user_mapping(name, path, table_id, user_col, value_col, rule_id=None):
    return types.SimpleNamespace(
        id=rule_id or uuid.uuid4(),
        rule_type="user_mapping",
        name=name,
        dimension_path=path,
        predicate_expression=None,
        applies_to_roles=None,
        mapping_table_id=table_id,
        mapping_user_column=user_col,
        mapping_value_column=value_col,
        is_enabled=True,
    )


def _fake_db_with_rules(rules, mapping_table=None):
    """Stand-in AsyncSession whose execute() returns the rules (or the
    mapping table for ModelTable lookups)."""
    from shared.db.models import ModelTable, RowSecurityRule

    class _Result:
        def __init__(self, items):
            self._items = list(items)

        def scalars(self):
            class _S:
                def __init__(_self, items):
                    _self._items = items

                def all(_self):
                    return _self._items

            return _S(self._items)

        def scalar_one_or_none(self):
            return self._items[0] if self._items else None

    db = AsyncMock()

    async def _execute(stmt):
        # Inspect the statement's column/FROM to decide what to return.
        text = str(stmt).lower()
        if "row_security_rules" in text:
            return _Result(rules)
        if "model_tables" in text:
            return _Result([mapping_table] if mapping_table else [])
        return _Result([])

    db.execute = _execute
    return db


@pytest.mark.asyncio
async def test_compile_returns_none_when_no_rule_matches():
    rule = _make_rule_row_predicate(
        "north",
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["region_manager_north"],
    )
    db = _fake_db_with_rules([rule])
    principal = Principal(user_identity="u@x", roles=frozenset({"viewer"}))
    out = await compile_row_security(uuid.uuid4(), principal, db)
    assert out is None


@pytest.mark.asyncio
async def test_wildcard_role_matches_any_principal():
    """Bug-883: applies_to_roles=["*"] must match every principal."""
    rule = _make_rule_row_predicate(
        "all_users",
        "payment.payment_status",
        "dimension_equals('payment.payment_status', 'SUCCESS')",
        ["*"],
    )
    db = _fake_db_with_rules([rule])
    principal = Principal(user_identity="u@x", roles=frozenset({"modeler"}))
    out = await compile_row_security(uuid.uuid4(), principal, db)
    assert out is not None
    assert '"payment_status" = \'SUCCESS\'' in out.sql_expression


@pytest.mark.asyncio
async def test_wildcard_role_matches_principal_with_no_roles():
    """Bug-883: wildcard matches even when principal has zero roles."""
    rule = _make_rule_row_predicate(
        "all_users",
        "status.flag",
        "dimension_equals('status.flag', 'ACTIVE')",
        ["*"],
    )
    db = _fake_db_with_rules([rule])
    principal = Principal(user_identity="u@x", roles=frozenset())
    out = await compile_row_security(uuid.uuid4(), principal, db)
    assert out is not None
    assert '"flag" = \'ACTIVE\'' in out.sql_expression


@pytest.mark.asyncio
async def test_compile_joins_multiple_matches_with_and():
    r1 = _make_rule_row_predicate(
        "north",
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["region_manager_north"],
    )
    r2 = _make_rule_row_predicate(
        "active",
        "status.flag",
        "dimension_equals('status.flag', 'ACTIVE')",
        ["region_manager_north"],
    )
    db = _fake_db_with_rules([r1, r2])
    principal = Principal(
        user_identity="u@x", roles=frozenset({"region_manager_north"})
    )
    out = await compile_row_security(uuid.uuid4(), principal, db)
    assert out is not None
    assert out.sql_expression.count(" AND ") >= 1
    assert set(out.active_rule_ids) == {str(r1.id), str(r2.id)}
    assert set(out.security_dimension_columns) == {"region_code", "flag"}


@pytest.mark.asyncio
async def test_compile_user_mapping_emits_in_subquery():
    table = types.SimpleNamespace(
        id=uuid.uuid4(),
        physical_name="demo_data.user_region_map",
    )
    rule = _make_rule_user_mapping(
        "per_user_region",
        "region.region_code",
        table.id,
        "user_id",
        "region_code",
    )
    db = _fake_db_with_rules([rule], mapping_table=table)
    principal = Principal(user_identity="alice@x", roles=frozenset())
    out = await compile_row_security(uuid.uuid4(), principal, db)
    assert out is not None
    assert '"region_code" IN (SELECT "region_code" FROM' in out.sql_expression
    assert '"demo_data"."user_region_map"' in out.sql_expression
    assert "'alice@x'" in out.sql_expression


# ---------------------------------------------------------------------------
# has_active_rules — router bypass gate
# ---------------------------------------------------------------------------


class TestHasActiveRules:
    def test_has_active_rules_none_returns_false(self):
        assert not has_active_rules(None)

    def test_has_active_rules_empty_returns_false(self):
        pred = CompiledPredicate(
            sql_expression="TRUE", active_rule_ids=(), security_dimension_columns=()
        )
        assert not has_active_rules(pred)

    def test_has_active_rules_truthy(self):
        pred = CompiledPredicate(
            sql_expression='"r" = \'N\'',
            active_rule_ids=("x",),
            security_dimension_columns=("r",),
        )
        assert has_active_rules(pred)
