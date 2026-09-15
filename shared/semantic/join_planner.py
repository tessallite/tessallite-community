"""One spanning-tree planner for every SQL FROM clause (Bug-8637).

The live query path (``query-router/src/rewrite/joins.py``) and the aggregate
CTAS path (``shared/semantic/sql_builder.build_from_clause``, used by the
optimizer's create and the scheduler's full and incremental refresh) each
owned a join-graph walk with a different rule: the live side grew the joined
set greedily and preferred a directly reachable REQUIRED table, the aggregate
side ran a breadth-first search from the anchor and kept the first parent it
reached. On a model with two paths between the anchor and a needed table the
two could join through different edges, so an aggregate was built along one
path while the live query answered along the other -- different row
multiplicity, different totals, no signal.

Both builders now call :func:`plan_join_tree` and only RENDER its steps. The
rule kept is the live one, so every live answer is unchanged and the aggregate
side adopts it:

1. joins are taken in ``canonical_join_order`` (Bug-8605);
2. the joined set is walked in insertion order (a list, never a set, so the
   plan is a pure function of the model rather than of hashing);
3. at each step the first join that attaches a REQUIRED table to an
   already-joined table wins; only when no required table can be attached is
   the first join that attaches any INTERMEDIATE table taken;
4. ``None`` when a required table is unreachable.

Rendering (aliases, join keyword and its flip, type coercion, quoting,
physical-name overrides) stays with each builder; that is presentation, and
it was never the source of the divergence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from shared.semantic.graph_order import canonical_join_order


@dataclass(frozen=True)
class JoinStep:
    """One edge of the planned tree: attach ``to_table_id`` to the already
    joined ``from_table_id`` through ``join``."""

    from_table_id: Any
    join: Any
    to_table_id: Any

    @property
    def flipped(self) -> bool:
        """True when the already-joined side is the modeller's RIGHT table, so
        the renderer must flip the declared join keyword (Bug-7775 / Bug-8628)."""
        return self.from_table_id == self.join.right_table_id


def plan_join_tree(
    anchor_id: Any,
    required_table_ids: Iterable[Any],
    joins: Iterable[Any],
    *,
    table_ids: Iterable[Any] | None = None,
) -> list[JoinStep] | None:
    """Plan the ordered joins that connect ``anchor_id`` to every required table.

    ``table_ids`` restricts the graph to known tables (a join naming a table the
    caller does not know is ignored, as both builders always did). Required
    tables that equal the anchor are satisfied trivially. Returns ``None`` when
    some required table cannot be reached.
    """
    known = set(table_ids) if table_ids is not None else None
    ordered_joins = [
        j for j in canonical_join_order(list(joins))
        if known is None or (j.left_table_id in known and j.right_table_id in known)
    ]
    adjacency: dict[Any, list[Any]] = {}
    for j in ordered_joins:
        adjacency.setdefault(j.left_table_id, []).append(j)
        adjacency.setdefault(j.right_table_id, []).append(j)

    joined: list[Any] = [anchor_id]
    joined_set: set[Any] = {anchor_id}
    pending: set[Any] = set(required_table_ids) - {anchor_id}
    all_joinable = {t for j in ordered_joins for t in (j.left_table_id, j.right_table_id)}
    steps: list[JoinStep] = []

    def _attach(targets: set[Any]) -> bool:
        for table_id in list(joined):
            for j in adjacency.get(table_id, []):
                if j.left_table_id == table_id and j.right_table_id in targets:
                    nxt = j.right_table_id
                elif j.right_table_id == table_id and j.left_table_id in targets:
                    nxt = j.left_table_id
                else:
                    continue
                if nxt in joined_set:
                    continue
                steps.append(JoinStep(table_id, j, nxt))
                joined.append(nxt)
                joined_set.add(nxt)
                pending.discard(nxt)
                return True
        return False

    while pending:
        if _attach(pending):
            continue
        intermediates = all_joinable - joined_set
        if intermediates and _attach(intermediates):
            continue
        return None
    return steps
