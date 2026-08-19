"""Bug-7034 guard: the RLS predicate must never fan out across every scan.

Bug-7034 reports a REAL defect: when two joined relations both expose the
security dimension column, the injected bare predicate ``region_code = 'NORTH'``
is ambiguous and the database rejects the query.

The proposed fix (integration source range ``9ad644b3..review/frozen-
workstream-2-2026-08-10``) made ``_inject_security_where`` collect one injection
target per PHYSICAL SCAN and alias-qualify the predicate with each scan's alias.
That fix was REJECTED during integration because it is a strictly worse defect:

``sqlglot``'s ``scope.tables`` enumerates EVERY relation in the query, and
nothing in ``CompiledPredicate`` records which relation owns the security
column. So for the ordinary star-schema shape the fan-out emits

    WHERE f."region_code" = 'NORTH' AND d."region_code" = 'NORTH'

against ``FROM sales f JOIN dim_product d``. ``dim_product`` has no
``region_code`` column, so the query fails at the source -- row-level security
becomes unusable on EVERY joined model, not just the ambiguous ones. Where the
second relation does carry the column, the extra conjunct silently over-filters
and under-reports.

These tests pin the invariant the rejected fix violated. They are expected to
FAIL against the per-scan fan-out implementation and to PASS against the
current one. A correct Bug-7034 fix must PROVE which relation owns the security
column (the model knows this; ``_inject_security_where`` does not) and qualify
only that relation -- at which point these tests must be updated to assert
single-relation qualification rather than removed.
"""
from __future__ import annotations

import uuid

import pytest
import sqlglot
from sqlglot import exp

from src.routing.router import _inject_security_where
from src.security import CompiledPredicate


_PRED = CompiledPredicate(
    sql_expression="\"region_code\" = 'NORTH'",
    active_rule_ids=("r1",),
    security_dimension_columns=("region_code",),
)


def _security_conjunct_count(out_sql: str) -> int:
    """How many times the injected security comparison appears in the output."""
    ast = sqlglot.parse_one(out_sql, read="postgres")
    return sum(
        1
        for eq in ast.find_all(exp.EQ)
        if isinstance(eq.this, exp.Column)
        and eq.this.name == "region_code"
        and "NORTH" in eq.sql(dialect="postgres")
    )


def test_star_schema_join_gets_exactly_one_security_conjunct():
    """The canonical shape: a fact joined to a dimension that does NOT carry
    the security column.

    One conjunct is correct. Two means the predicate was fanned out onto
    ``dim_product``, which has no ``region_code`` -- the source rejects the
    query and row-security is unusable on every joined model.
    """
    sql = (
        "SELECT d.category, SUM(f.amount) "
        "FROM demo.sales AS f "
        "JOIN demo.dim_product AS d ON f.product_id = d.id "
        "GROUP BY d.category"
    )
    out = _inject_security_where(sql, _PRED)

    assert _security_conjunct_count(out) == 1, (
        "the security predicate was injected once per physical scan. "
        "dim_product has no region_code column, so this SQL cannot execute "
        f"and row-security is broken for every joined model. Got: {out}"
    )


def test_three_relation_join_gets_exactly_one_security_conjunct():
    """Fan-out scales with the join width -- a snowflake would emit one bogus
    conjunct per dimension. Pin the count at one regardless of join width."""
    sql = (
        "SELECT d.category, c.name, SUM(f.amount) "
        "FROM demo.sales AS f "
        "JOIN demo.dim_product AS d ON f.product_id = d.id "
        "JOIN demo.dim_customer AS c ON f.cust_id = c.id "
        "GROUP BY d.category, c.name"
    )
    out = _inject_security_where(sql, _PRED)

    assert _security_conjunct_count(out) == 1, (
        f"expected exactly one security conjunct regardless of join width, "
        f"got {_security_conjunct_count(out)} in: {out}"
    )


def test_no_security_conjunct_references_a_relation_that_cannot_carry_it():
    """The safety property behind the count: the injected predicate must never
    name a relation alias that the compiler never claimed owns the column.

    ``CompiledPredicate`` carries column NAMES only, with no relation binding,
    so any alias qualification invented inside ``_inject_security_where`` is a
    guess. Assert that no such guess is made against the dimension aliases.
    """
    sql = (
        "SELECT d.category, SUM(f.amount) "
        "FROM demo.sales AS f "
        "JOIN demo.dim_product AS d ON f.product_id = d.id "
        "GROUP BY d.category"
    )
    out = _inject_security_where(sql, _PRED)
    ast = sqlglot.parse_one(out, read="postgres")

    offending = [
        col.sql(dialect="postgres")
        for col in ast.find_all(exp.Column)
        if col.name == "region_code" and col.table == "d"
    ]
    assert not offending, (
        "the injected predicate qualified the security column with the "
        "dimension alias 'd'. Nothing proved dim_product carries "
        f"region_code. Offending nodes: {offending}"
    )


def test_single_relation_query_is_unaffected():
    """The single-scan case has no ambiguity and must keep working exactly as
    before -- one conjunct, predicate present."""
    out = _inject_security_where("SELECT region_code FROM demo.sales", _PRED)
    assert _security_conjunct_count(out) == 1
    assert "'NORTH'" in out


# ---------------------------------------------------------------------------
# Bug-8896 (formerly Bug-7034) — the underlying defect is STILL OPEN
# ---------------------------------------------------------------------------


def test_bug_8896_self_join_constrains_every_scan_of_the_owner():
    """F-007-05 / Bug-8896 / Bug-9264: a self-join of the owner relation must
    constrain EVERY scan, not just the first. The prior fix qualified only the
    first alias (``e1``), leaving the ``e2`` scan row-UNSECURED — a data leak.
    The predicate must now be AND-combined once per alias (``e1`` AND ``e2``),
    each fully qualified, and still never fan onto a relation that does not own
    the column (Bug-7034 guard stays green). This asserts the both-scans outcome
    and FAILS against the pre-fix first-alias-only code (which emitted 1 conjunct
    bound to ``e1`` only).
    """
    pred = CompiledPredicate(
        sql_expression="\"region_code\" = 'NORTH'",
        active_rule_ids=("r1",),
        security_dimension_columns=("region_code",),
        security_column_owners=(("region_code", "employees"),),
    )
    sql = (
        "SELECT e1.region_code, e2.region_code AS peer_region "
        "FROM employees AS e1 JOIN employees AS e2 ON e1.mgr_id = e2.id"
    )
    out = _inject_security_where(sql, pred)
    ast = sqlglot.parse_one(out, read="postgres")

    injected = [
        eq for eq in ast.find_all(exp.EQ)
        if isinstance(eq.this, exp.Column)
        and eq.this.name == "region_code"
        and "NORTH" in eq.sql(dialect="postgres")
    ]
    assert injected, "the security predicate was not injected at all"
    assert all(col.this.table != "" for col in injected), (
        "the injected security column is unqualified while two scans in scope "
        "both expose it — the database cannot disambiguate it (Bug-8896)"
    )
    # Both owner scans must be constrained — one conjunct qualified to each of
    # the two self-join aliases. The pre-fix code produced exactly one (``e1``).
    qualifying_aliases = {col.this.table for col in injected}
    assert qualifying_aliases == {"e1", "e2"}, (
        "every scan of the owner relation must be constrained; got "
        f"{sorted(qualifying_aliases)} (e2 left row-unsecured is the Bug-8896 leak)"
    )
    assert _security_conjunct_count(out) == 2


def test_f007_05_star_join_with_owners_qualifies_fact_not_dimension():
    """F-007-05: owners pointing at the fact table must qualify ``f``, never
    ``d``, and still emit exactly one conjunct.
    """
    pred = CompiledPredicate(
        sql_expression="\"region_code\" = 'NORTH'",
        active_rule_ids=("r1",),
        security_dimension_columns=("region_code",),
        security_column_owners=(("region_code", "sales"),),
    )
    sql = (
        "SELECT d.category, SUM(f.amount) "
        "FROM demo.sales AS f "
        "JOIN demo.dim_product AS d ON f.product_id = d.id "
        "GROUP BY d.category"
    )
    out = _inject_security_where(sql, pred)
    assert _security_conjunct_count(out) == 1
    ast = sqlglot.parse_one(out, read="postgres")
    offending = [
        col.sql(dialect="postgres")
        for col in ast.find_all(exp.Column)
        if col.name == "region_code" and col.table == "d"
    ]
    assert not offending, (
        "owners named sales, but the predicate was qualified with "
        f"dimension alias d: {offending}"
    )
    fact_qualified = [
        col for col in ast.find_all(exp.Column)
        if col.name == "region_code" and col.table == "f"
    ]
    assert fact_qualified, f"expected f.region_code qualification, got: {out}"


@pytest.mark.asyncio
async def test_multi_owner_qualification_is_deterministic():
    """Bug-9264: two tables both carrying the security column must qualify
    the path/fact owner, not whichever row the unordered lookup returned
    first. Still exactly one conjunct (Bug-7034 must stay green).
    """
    from unittest.mock import AsyncMock

    from shared.security.predicate_compiler import _load_security_column_owners

    dim_id_late = uuid.uuid4()
    dim_id_early = uuid.uuid4()
    # Unordered fetch: dimension table first, fact second. Ranking must
    # still put the path owner (Dimension.name ``region`` on fact ``sales``)
    # ahead of the other table that also carries the column.
    rows = [
        ("region_code", "public.dim_region", "dim_detail", "dim_region", dim_id_late),
        ("region_code", "public.sales", "fact", "region", dim_id_early),
    ]

    class _Result:
        def all(self):
            return list(rows)

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Result())
    owners = await _load_security_column_owners(
        db, uuid.uuid4(), ["region_code"],
        dim_paths=["region.region_code"],
    )
    assert owners[0] == ("region_code", "sales"), (
        f"path/fact owner must win; got {owners!r}"
    )
    assert ("region_code", "dim_region") in owners

    pred = CompiledPredicate(
        sql_expression="\"region_code\" = 'NORTH'",
        active_rule_ids=("r1",),
        security_dimension_columns=("region_code",),
        security_column_owners=owners,
    )
    sql = (
        "SELECT d.category, SUM(f.amount) "
        "FROM demo.sales AS f "
        "JOIN demo.dim_region AS d ON f.region_id = d.id "
        "GROUP BY d.category"
    )
    out = _inject_security_where(sql, pred)
    assert _security_conjunct_count(out) == 1, (
        "Bug-7034: two owners must not fan the predicate. Got: " + out
    )
    ast = sqlglot.parse_one(out, read="postgres")
    where = ast.args.get("where")
    assert where is not None, f"expected a WHERE clause, got: {out}"
    dim_qualified = [
        col for col in where.find_all(exp.Column)
        if col.name == "region_code" and col.table == "d"
    ]
    assert not dim_qualified, (
        "path/fact owner is sales, but the predicate qualified dim_region: "
        f"{out}"
    )
    fact_qualified = [
        col for col in where.find_all(exp.Column)
        if col.name == "region_code" and col.table == "f"
    ]
    assert fact_qualified, f"expected f.region_code qualification, got: {out}"
