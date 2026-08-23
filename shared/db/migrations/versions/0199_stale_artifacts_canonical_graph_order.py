"""Bug-8621 — stale artifacts for models whose FROM anchor or join expansion
changed due to the canonical-graph-order fix (Bug-8605).

Bug-8605 changed the FROM anchor for zero-fact multi-table models from
"whatever the database returned first" to "first table in canonical ``id``
order" and changed the BFS join expansion from Python set-iteration order to
canonical join order. Every already-materialised aggregate and pocket for an
affected model was built under the old rule. Nothing in the definition-closure
invalidation path catches this — the definition did not change, the code did.

This migration conservatively stales every aggregate and pocket belonging to:

1. A model with ZERO fact tables and MORE THAN ONE model table — the anchor may
   have changed.
2. A model whose join graph contains a CYCLE (two paths between anchor and any
   reachable table) — the BFS spanning-tree choice may have changed.

Staling: aggregates ``is_stale=True`` where ``status='active'``, pockets
``status='stale'`` where ``status='fresh'`` and not retired. The scheduler's
due-selection rebuilds on the next sweep; the runtime matchers fail closed on
the built-for gate, so a stale artifact is non-servable (source fallback)
until rebuilt. Safe to be over-broad — staling produces a rebuild, never a
wrong number.

Idempotent: no-op on re-run once the staling has been applied.

Revision ID: 0199
Revises: 0198
Create Date: 2026-08-07
"""
from __future__ import annotations

import logging
from collections import defaultdict

import sqlalchemy as sa
from alembic import op

revision = "0199"
down_revision = "0198"
branch_labels = None
depends_on = None

_FACT_TABLE_TYPE = "fact"


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def cyclic_model_ids(edges) -> set[str]:
    """Model ids whose join graph contains a cycle.

    ``edges`` is an iterable of ``(model_id, left_table_id, right_table_id)``.
    A cycle means two paths exist between the anchor and some reachable table,
    so the BFS spanning-tree choice may have changed under Bug-8605.

    Module-level and pure so the gate is unit-testable without a database:
    the cost of getting this wrong is a FALSE NEGATIVE — an artifact that
    silently keeps serving rows built under the old join order.
    """
    model_edges: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for mid, lid, rid in edges:
        model_edges[mid].append((lid, rid))

    cyclic: set[str] = set()
    for mid, model_edge_list in model_edges.items():
        adj: dict[str, list[str]] = defaultdict(list)
        # A PARALLEL EDGE (two joins between the same pair of tables) and a
        # SELF-JOIN are both cycles, but the parent-skip DFS below cannot see
        # a parallel edge: it skips every neighbour equal to the parent, so
        # the second A-B edge looks like the one it arrived on. Detect both
        # from the edge multiset up front.
        seen_pairs: set[tuple[str, str]] = set()
        for lid, rid in model_edge_list:
            if lid == rid:
                cyclic.add(mid)
            pair = (lid, rid) if lid <= rid else (rid, lid)
            if pair in seen_pairs:
                cyclic.add(mid)
            seen_pairs.add(pair)
            adj[lid].append(rid)
            adj[rid].append(lid)
        if mid in cyclic:
            continue

        # Undirected DFS: a neighbour already visited and not the parent
        # proves a second path, i.e. a cycle.
        visited: set[str] = set()
        for start in list(adj.keys()):
            if start in visited or mid in cyclic:
                continue
            stack: list[tuple[str, str | None]] = [(start, None)]
            while stack:
                node, parent = stack.pop()
                if node in visited:
                    continue
                visited.add(node)
                for neighbor in adj.get(node, []):
                    if neighbor == parent:
                        continue
                    if neighbor in visited:
                        cyclic.add(mid)
                        break
                    stack.append((neighbor, node))
                if mid in cyclic:
                    break
    return cyclic


def upgrade() -> None:
    bind = op.get_bind()

    # Only run when model_tables exists (tenant schema ready).
    if not _has_table("model_tables"):
        return

    # ------------------------------------------------------------------
    # Collect model ids affected by the anchor change: models with ZERO
    # fact tables and MORE THAN ONE table total.
    # ------------------------------------------------------------------
    tally = bind.execute(
        sa.text(
            "SELECT model_id, COUNT(*) AS cnt "
            "FROM model_tables GROUP BY model_id"
        )
    ).fetchall()

    # Map of model_id -> table count for multi-table models
    multi_table: dict[str, int] = {}
    for row in tally:
        mid, cnt = str(row[0]), row[1]
        if cnt > 1:
            multi_table[mid] = cnt

    if multi_table:
        fact_rows = bind.execute(
            sa.text(
                "SELECT model_id FROM model_tables "
                "WHERE table_type = :fact_type"
            ),
            {"fact_type": _FACT_TABLE_TYPE},
        ).fetchall()
        for row in fact_rows:
            multi_table.pop(str(row[0]), None)

    zero_fact_ids: set[str] = set(multi_table.keys())

    # ------------------------------------------------------------------
    # Collect model ids with cyclic join graphs (Bug-8637 co-gate).
    # ------------------------------------------------------------------
    # Guarded like every other table this migration touches: a tenant schema
    # without ``joins`` has no join graph to be cyclic.
    cyclic_ids: set[str] = set()
    if _has_table("joins"):
        join_rows = bind.execute(
            sa.text("SELECT model_id, left_table_id, right_table_id FROM joins")
        ).fetchall()
        cyclic_ids = cyclic_model_ids(
            (str(row[0]), str(row[1]), str(row[2])) for row in join_rows
        )

    affected: set[str] = zero_fact_ids | cyclic_ids
    log = logging.getLogger("alembic.runtime.migration")
    if not affected:
        # Log the no-op too. This migration MUTATES rows, so "it ran and
        # decided to change nothing" is exactly the fact an operator needs
        # recorded — silence is indistinguishable from the gate never
        # having been evaluated.
        log.info(
            "0199 canonical-graph-order staling: no model matched "
            "(0 zero-fact multi-table, 0 cyclic join graph); nothing staled."
        )
        return

    model_ids = sorted(affected)

    # ------------------------------------------------------------------
    # Stale aggregates: active, not already stale.
    #
    # Set-based, not a per-model loop: this runs inside the upgrade
    # transaction and a tenant with thousands of models would otherwise pay
    # one round trip each. ``uuid[]`` binding keeps a single statement.
    # ------------------------------------------------------------------
    staled_aggregates = 0
    if _has_table("aggregate_definitions"):
        staled_aggregates = bind.execute(
            sa.text(
                "UPDATE aggregate_definitions "
                "   SET is_stale = TRUE "
                " WHERE model_id = ANY(CAST(:model_ids AS uuid[])) "
                "   AND status = 'active' "
                "   AND is_stale IS FALSE"
            ),
            {"model_ids": model_ids},
        ).rowcount

    # ------------------------------------------------------------------
    # Stale pockets: fresh, not retired.
    # ------------------------------------------------------------------
    staled_pockets = 0
    if _has_table("pocket_definitions"):
        staled_pockets = bind.execute(
            sa.text(
                "UPDATE pocket_definitions "
                "   SET status = 'stale' "
                " WHERE model_id = ANY(CAST(:model_ids AS uuid[])) "
                "   AND status = 'fresh' "
                "   AND retired_at IS NULL"
            ),
            {"model_ids": model_ids},
        ).rowcount

    # ------------------------------------------------------------------
    # OPERATOR NOTE (blast radius).
    #
    # Every row counted here rebuilds on the scheduler's next sweep. Until it
    # does, the runtime matchers fail closed on the built-for gate, so the
    # query falls back to the SOURCE: results stay CORRECT, but those queries
    # lose aggregate acceleration and the rebuild consumes source/target
    # warehouse compute. That is the intended trade — an artifact built under
    # the pre-Bug-8605 join order can return WRONG NUMBERS, and a rebuild is
    # the only way to clear it.
    #
    # The counts are logged (not merely computed) so an operator can see the
    # cost BEFORE the scheduler starts rebuilding, and can stage the sweep if
    # the number is large. Measured on the seeded tenants at integration time
    # (acme-demo, demo, acme, large) this gate matched ZERO models: every
    # model there is a single-fact acyclic tree, so both conditions are false
    # and this migration is a no-op. A non-zero count means the tenant really
    # does have a zero-fact multi-table model or a cyclic join graph.
    # ------------------------------------------------------------------
    log.warning(
        "0199 canonical-graph-order staling: %d model(s) matched "
        "(%d zero-fact multi-table, %d cyclic join graph); "
        "staled %d aggregate(s) and %d pocket(s). "
        "These rebuild on the next scheduler sweep; queries fall back to "
        "source (correct but unaccelerated) until they do.",
        len(model_ids),
        len(zero_fact_ids),
        len(cyclic_ids),
        staled_aggregates,
        staled_pockets,
    )


def downgrade() -> None:
    """No downgrade: staling is an operational upgrade step. Rolling back the
    migration does not un-stale artifacts — a stale artifact will be rebuilt
    by the next scheduler sweep, which is correct regardless of which code
    version triggered the rebuild."""
    pass
