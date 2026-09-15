"""Bug-8637 — the served SQL and the aggregate CTAS traverse the join graph
with DIFFERENT algorithms, so on a model with two paths between the anchor and
a needed table they can join through different edges and answer the same
question with different numbers. Permanently, deterministically, with no
staleness signal.

Both sides are individually deterministic since Bug-8605 made the input order
canonical. Determinism is not agreement: each picks the same edges every time,
but not the SAME edges as the other.

The two algorithms:

* live/source (``rewrite/joins._build_joined_from_clause``) grows the joined set
  greedily and PREFERS a directly-reachable REQUIRED table, falling back to an
  intermediate only when no required table can be attached.
* aggregate CTAS (``shared/semantic/sql_builder._join_closure``) runs a BFS from
  the anchor and keeps a parent map — first-reached wins — with no notion of
  required-before-intermediate.

On the diamond below they disagree about whether table B belongs in the FROM
clause at all. An extra table on the path changes row multiplicity, so the same
question can return different totals depending only on which route served it.

Since 2026-09-05 both builders render ONE shared planner
(``shared.semantic.join_planner.plan_join_tree``, the live rule). This file is
now the equality guard: the two sides select the same tables in the same
order, the planner is a pure function of the model regardless of input order,
and the retired BFS closure is shown to be the thing that disagreed.
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from shared.semantic.graph_order import canonical_join_order
from shared.semantic.join_planner import plan_join_tree
from shared.semantic.sql_builder import _join_closure
from src.rewrite.joins import _build_joined_from_clause


def _diamond():
    """anchor A; A-B, A-C, B-D, C-D. Two equal-length routes A->D."""
    tables = {
        t: NS(id=t, physical_name=f"sch.{t}", alias=t) for t in "ABCD"
    }
    columns: dict[str, NS] = {}

    def _join(jid, lt, rt):
        lc, rc = f"c_{lt}_{jid}", f"c_{rt}_{jid}"
        columns[lc] = NS(id=lc, model_table_id=lt, column_name=f"{lt}_key")
        columns[rc] = NS(id=rc, model_table_id=rt, column_name=f"{rt}_key")
        return NS(
            id=jid, left_table_id=lt, right_table_id=rt,
            left_column_id=lc, right_column_id=rc, join_type="inner",
        )

    joins = [
        _join("j1", "A", "B"), _join("j2", "A", "C"),
        _join("j3", "B", "D"), _join("j4", "C", "D"),
    ]
    return tables, columns, joins


def _live_tables(tables, columns, joins, anchor, required):
    from_sql = _build_joined_from_clause(
        base_table_id=anchor,
        required_table_ids=set(required),
        joins=joins,
        tables_by_id=tables,
        columns_by_id=columns,
        alias_by_table_id={anchor: "t0"},
    )
    assert from_sql is not None, "the live builder could not span the graph"
    return {t for t in tables if f'"sch"."{t}"' in from_sql}, from_sql


def _aggregate_tables(joins, anchor, required):
    """What the aggregate CTAS joins: the shared plan's tables (in order)."""
    plan = plan_join_tree(anchor, set(required), joins, table_ids={"A", "B", "C", "D"})
    assert plan is not None
    return [anchor] + [step.to_table_id for step in plan]


def _legacy_bfs_tables(joins, anchor, required):
    """The retired aggregate traversal, kept only to show what disagreed."""
    adjacency: dict[str, list[str]] = {}
    for j in canonical_join_order(joins):
        adjacency.setdefault(j.left_table_id, []).append(j.right_table_id)
        adjacency.setdefault(j.right_table_id, []).append(j.left_table_id)
    return set(_join_closure(anchor, set(required), adjacency))


def test_bug8637_the_two_builders_select_the_same_tables():
    """The guard: same model, same question, same FROM on both routes."""
    tables, columns, joins = _diamond()
    live, from_sql = _live_tables(tables, columns, joins, "A", {"C", "D"})
    agg = _aggregate_tables(joins, "A", {"C", "D"})
    assert live == set(agg), f"live={sorted(live)} aggregate={agg} from={from_sql}"
    # The live rule prefers the directly reachable required tables and never
    # drags in the unrequired intermediate B.
    assert "B" not in live
    # The retired BFS closure did, which is the divergence this lane removed.
    assert "B" in _legacy_bfs_tables(joins, "A", {"C", "D"})
    # And the live SQL joins in the planned order.
    assert from_sql.index('"sch"."C"') < from_sql.index('"sch"."D"')


def test_bug8637_the_plan_is_a_pure_function_of_the_model():
    """Input order no longer matters anywhere: the planner canonicalises
    internally and walks the joined set in insertion order."""
    tables, columns, joins = _diamond()
    forward = _aggregate_tables(joins, "A", {"C", "D"})
    backward = _aggregate_tables(list(reversed(joins)), "A", {"C", "D"})
    assert forward == backward
    live_fwd, _ = _live_tables(tables, columns, joins, "A", {"C", "D"})
    live_bwd, _ = _live_tables(tables, columns, list(reversed(joins)), "A", {"C", "D"})
    assert live_fwd == live_bwd == set(forward)


def test_bug8637_intermediate_is_taken_only_when_no_required_table_attaches():
    """Triangle with a required leaf behind an intermediate: A-B, B-C, A-D;
    required {C}. C is not adjacent to A, so the planner takes B (the
    intermediate) and then C, and never D."""
    tables, columns, joins = _diamond()
    joins = [j for j in joins if j.id in ("j1", "j3")]  # A-B, B-D
    tables["D"] = tables["D"]
    plan = plan_join_tree("A", {"D"}, joins, table_ids=set(tables))
    assert [s.to_table_id for s in plan] == ["B", "D"]
    assert plan_join_tree("A", {"C"}, joins, table_ids=set(tables)) is None
