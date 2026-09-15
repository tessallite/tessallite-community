"""1.1.7 first-delivery deep review, round 1 -- regression guards for two
row-security binding defects introduced by the reviewed change set
(handoff: work/reviews/1-1-7-first-delivery-deep-review-handoff.md).

F-R2-01 (Bug-9373 follow-up). ``_qualify_for_select`` now emits one predicate
copy per *column-to-alias combination*, so a predicate that names two security
columns owned by the same relation is also emitted with the columns bound to
DIFFERENT scans of that relation: ``(e1.region_code = 'NORTH' OR e2.dept =
'OPS')``. A row-security predicate is a per-row condition on ONE relation row;
a copy that evaluates half of it on one scan and half on another is not the
modeller's rule. For an OR-composed predicate (two named-role grants for a
multi-role principal, F-007-03) the mixed copies exclude rows the pure
per-alias copies admit -- under-reporting, silent. The base SHA emitted the
pure copies ``P[e1] AND P[e2]`` for this shape.

F-R2-02 (Bug-9914 follow-up). ``rewrite_for_raw`` adds the resolved security
owner table to ``required_table_ids`` BEFORE base-table selection. When the
owner is another FACT table (the rule's dimension is sourced from a fact
column, ranked first by ``_load_security_column_owners``), the owner fact can
win the ``fact in required_table_ids`` pass and become the FROM relation; the
fact the user actually queried is then LEFT JOINed through the shared
dimension -- a many-to-many fan-out that multiplies every returned row. The
base SHA refused this shape (owner not scanned -> 403); the tip returns a
silently multiplied row set.

Both tests FAIL on main ad9db11ca and pass once the bindings are per-relation
and the queried fact stays the raw base (or the shape is refused).
Tier: T3 (row-level security, wrong numbers).
"""
from __future__ import annotations

import uuid

import pytest
import sqlglot
from sqlglot import exp

from src.rewrite.raw_sql import RawRouteUnsupported, rewrite_for_raw
from src.routing.router import _inject_security_where
from src.security import CompiledPredicate

from test_rewrite_raw import (
    _MockDB,
    _bound,
    _column,
    _join,
    _measure,
    _patch_graph,
    _table,
)


def _top_level_conjuncts(out_sql: str) -> list[exp.Expression]:
    ast = sqlglot.parse_one(out_sql, read="postgres")
    parts: list[exp.Expression] = []

    def walk(e: exp.Expression) -> None:
        if isinstance(e, exp.And):
            walk(e.left)
            walk(e.right)
        else:
            parts.append(e)

    walk(ast.args["where"].this)
    return parts


def _aliases_in(conjunct: exp.Expression) -> set[str]:
    return {c.table for c in conjunct.find_all(exp.Column) if c.table}


def test_f_r2_01_multi_column_or_predicate_binds_each_copy_to_one_scan_of_a_self_joined_owner():
    """Two grants on two columns of ``employees`` (OR-composed), employees
    scanned twice: exactly one predicate copy per scan, every copy bound to a
    single alias, and both scans covered. No copy may mix ``e1`` and ``e2``."""
    pred = CompiledPredicate(
        sql_expression="(\"region_code\" = 'NORTH' OR \"dept\" = 'OPS')",
        active_rule_ids=("r_region", "r_dept"),
        security_dimension_columns=("region_code", "dept"),
        security_column_owners=(("region_code", "employees"), ("dept", "employees")),
    )
    sql = (
        "SELECT e1.region_code FROM demo.sales AS s "
        "JOIN demo.employees AS e1 ON s.emp_id = e1.id "
        "JOIN demo.employees AS e2 ON e1.mgr_id = e2.id"
    )
    out = _inject_security_where(sql, pred)
    conjuncts = _top_level_conjuncts(out)
    per_copy_aliases = [_aliases_in(c) for c in conjuncts]
    assert all(len(a) == 1 for a in per_copy_aliases), (
        "a predicate copy mixes columns from two different scans "
        f"(a per-row rule evaluated across two rows): {out}"
    )
    assert sorted(next(iter(a)) for a in per_copy_aliases) == ["e1", "e2"], (
        "expected exactly one copy per scanned owner alias; got " + out
    )


def test_f_r2_01_two_relations_each_owning_both_columns_get_one_pure_copy_each():
    """``sales`` and ``employees`` both carry ``region_code`` AND ``dept``:
    the correct emission is ``P[s] AND P[e]`` -- two copies, no mixed copy."""
    pred = CompiledPredicate(
        sql_expression="(\"region_code\" = 'NORTH' OR \"dept\" = 'OPS')",
        active_rule_ids=("r_region", "r_dept"),
        security_dimension_columns=("region_code", "dept"),
        security_column_owners=(
            ("region_code", "sales"), ("region_code", "employees"),
            ("dept", "sales"), ("dept", "employees"),
        ),
    )
    sql = (
        "SELECT e.region_code FROM demo.sales AS s "
        "JOIN demo.employees AS e ON s.emp_id = e.id"
    )
    out = _inject_security_where(sql, pred)
    conjuncts = _top_level_conjuncts(out)
    per_copy_aliases = [_aliases_in(c) for c in conjuncts]
    assert all(len(a) == 1 for a in per_copy_aliases), out
    assert sorted(next(iter(a)) for a in per_copy_aliases) == ["e", "s"], out


async def test_f_r2_02_raw_plan_never_makes_a_security_owner_fact_the_base_of_another_facts_query(monkeypatch):
    """Multi-fact model: ``payment_transaction`` (owns the security column
    through a fact-sourced dimension) and ``refunds`` (queried), sharing
    ``dim_channel_code``. The raw plan for a refunds-only query must keep
    ``refunds`` as its FROM relation and must not scan the other fact --
    or decline to the source route. It must never emit
    ``FROM payment_transaction ... LEFT JOIN refunds``."""
    owner_fact = _table("payment_transaction", "demo.payment_transaction", table_type="fact")
    queried_fact = _table("refunds", "demo.refunds", table_type="fact")
    dim = _table("dim_channel_code", "demo.dim_channel_code")
    # Deterministic base-table tie-break sorts by str(id): make the owner sort first.
    owner_fact.id = uuid.UUID(int=1)
    queried_fact.id = uuid.UUID(int=2)
    dim.id = uuid.UUID(int=3)
    pt_channel = _column("channel_code", owner_fact, "text")
    rf_amount = _column("refund_amount", queried_fact, "numeric")
    rf_channel = _column("channel_code", queried_fact, "text")
    d_channel = _column("channel_code", dim, "text")
    joins = [
        _join(owner_fact, dim, pt_channel, d_channel, join_type="left"),
        _join(queried_fact, dim, rf_channel, d_channel, join_type="left"),
    ]
    _patch_graph(
        monkeypatch, [owner_fact, queried_fact, dim], joins,
        [pt_channel, rf_amount, rf_channel, d_channel],
    )
    bound = _bound([_measure("refund_amount", rf_amount)], [])
    owners = (("channel_code", "payment_transaction"), ("channel_code", "dim_channel_code"))

    try:
        sql = await rewrite_for_raw(
            bound, _MockDB({}, [], {}, {}), target_dialect="postgres",
            security_column_owners=owners,
        )
    except RawRouteUnsupported:
        return  # declining to the source route is an acceptable outcome

    ast = sqlglot.parse_one(sql, read="postgres")
    base = ast.find(exp.From).this.name
    scanned = {t.name for t in ast.find_all(exp.Table)}
    assert base == "refunds", (
        "the queried fact must stay the raw base; the security owner fact "
        f"took the FROM position and fans out every refund row: {sql}"
    )
    assert "payment_transaction" not in scanned, (
        "joining a second fact through the shared dimension multiplies the "
        f"queried fact's rows: {sql}"
    )


# ---------------------------------------------------------------------------
# F-R2-03 / Bug-9930 -- source-route twin of the raw-route guard above, plus
# the cardinality-aware acceptance cases the shared resolver must keep.
# ---------------------------------------------------------------------------

from src.ir.logical_query import SemanticBindingError  # noqa: E402
from src.rewrite.query_rewriter import rewrite_for_source  # noqa: E402

from conftest import attach_fixture_deployed_shape  # noqa: E402
from test_render_golden import (  # noqa: E402
    FakeDB,
    _bound as _golden_bound,
    _col,
    _meas,
    _se,
    _tbl,
)


def _two_fact_source_db(*, direct_edge=None):
    """``payment_transaction`` (owns the security column) and ``refunds``
    (queried) share ``dim_channel_code``. ``direct_edge`` optionally adds a
    refunds -> payment_transaction join with the given (join_type, cardinality).

    Persisted models carry exactly one ``fact`` row
    (``uq_model_tables_one_fact_per_model``), so the second measure-bearing
    relation is typed like any other non-fact table; the dimension's key is
    the introspected primary key, exactly as ``information_schema`` reports it.
    Neither fact->dim edge declares a cardinality, which is how most deployed
    joins look."""
    import types

    owner_fact = _tbl("t-pt", "demo.payment_transaction", "pt", table_type="dim_aggregate")
    queried_fact = _tbl("t-rf", "demo.refunds", "rf", table_type="fact")
    dim = _tbl("t-dim", "demo.dim_channel_code", "dc", table_type="dim_aggregate")
    dim_key = _col("c-dim-channel", "t-dim", "channel_code")
    dim_key.is_primary_key = True
    cols = [
        _col("c-pt-channel", "t-pt", "channel_code"),
        _col("c-pt-id", "t-pt", "id", data_type="integer"),
        _col("c-rf-amount", "t-rf", "refund_amount", data_type="numeric"),
        _col("c-rf-channel", "t-rf", "channel_code"),
        _col("c-rf-payment", "t-rf", "payment_id", data_type="integer"),
        dim_key,
    ]
    joins = [
        types.SimpleNamespace(
            id="j-pt-dim", model_id="golden-model", left_table_id="t-pt",
            left_column_id="c-pt-channel", right_table_id="t-dim",
            right_column_id="c-dim-channel", join_type="left",
        ),
        types.SimpleNamespace(
            id="j-rf-dim", model_id="golden-model", left_table_id="t-rf",
            left_column_id="c-rf-channel", right_table_id="t-dim",
            right_column_id="c-dim-channel", join_type="left",
        ),
    ]
    if direct_edge is not None:
        join_type, cardinality = direct_edge
        joins.append(types.SimpleNamespace(
            id="j-rf-pt", model_id="golden-model", left_table_id="t-rf",
            left_column_id="c-rf-payment", right_table_id="t-pt",
            right_column_id="c-pt-id", join_type=join_type,
            cardinality=cardinality,
        ))
    refund_amount = _meas("refund_amount", source_column_id="c-rf-amount")
    bound = _golden_bound(
        measures=[refund_amount],
        raw_query="SELECT SUM(refund_amount) FROM m",
        select_expressions=[
            _se("SUM(refund_amount)", classification="analytical",
                agg_function="sum", inner_column="refund_amount"),
        ],
    )
    db = FakeDB(tables=[owner_fact, queried_fact, dim], columns=cols,
                joins=joins, measures=[refund_amount])
    return bound, db


def _scanned(sql: str) -> set[str]:
    return {t.name for t in sqlglot.parse_one(sql, read="postgres").find_all(exp.Table)}


_OWNERS_WITH_DIM_FALLBACK = (
    ("channel_code", "payment_transaction"), ("channel_code", "dim_channel_code"),
)
_OWNERS_FACT_ONLY = (("channel_code", "payment_transaction"),)


async def test_f_r2_03_source_plan_never_joins_a_security_owner_fact_across_a_shared_dimension():
    """Source-route twin of TP-3: the owner fact is reachable only through
    ``dim_channel_code`` (one-to-many away from the queried fact), so the
    resolver must fall back to the dimension owner; the plan scans refunds and
    the dimension only, and the predicate binds to the dimension scan."""
    bound, db = _two_fact_source_db()
    await attach_fixture_deployed_shape(bound, db)
    sql = await rewrite_for_source(
        bound, db, target_dialect="postgres",
        security_column_owners=_OWNERS_WITH_DIM_FALLBACK,
    )
    scanned = _scanned(sql)
    assert "payment_transaction" not in scanned, (
        f"a second fact joined through the shared dimension multiplies refunds: {sql}"
    )
    assert scanned == {"refunds", "dim_channel_code"}, sql
    pred = CompiledPredicate(
        sql_expression="\"channel_code\" IN ('WEB', 'MOBILE')",
        active_rule_ids=("r",), security_dimension_columns=("channel_code",),
        security_column_owners=_OWNERS_WITH_DIM_FALLBACK,
    )
    injected = _inject_security_where(sql, pred)
    conjuncts = _top_level_conjuncts(injected)
    assert [_aliases_in(c) for c in conjuncts] == [{"dc"}], injected


async def test_f_r2_03_owner_reachable_only_across_a_many_side_is_refused_on_both_routes(monkeypatch):
    """No dimension fallback: the only owner is the other fact. Neither route
    may join it; the source route raises the typed refusal and the raw route
    declines to the source route (which then fails closed)."""
    bound, db = _two_fact_source_db()
    await attach_fixture_deployed_shape(bound, db)
    with pytest.raises(SemanticBindingError, match="multiply its rows"):
        await rewrite_for_source(
            bound, db, target_dialect="postgres",
            security_column_owners=_OWNERS_FACT_ONLY,
        )

    owner_fact = _table("payment_transaction", "demo.payment_transaction")
    queried_fact = _table("refunds", "demo.refunds", table_type="fact")
    dim = _table("dim_channel_code", "demo.dim_channel_code")
    pt_channel = _column("channel_code", owner_fact, "text")
    rf_amount = _column("refund_amount", queried_fact, "numeric")
    rf_channel = _column("channel_code", queried_fact, "text")
    d_channel = _column("channel_code", dim, "text")
    d_channel.is_primary_key = True
    _patch_graph(
        monkeypatch, [owner_fact, queried_fact, dim],
        [_join(owner_fact, dim, pt_channel, d_channel, join_type="left"),
         _join(queried_fact, dim, rf_channel, d_channel, join_type="left")],
        [pt_channel, rf_amount, rf_channel, d_channel],
    )
    raw_bound = _bound([_measure("refund_amount", rf_amount)], [])
    with pytest.raises(RawRouteUnsupported, match="multiply its rows"):
        await rewrite_for_raw(
            raw_bound, _MockDB({}, [], {}, {}), target_dialect="postgres",
            security_column_owners=_OWNERS_FACT_ONLY,
        )


@pytest.mark.parametrize(
    "direct_edge",
    [("left", "many_to_one"), ("many_to_one", None)],
    ids=["declared-cardinality", "legacy-token-coerced"],
)
async def test_f_r2_03_owner_fact_on_a_declared_to_one_edge_is_joined(direct_edge):
    """A declared many-to-one step from the queried fact to the owner fact
    (refunds.payment_id -> payment_transaction.id) preserves refund rows, so
    the owner fact IS the right relation to join. A legacy ``many_to_one``
    token parked in ``join_type`` is coerced, not rejected (invariant 4)."""
    bound, db = _two_fact_source_db(direct_edge=direct_edge)
    await attach_fixture_deployed_shape(bound, db)
    sql = await rewrite_for_source(
        bound, db, target_dialect="postgres",
        security_column_owners=_OWNERS_FACT_ONLY,
    )
    assert "payment_transaction" in _scanned(sql), sql
    base = sqlglot.parse_one(sql, read="postgres").find(exp.From).this.name
    assert base == "refunds", sql
