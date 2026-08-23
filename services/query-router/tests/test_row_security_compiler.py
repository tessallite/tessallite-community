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

from shared.auth.middleware import CurrentServiceUser
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

    def test_f007_01_dimension_in_is_rejected(self):
        """F-007-01: ``dimension_in`` is not a supported DSL function.
        Seed predicates must use ``in(...)``. Guard: compiler still rejects
        the invalid token so a drifted live tenant cannot silently compile.
        """
        with pytest.raises(RowSecurityCompileError, match="unknown row-security function"):
            _compile_dsl_expression(
                "dimension_in('region.region_code', 'FR', 'EMEA')"
            )

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
async def test_compile_fails_closed_when_role_rule_exists_but_none_matches():
    """F-007-01: a model that defines a role_predicate rule governs an audience.
    A principal who matches NO role_predicate rule (typo'd/renamed IdP role, or a
    genuine non-member) must be DENIED every row — a deny-all predicate — not
    treated as unrestricted (the old return-None no-op left them reading all
    rows). ``has_active_rules`` reports the policy active so the router injects
    the deny."""
    rule = _make_rule_row_predicate(
        "north",
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["region_manager_north"],
    )
    db = _fake_db_with_rules([rule])
    principal = Principal(user_identity="u@x", roles=frozenset({"viewer"}))
    out = await compile_row_security(uuid.uuid4(), principal, db)
    assert out is not None
    # Deny-all predicate admits no row.
    assert out.sql_expression == "0 = 1"
    assert has_active_rules(out) is True
    # Empty security columns -> aggregate/pocket are never RLS-safe (source only).
    assert out.security_dimension_columns == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["tenant_admin", "modeler", "system_admin"])
async def test_privileged_role_is_exempt_from_unmatched_audience_denial(role):
    """Bug-8447 Option B exempts only the unmatched-audience coverage gate."""
    rule = _make_rule_row_predicate(
        "north", "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')", ["member"],
    )
    out = await compile_row_security(
        uuid.uuid4(),
        Principal(user_identity="admin@x", roles=frozenset({role})),
        _fake_db_with_rules([rule]),
    )
    assert out is None


@pytest.mark.asyncio
async def test_privileged_role_still_obeys_rule_that_explicitly_targets_it():
    rule = _make_rule_row_predicate(
        "admin-north", "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')", ["tenant_admin"],
    )
    out = await compile_row_security(
        uuid.uuid4(),
        Principal(user_identity="admin@x", roles=frozenset({"tenant_admin"})),
        _fake_db_with_rules([rule]),
    )
    assert out is not None
    assert out.sql_expression != "0 = 1"
    assert "'NORTH'" in out.sql_expression


@pytest.mark.asyncio
async def test_kpi_service_principal_remains_fail_closed_when_unmatched():
    rule = _make_rule_row_predicate(
        "members", "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')", ["member"],
    )
    out = await compile_row_security(
        uuid.uuid4(),
        Principal(
            user_identity="service:kpi-evaluator",
            roles=frozenset({"kpi_evaluator"}),
        ),
        _fake_db_with_rules([rule]),
    )
    assert out is not None
    assert out.sql_expression == "0 = 1"


@pytest.mark.asyncio
async def test_internal_kpi_snapshot_coverage_exemption_preserves_rls_rules():
    """Bug-9257: the signed KPI snapshot hop may compute the global value,
    while a normal kpi_evaluator principal remains deny-all and wildcard rules
    still apply to the explicitly opted-in operation."""
    unmatched_rule = _make_rule_row_predicate(
        "members", "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')", ["member"],
    )
    out = await compile_row_security(
        uuid.uuid4(),
        Principal(
            user_identity="service:kpi-snapshot-sweep",
            roles=frozenset({"kpi_evaluator"}),
            unmatched_role_coverage_exempt=True,
        ),
        _fake_db_with_rules([unmatched_rule]),
    )
    assert out is None

    wildcard = _make_rule_row_predicate(
        "everyone", "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')", ["*"],
    )
    out = await compile_row_security(
        uuid.uuid4(),
        Principal(
            user_identity="service:kpi-snapshot-sweep",
            roles=frozenset({"kpi_evaluator"}),
            unmatched_role_coverage_exempt=True,
        ),
        _fake_db_with_rules([wildcard]),
    )
    assert out is not None
    assert "NORTH" in out.sql_expression


@pytest.mark.asyncio
async def test_privileged_service_principal_is_exempt_for_full_data_operations():
    """Privileged internal operations need the same role-based exemption.

    Pocket materialisation, data-quality introspection, and aggregate rebuilds
    intentionally mint service tokens with a privileged platform role. The
    service adapter must preserve that role so unmatched audience rules do not
    accidentally narrow a full-data maintenance operation.
    """
    rule = _make_rule_row_predicate(
        "members", "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')", ["member"],
    )
    service_user = CurrentServiceUser(
        principal="pocket-refresh",
        tenant_id="tenant-1",
        role="system_admin",
        scopes=["pocket:refresh"],
    )

    out = await compile_row_security(
        uuid.uuid4(),
        Principal.from_current_user(service_user),
        _fake_db_with_rules([rule]),
    )

    assert out is None


@pytest.mark.asyncio
async def test_subjectless_embed_principal_remains_fail_closed_when_unmatched():
    rule = _make_rule_row_predicate(
        "members", "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')", ["member"],
    )
    out = await compile_row_security(
        uuid.uuid4(),
        Principal(user_identity="embed-user", roles=frozenset()),
        _fake_db_with_rules([rule]),
    )
    assert out is not None
    assert out.sql_expression == "0 = 1"


@pytest.mark.asyncio
async def test_compile_returns_none_when_model_has_no_role_rules():
    """A model with only a user_mapping rule (or no role_predicate rules) is not
    a role-governed audience, so a principal outside it is genuinely
    unrestricted — compile returns None (no injection)."""
    rule = _make_rule_user_mapping(
        "map", "region.region_code", uuid.uuid4(), "user_email", "region_code",
    )
    # Principal with no user_identity -> user_mapping does not apply, and there
    # is no role_predicate rule, so the result is a no-op.
    db = _fake_db_with_rules([rule])
    principal = Principal(user_identity="", roles=frozenset({"viewer"}))
    out = await compile_row_security(uuid.uuid4(), principal, db)
    assert out is None


@pytest.mark.asyncio
async def test_compile_multi_role_grants_are_ored():
    """F-007-03: a principal holding two roles, each with its own grant rule, is
    entitled to the UNION of those grants — the fragments OR together, not AND
    (the old AND made a France+Germany manager see the empty intersection)."""
    r_fr = _make_rule_row_predicate(
        "france",
        "region.region_code",
        "dimension_equals('region.region_code', 'FR')",
        ["mgr_fr"],
    )
    r_de = _make_rule_row_predicate(
        "germany",
        "region.region_code",
        "dimension_equals('region.region_code', 'DE')",
        ["mgr_de"],
    )
    db = _fake_db_with_rules([r_fr, r_de])
    principal = Principal(
        user_identity="u@x", roles=frozenset({"mgr_fr", "mgr_de"}),
    )
    out = await compile_row_security(uuid.uuid4(), principal, db)
    assert out is not None
    # UNION of the two grants: OR, never AND (AND would be the empty set).
    assert " OR " in out.sql_expression
    assert " AND " not in out.sql_expression
    assert "'FR'" in out.sql_expression and "'DE'" in out.sql_expression
    assert set(out.active_rule_ids) == {str(r_fr.id), str(r_de.id)}


@pytest.mark.asyncio
async def test_compile_wildcard_baseline_ands_with_named_grant():
    """Fable B2-1: a wildcard ('*') rule is a UNIVERSAL restriction every
    principal must satisfy. It must AND as a mandatory baseline with the named
    role grants, NEVER OR with them — OR'ing let a named-role caller bypass the
    universal floor (e.g. read inactive rows past a wildcard active='true')."""
    wildcard = _make_rule_row_predicate(
        "active-floor",
        "status.active",
        "dimension_equals('status.active', 'true')",
        ["*"],
    )
    named = _make_rule_row_predicate(
        "germany",
        "region.region_code",
        "dimension_equals('region.region_code', 'DE')",
        ["mgr_de"],
    )
    db = _fake_db_with_rules([wildcard, named])
    principal = Principal(user_identity="u@x", roles=frozenset({"mgr_de"}))
    out = await compile_row_security(uuid.uuid4(), principal, db)
    assert out is not None
    # The wildcard floor ANDs with the named grant — the caller must satisfy
    # BOTH active='true' AND region='DE'. It must NOT be OR'd.
    assert '"active" = \'true\'' in out.sql_expression
    assert '"region_code" = \'DE\'' in out.sql_expression
    assert " AND " in out.sql_expression
    # The wildcard fragment is not inside an OR alternative with the grant.
    assert "'true' OR" not in out.sql_expression
    assert "OR \"active\"" not in out.sql_expression


@pytest.mark.asyncio
async def test_compile_wildcard_baseline_ands_with_multirole_grants():
    """Fable B2-1: wildcard baseline ANDs OUTSIDE the multi-role OR — the
    effective predicate is wildcard AND (grant_fr OR grant_de)."""
    wildcard = _make_rule_row_predicate(
        "active-floor", "status.active",
        "dimension_equals('status.active', 'true')", ["*"],
    )
    r_fr = _make_rule_row_predicate(
        "france", "region.region_code",
        "dimension_equals('region.region_code', 'FR')", ["mgr_fr"],
    )
    r_de = _make_rule_row_predicate(
        "germany", "region.region_code",
        "dimension_equals('region.region_code', 'DE')", ["mgr_de"],
    )
    db = _fake_db_with_rules([wildcard, r_fr, r_de])
    principal = Principal(
        user_identity="u@x", roles=frozenset({"mgr_fr", "mgr_de"}),
    )
    out = await compile_row_security(uuid.uuid4(), principal, db)
    assert out is not None
    # Union of FR/DE grants is OR'd, but ANDed with the universal active floor.
    assert " OR " in out.sql_expression
    assert '"active" = \'true\'' in out.sql_expression
    assert " AND " in out.sql_expression


@pytest.mark.asyncio
async def test_compile_denies_when_role_audience_unmatched_but_user_mapping_matches():
    """Fable B2-2: a model with BOTH a role_predicate rule and a user_mapping
    rule is role-governed. A principal who matches the user_mapping but NO role
    rule must be DENIED every row of the role audience — the user_mapping match
    must NOT satisfy role-audience coverage (which previously left the role
    dimension unrestricted for that caller)."""
    role_rule = _make_rule_row_predicate(
        "france", "region.region_code",
        "dimension_equals('region.region_code', 'FR')", ["mgr_fr"],
    )
    mapping_rule = _make_rule_user_mapping(
        "dept-map", "dept.code", uuid.uuid4(), "user_email", "dept_code",
    )
    db = _fake_db_with_rules(
        [role_rule, mapping_rule],
        mapping_table=types.SimpleNamespace(
            id=mapping_rule.mapping_table_id, physical_name="dept_map",
            source_id="src-1",
        ),
    )
    # Principal has a valid identity (matches user_mapping) but role 'viewer'
    # (does NOT match the role rule).
    principal = Principal(user_identity="bob@x", roles=frozenset({"viewer"}))
    out = await compile_row_security(uuid.uuid4(), principal, db)
    assert out is not None
    # Fail closed: deny-all, NOT the user_mapping predicate alone (which would
    # leave the region audience unrestricted).
    assert out.sql_expression == "0 = 1"
    assert has_active_rules(out) is True


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
# Bug-5559: SQL injection via user_identity literal (user_mapping)
# ---------------------------------------------------------------------------


async def _compile_user_mapping_with_identity(user_identity: str, connector: str = "postgresql") -> str:
    """Helper: compile a user_mapping rule and return the SQL expression."""
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
    principal = Principal(user_identity=user_identity, roles=frozenset())
    out = await compile_row_security(uuid.uuid4(), principal, db, connector=connector)
    assert out is not None
    return out.sql_expression


@pytest.mark.asyncio
async def test_user_mapping_single_quote_injection():
    """Bug-5559: single-quote injection attempt must be escaped, not break out."""
    sql = await _compile_user_mapping_with_identity("'; DROP TABLE users; --")
    # The injected payload must appear entirely inside the string literal,
    # never as executable SQL. The literal should contain the escaped quote.
    assert "DROP TABLE" in sql  # the text is there, but as a string value
    # The SQL must have exactly two top-level single-quote delimiters around
    # the user literal (open + close), with the embedded quote doubled.
    assert "'''; DROP TABLE users; --'" in sql or "''''" in sql


@pytest.mark.asyncio
async def test_user_mapping_backslash_escape_injection_postgresql():
    """Bug-5559: backslash-quote sequence must not break out on PostgreSQL."""
    sql = await _compile_user_mapping_with_identity("\\'; DROP TABLE users; --", "postgresql")
    # In PostgreSQL (standard_conforming_strings=on), backslash is literal.
    # The entire payload must remain inside the string literal.
    # Count unescaped SQL statement terminators outside string context:
    # the WHERE clause should have exactly one = sign for the user_col comparison.
    assert sql.count("WHERE") == 1
    assert "DROP TABLE" in sql  # present as data, not as executable SQL


@pytest.mark.asyncio
async def test_user_mapping_backslash_escape_injection_bigquery():
    """Bug-5559: backslash-quote on BigQuery uses backslash escaping."""
    sql = await _compile_user_mapping_with_identity("\\'; DROP TABLE users; --", "bigquery")
    # BigQuery escapes single quotes with backslash inside string literals.
    # The backslash in the input must also be escaped.
    assert sql.count("WHERE") == 1
    assert "DROP TABLE" in sql  # present as data, not as executable SQL


@pytest.mark.asyncio
async def test_user_mapping_nested_double_quote_injection():
    """Bug-5559: nested doubled quotes must not break literal boundaries."""
    sql = await _compile_user_mapping_with_identity("a]''b]'; DROP TABLE x; --")
    assert sql.count("WHERE") == 1
    assert "DROP TABLE" in sql  # text is inside the literal


@pytest.mark.asyncio
async def test_user_mapping_unicode_escape_injection():
    """Bug-5559: Unicode apostrophe (U+0027) in identity must be escaped."""
    # U+0027 is the standard ASCII single quote
    payload = "admin'; DROP TABLE x"
    sql = await _compile_user_mapping_with_identity(payload)
    assert sql.count("WHERE") == 1
    assert "DROP TABLE" in sql  # present as data


@pytest.mark.asyncio
async def test_user_mapping_normal_email_unchanged():
    """Bug-5559: normal user_identity values produce identical SQL to before."""
    sql = await _compile_user_mapping_with_identity("alice@example.com")
    assert "'alice@example.com'" in sql


@pytest.mark.asyncio
async def test_user_mapping_email_with_apostrophe():
    """Bug-5559: legitimate O'Brien-style names are properly escaped."""
    sql = await _compile_user_mapping_with_identity("o'brien@example.com", "postgresql")
    assert "'o''brien@example.com'" in sql


@pytest.mark.asyncio
async def test_user_mapping_sqlserver_connector():
    """Bug-5559: SQL Server (tsql) dialect produces correct escaping."""
    sql = await _compile_user_mapping_with_identity("alice@x", "sqlserver")
    assert "'alice@x'" in sql
    # SQL Server uses bracket quoting for identifiers
    assert "[region_code]" in sql


@pytest.mark.asyncio
async def test_user_mapping_bigquery_normal():
    """Bug-5559: BigQuery dialect produces correct literal and backtick identifiers."""
    sql = await _compile_user_mapping_with_identity("alice@x", "bigquery")
    assert "'alice@x'" in sql
    assert "`region_code`" in sql


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


# ---------------------------------------------------------------------------
# Bug-7039: mapping_source_ids tracking for cross-source validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compile_user_mapping_tracks_mapping_source_id():
    """Bug-7039: the compiled predicate must expose the source_id of each
    user_mapping rule's mapping table so the router can validate
    cross-source compatibility at routing time."""
    src_id = uuid.uuid4()
    table = types.SimpleNamespace(
        id=uuid.uuid4(),
        physical_name="demo_data.user_region_map",
        source_id=src_id,
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
    assert out.mapping_source_ids == (str(src_id),)


@pytest.mark.asyncio
async def test_role_predicate_has_no_mapping_source_ids():
    """Role-predicate rules do not reference mapping tables, so
    mapping_source_ids should be empty."""
    rule = _make_rule_row_predicate(
        "north",
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["region_manager_north"],
    )
    db = _fake_db_with_rules([rule])
    principal = Principal(
        user_identity="alice@x", roles=frozenset({"region_manager_north"})
    )
    out = await compile_row_security(uuid.uuid4(), principal, db)
    assert out is not None
    assert out.mapping_source_ids == ()
