"""Bug-9373 [RLS leak] — two joined relations that BOTH own the security
column must BOTH be constrained.

``_qualify_for_select`` resolved each security column to the FIRST owner
candidate scanned in the SELECT. For ``FROM sales s JOIN employees e`` where
the model proves both tables carry ``region_code``, only ``s`` was qualified;
the ``e`` scan was left unfiltered, so a user selecting ``e.region_code`` saw
every region.

This is not the Bug-7034 fan-out (which bound the predicate to EVERY scan,
including relations that do not carry the column). The predicate is bound
only to the relations the model names as owners; a non-owner relation in
the same query still receives nothing.

Test escape: the owner tests covered a self-join of ONE owner (Bug-8896) and
the deterministic ranking of two owners (Bug-9264) but asserted a single
conjunct for the latter, pinning the defect. Guard: this file plus the
updated Bug-9264 test. Tier: T3 (row-level security).
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp

from src.routing.router import _inject_security_where
from src.security import CompiledPredicate


def _pred(owners):
    return CompiledPredicate(
        sql_expression="\"region_code\" = 'NORTH'",
        active_rule_ids=("r1",),
        security_dimension_columns=("region_code",),
        security_column_owners=owners,
    )


def _region_bindings(out_sql: str) -> list[str]:
    """Table qualifier of every injected ``region_code = 'NORTH'`` comparison."""
    ast = sqlglot.parse_one(out_sql, read="postgres")
    return sorted(
        eq.this.table
        for eq in ast.find_all(exp.EQ)
        if isinstance(eq.this, exp.Column)
        and eq.this.name == "region_code"
        and "NORTH" in eq.sql(dialect="postgres")
    )


def test_bug_9373_two_owning_tables_both_scans_carry_the_predicate():
    sql = (
        "SELECT e.region_code, COUNT(*) AS cnt "
        "FROM demo.sales AS s JOIN demo.employees AS e ON s.emp_id = e.id "
        "GROUP BY e.region_code"
    )
    out = _inject_security_where(
        sql, _pred((("region_code", "sales"), ("region_code", "employees"))),
    )
    assert _region_bindings(out) == ["e", "s"], (
        "both owning scans must be constrained (one copy each); got: " + out
    )


def test_bug_9373_non_owner_in_the_same_join_receives_nothing():
    """Owners name sales and employees; dim_product does NOT carry the column
    and must not be qualified (the Bug-7034 guard holds alongside the fix)."""
    sql = (
        "SELECT e.region_code, d.category, SUM(s.amount) "
        "FROM demo.sales AS s "
        "JOIN demo.employees AS e ON s.emp_id = e.id "
        "JOIN demo.dim_product AS d ON s.product_id = d.id "
        "GROUP BY e.region_code, d.category"
    )
    out = _inject_security_where(
        sql, _pred((("region_code", "sales"), ("region_code", "employees"))),
    )
    assert _region_bindings(out) == ["e", "s"], out


def test_bug_9373_only_the_scanned_owner_is_constrained():
    """Two owners in the model, one scanned: exactly one conjunct, bound to
    the scanned owner -- absent owners are never injected."""
    sql = "SELECT s.region_code, SUM(s.amount) FROM demo.sales AS s GROUP BY s.region_code"
    out = _inject_security_where(
        sql, _pred((("region_code", "sales"), ("region_code", "employees"))),
    )
    assert _region_bindings(out) == ["s"], out


def test_bug_9373_second_owner_self_joined_gets_every_alias():
    """Owner ranking plus self-join: sales once, employees twice -> three
    constrained scans, each qualified to its own alias."""
    sql = (
        "SELECT e1.region_code FROM demo.sales AS s "
        "JOIN demo.employees AS e1 ON s.emp_id = e1.id "
        "JOIN demo.employees AS e2 ON e1.mgr_id = e2.id"
    )
    out = _inject_security_where(
        sql, _pred((("region_code", "sales"), ("region_code", "employees"))),
    )
    assert _region_bindings(out) == ["e1", "e2", "s"], out


def test_bug_9373_two_security_columns_each_bound_to_their_own_owner():
    """A multi-column predicate binds each column to its owner set; with one
    owner per column the result is a single fully-qualified conjunct."""
    pred = CompiledPredicate(
        sql_expression="\"region_code\" = 'NORTH' AND \"dept\" = 'OPS'",
        active_rule_ids=("r1",),
        security_dimension_columns=("region_code", "dept"),
        security_column_owners=(("region_code", "sales"), ("dept", "employees")),
    )
    sql = (
        "SELECT s.amount FROM demo.sales AS s "
        "JOIN demo.employees AS e ON s.emp_id = e.id"
    )
    out = _inject_security_where(sql, pred)
    ast = sqlglot.parse_one(out, read="postgres")
    bindings = {
        (col.name, col.table)
        for col in ast.args["where"].find_all(exp.Column)
    }
    assert bindings == {("region_code", "s"), ("dept", "e")}, out
