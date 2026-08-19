"""Join-clause construction for the query rewriter.

Builds the JOIN graph traversal and FROM clause (PostgreSQL-canonical) and the
diagnostic message raised when a hierarchy path cannot be joined.

Extracted from query_rewriter.py (Phase 3 decomposition); behaviour-identical.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Sequence

from shared.connector_qualify import coerce_join_types, quote_identifier, quote_table_ref
from shared.semantic.graph_order import canonical_join_order
from shared.semantic.join_keyword import join_keyword as _join_keyword



def _coerce_join_pair(
    lhs_expr: str, lhs_type: str | None,
    rhs_expr: str, rhs_type: str | None,
) -> tuple[str, str]:
    """Truncate the TIMESTAMP side to DATE when joining DATE to TIMESTAMP columns.

    Delegates to ``shared.connector_qualify.coerce_join_types`` — the single
    source of truth for cross-type join coercion.
    """
    return coerce_join_types(lhs_expr, lhs_type, rhs_expr, rhs_type)


def _build_joined_from_clause(
    *,
    base_table_id: Any,
    required_table_ids: set[Any],
    joins: list[Any],
    tables_by_id: dict[Any, Any],
    columns_by_id: dict[Any, Any],
    alias_by_table_id: dict[Any, str],
    connector: str = "postgresql",
    preferred_join_ids: Sequence[str] | None = None,
) -> str | None:
    base_table = tables_by_id.get(base_table_id)
    if not base_table:
        return None

    # Bug-8605 round-3 review: normalise HERE rather than trusting the caller.
    #
    # Join order is load-bearing — it breaks ties between equally short
    # base-to-table paths, so a different order puts different INTERMEDIATE
    # tables in the FROM clause, with different join keywords and different ON
    # columns. Three review rounds each found one more caller that handed this
    # builder an unordered graph (the latest: the DEPLOYED snapshot's stored
    # ``joins`` list, hydrated straight out of JSON and never re-sorted, so
    # every snapshot written before the serialiser emitted canonical order
    # made the served SQL expand the graph differently from the CTAS).
    #
    # Ordering the input at the point of use makes an unordered graph
    # unobtainable here regardless of who calls it, which a static
    # "did the caller remember?" guard demonstrably cannot.
    joins = canonical_join_order(joins)

    adjacency: dict[Any, list[Any]] = defaultdict(list)
    for join in joins:
        adjacency[join.left_table_id].append(join)
        adjacency[join.right_table_id].append(join)

    def _qid(name: str) -> str:
        return quote_identifier(connector, name)

    def _qtbl(dotted: str) -> str:
        return quote_table_ref(connector, dotted)

    def _qcol(table_alias: str, col_name: str) -> str:
        return f"{_qid(table_alias)}.{_qid(col_name)}"

    from_clause = f"{_qtbl(base_table.physical_name)} AS {_qid(alias_by_table_id[base_table_id])}"
    joined_table_ids = {base_table_id}
    pending_table_ids = set(required_table_ids) - {base_table_id}

    if preferred_join_ids:
        joins_by_id = {str(getattr(join, "id", "")): join for join in joins}
        preferred = [joins_by_id.get(str(join_id)) for join_id in preferred_join_ids]
        if any(join is None for join in preferred):
            return None

        def _append_preferred(path: list[Any]) -> tuple[str, set[Any], set[Any]] | None:
            forced_from = from_clause
            forced_joined = set(joined_table_ids)
            forced_pending = set(pending_table_ids)
            for join in path:
                table_id = None
                next_table_id = None
                if join.left_table_id in forced_joined and join.right_table_id not in forced_joined:
                    table_id = join.left_table_id
                    next_table_id = join.right_table_id
                elif join.right_table_id in forced_joined and join.left_table_id not in forced_joined:
                    table_id = join.right_table_id
                    next_table_id = join.left_table_id
                elif join.left_table_id in forced_joined and join.right_table_id in forced_joined:
                    continue
                else:
                    return None

                next_table = tables_by_id.get(next_table_id)
                if not next_table:
                    return None

                if next_table_id not in alias_by_table_id:
                    alias_by_table_id[next_table_id] = next_table.alias or f"t_{len(alias_by_table_id)}"

                current_col_id = join.left_column_id if join.left_table_id == table_id else join.right_column_id
                next_col_id = join.right_column_id if join.left_table_id == table_id else join.left_column_id
                current_col = columns_by_id.get(current_col_id)
                next_col = columns_by_id.get(next_col_id)
                if not current_col or not next_col:
                    return None

                # Bug-7775: the modeler's join_type is defined relative to
                # ``join.left_table_id`` / ``join.right_table_id``.  Here the
                # already-joined ``table_id`` is the FROM/left side; the traversal
                # is FLIPPED when that table is the modeler's RIGHT table (so the
                # added ``next_table_id`` is the modeler's LEFT table).
                flipped = table_id == join.right_table_id
                join_keyword = _join_keyword(join.join_type, flipped=flipped)
                lhs_expr = _qcol(alias_by_table_id[table_id], current_col.column_name)
                rhs_expr = _qcol(alias_by_table_id[next_table_id], next_col.column_name)
                lhs_expr, rhs_expr = _coerce_join_pair(
                    lhs_expr, getattr(current_col, "data_type", None),
                    rhs_expr, getattr(next_col, "data_type", None),
                )
                forced_from += (
                    f" {join_keyword} {_qtbl(next_table.physical_name)} AS {_qid(alias_by_table_id[next_table_id])}"
                    f" ON {lhs_expr} = {rhs_expr}"
                )
                forced_joined.add(next_table_id)
                forced_pending.discard(next_table_id)

            return forced_from, forced_joined, forced_pending

        ordered_path = [join for join in preferred if join is not None]
        forced = _append_preferred(ordered_path)
        if forced is None:
            forced = _append_preferred(list(reversed(ordered_path)))
        if forced is None:
            return None
        from_clause, joined_table_ids, pending_table_ids = forced

    # All table IDs known to the join graph — candidates for intermediate hops.
    all_joinable_ids = set()
    for join in joins:
        all_joinable_ids.add(join.left_table_id)
        all_joinable_ids.add(join.right_table_id)

    def _try_join_to(target_set: set) -> bool:
        """Try to join from any already-joined table to a table in target_set.
        Returns True if a join was added."""
        for table_id in list(joined_table_ids):
            for join in adjacency.get(table_id, []):
                next_table_id = None
                if join.left_table_id == table_id and join.right_table_id in target_set:
                    next_table_id = join.right_table_id
                elif join.right_table_id == table_id and join.left_table_id in target_set:
                    next_table_id = join.left_table_id
                if next_table_id is None:
                    continue

                next_table = tables_by_id.get(next_table_id)
                if not next_table:
                    continue

                if next_table_id not in alias_by_table_id:
                    alias_by_table_id[next_table_id] = next_table.alias or f"t_{len(alias_by_table_id)}"

                current_col_id = join.left_column_id if join.left_table_id == table_id else join.right_column_id
                next_col_id = join.right_column_id if join.left_table_id == table_id else join.left_column_id
                current_col = columns_by_id.get(current_col_id)
                next_col = columns_by_id.get(next_col_id)
                if not current_col or not next_col:
                    continue

                nonlocal from_clause
                # Bug-7775: flip LEFT<->RIGHT when the already-joined table is
                # the modeler's RIGHT table (see _append_preferred above).
                flipped = table_id == join.right_table_id
                join_keyword = _join_keyword(join.join_type, flipped=flipped)
                lhs_expr = _qcol(alias_by_table_id[table_id], current_col.column_name)
                rhs_expr = _qcol(alias_by_table_id[next_table_id], next_col.column_name)
                lhs_expr, rhs_expr = _coerce_join_pair(
                    lhs_expr, getattr(current_col, "data_type", None),
                    rhs_expr, getattr(next_col, "data_type", None),
                )
                from_clause += (
                    f" {join_keyword} {_qtbl(next_table.physical_name)} AS {_qid(alias_by_table_id[next_table_id])}"
                    f" ON {lhs_expr} = {rhs_expr}"
                )
                joined_table_ids.add(next_table_id)
                pending_table_ids.discard(next_table_id)
                return True
        return False

    while pending_table_ids:
        # First: try to join directly to a required table.
        if _try_join_to(pending_table_ids):
            continue
        # Second: try any reachable intermediate table as a stepping stone.
        intermediates = all_joinable_ids - joined_table_ids
        if intermediates and _try_join_to(intermediates):
            continue
        # No progress possible — graph is disconnected.
        return None

    return from_clause


# Bug-7018 / Bug-7775 / Bug-8628: the join-type vocabulary and the flip rule
# now live in ONE place for every SQL builder —
# ``shared/semantic/join_keyword.py``, imported as ``_join_keyword`` at the top
# of this module. This module keeps the name (``query_rewriter`` re-exports it
# and the Bug-7775 suite imports it) but no longer owns a second copy of the
# logic: the CTAS builder in ``shared/semantic/sql_builder.py`` had drifted to
# a flat, flip-blind map and materialised the opposite row population to what
# this route served. See
# docs/architecture/architecture_join-orientation-and-cardinality.md
# (invariant 1: one join-keyword function, not two).


def _missing_join_error_message(
    *,
    base_table_id: Any,
    required_table_ids: set[Any],
    joins: list[Any],
    tables_by_id: dict[Any, Any],
) -> str:
    adjacency: dict[Any, list[Any]] = defaultdict(list)
    for join in joins:
        adjacency[join.left_table_id].append(join.right_table_id)
        adjacency[join.right_table_id].append(join.left_table_id)

    reachable: set[Any] = {base_table_id}
    queue: list[Any] = [base_table_id]
    while queue:
        current = queue.pop(0)
        for nxt in adjacency.get(current, []):
            if nxt in reachable:
                continue
            reachable.add(nxt)
            queue.append(nxt)

    missing = sorted(required_table_ids - reachable, key=str)
    if not missing:
        return "Cannot resolve hierarchy path. Missing join between required hierarchy tables."

    base_table = tables_by_id.get(base_table_id)
    missing_table = tables_by_id.get(missing[0])
    base_name = (
        (base_table.alias or base_table.physical_name)
        if base_table is not None
        else str(base_table_id)
    )
    missing_name = (
        (missing_table.alias or missing_table.physical_name)
        if missing_table is not None
        else str(missing[0])
    )
    return f"Cannot resolve hierarchy path. Missing join between '{base_name}' and '{missing_name}'."
