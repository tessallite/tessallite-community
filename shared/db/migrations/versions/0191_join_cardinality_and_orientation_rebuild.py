"""Bug-8628 / Bug-8618 join-orientation contract rollout (tenant branch).

Three changes, one revision, because they are one semantic move and must not
be able to land half-applied:

1. ``joins.cardinality`` — the new, explicit fan-out field. ``join_type`` used
   to carry BOTH "which rows survive" and "how many rows match", with
   ``many_to_one`` as its storage default (a cardinality, not a join type).
2. Backfill ``cardinality`` from a cardinality token found in ``join_type``,
   and move the ``join_type`` server default to ``inner``, so no NEW row can
   reintroduce the conflation. Existing ``join_type`` values are LEFT ALONE —
   see "Why join_type is not rewritten" below.
3. Stale every materialised aggregate/pocket whose model contains — or whose
   stored version snapshots ever contained — a join the Bug-8628 fix
   re-renders (contract invariant 6). Without this the artifact keeps the
   pre-fix row population while the source route computes the post-fix one,
   and nothing marks the disagreement. The snapshot half of that union
   matters because an artifact's rows reflect BUILD-time tokens, which a
   later un-redeployed join edit can move out of the live graph.

Why join_type is NOT rewritten
------------------------------
Rewriting a legacy ``many_to_one`` to a real orientation looks like the
completion of the split, and it is not safe as an unattended migration. The
value is mirrored verbatim into every deployed model-version snapshot, which
is what the query-router binds each query to and what
``shared/definition_closure.py`` diffs the live graph against. Moving one side
without the other makes the live graph DIFFER from the deployed snapshot on
``joins.join_type``, and the closure then refuses every aggregate and pocket
refresh on that model until a human redeploys it. That trades a silent
wrong-number bug for an unattended availability outage — on models whose
rendering is, in the common case, not even changing. Rewriting the stored
snapshots instead would mutate immutable version history that revert relies
on.

Adding ``cardinality`` has no such effect: ``_compare_group`` iterates the
fields the SNAPSHOT carries, so a column added after a snapshot was written is
invisible to the comparison by design.

Existing rows therefore keep rendering exactly as before
(``join_keyword`` coerces a legacy token to an un-flipped ``LEFT JOIN``,
contract invariant 4), and a modeller declares the real orientation through
the Joins panel or a redeploy. The acme-demo seed does exactly that.

Revision ID: 0191
Revises: 0190
Create Date: 2026-08-04

Re-parented from 0188 onto 0190 (Bug-8642): the concurrently-running
join-population-governance lane authored ``0190_join_population_governance``
off the same tenant head. Two siblings off 0188 make ``alembic upgrade
tenant@head`` fail with "Multiple head revisions are present", and env.py
forbids a merge revision because it would collapse the deliberate
system/tenant branch split. The two revisions touch different columns
(``population_participation`` there, ``cardinality`` here) so the order is
free; this one takes the later slot.
"""
from __future__ import annotations

import logging
from collections import defaultdict

import sqlalchemy as sa
from alembic import op

from shared.semantic.join_keyword import normalise_cardinality
from shared.semantic.join_orientation_invalidation import model_ids_needing_rebuild

revision = "0191"
down_revision = "0190"
branch_labels = None
depends_on = None

_JOINS = "joins"
_CARDINALITY = "cardinality"


def _table_exists(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_column(table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    if not _table_exists(_JOINS):
        return

    bind = op.get_bind()

    if not _has_column(_JOINS, _CARDINALITY):
        op.add_column(_JOINS, sa.Column(_CARDINALITY, sa.String(32), nullable=True))

    # The conflation cannot re-enter through a raw INSERT that omits the column.
    op.alter_column(_JOINS, "join_type", server_default="inner")

    rows = bind.execute(
        sa.text(f"SELECT id, model_id, join_type FROM {_JOINS}")
    ).fetchall()
    # NOTE: deliberately no ``if not rows: return`` here. An empty live
    # ``joins`` table does NOT mean there is nothing to do: the snapshot-token
    # union below reads DEPLOYED MODEL VERSIONS, and the whole point of that
    # pass (see the module docstring) is the case where a join was edited or
    # DELETED OUTRIGHT after the artifact was built — the live graph then
    # looks clean while the stored artifact still holds pre-fix rows and
    # cannot repair itself. Short-circuiting on empty live joins would skip
    # exactly the tenants that most need staling, in the "serves wrong
    # numbers" direction. The loops below are already no-ops on an empty list.

    # (2) Backfill the cardinality that was parked in ``join_type``.
    for row in rows:
        cardinality = normalise_cardinality(row.join_type)
        if cardinality is None:
            continue
        bind.execute(
            sa.text(
                f"UPDATE {_JOINS} SET {_CARDINALITY} = :c "
                f"WHERE id = :i AND {_CARDINALITY} IS NULL"
            ),
            {"c": cardinality, "i": row.id},
        )

    # (3) Invalidate artifacts the orientation fix re-renders. The predicate is
    # derived by running the pre-fix and post-fix renderers over each stored
    # token, not by listing tokens here — see
    # ``shared/semantic/join_orientation_invalidation.py``.
    #
    # The token set is the UNION of the live join rows and every join token in
    # this tenant's stored model-version snapshots. Live rows alone are not
    # enough: an artifact's physical rows reflect the tokens in force WHEN IT
    # WAS BUILT, and a join edited after the build from an affected token
    # (``right``/``full``/a padded spelling) to an unaffected one — or deleted
    # outright — would make the live graph look clean while the stored artifact
    # still holds pre-fix rows. That artifact also cannot repair itself: the
    # definition closure refuses its refresh precisely because live and
    # snapshot now differ, so it would keep serving the pre-fix population
    # indefinitely.
    #
    # Unioning over ALL stored versions rather than resolving each model's
    # deployed pointer is deliberate. It is a strict over-approximation, and
    # invariant 6's trade is explicit: a needless rebuild costs
    # target-database time, a missed one serves wrong numbers. It also avoids
    # this one-shot migration having to reimplement the deploy-pointer
    # resolution, which is exactly the kind of second, drifting copy of a rule
    # that this whole contract exists to remove.
    token_rows: list[tuple[object, object]] = [
        (row.model_id, row.join_type) for row in rows
    ]
    if _table_exists("model_versions"):
        for version in bind.execute(
            sa.text(
                "SELECT model_id, snapshot_json FROM model_versions "
                "WHERE snapshot_unavailable IS NOT TRUE"
            )
        ).fetchall():
            snapshot = version.snapshot_json
            if not isinstance(snapshot, dict):
                continue
            for join in snapshot.get("joins") or []:
                if isinstance(join, dict):
                    token_rows.append((version.model_id, join.get("join_type")))

    affected = model_ids_needing_rebuild(token_rows)

    # Bug-8660: artifacts built against a deployed snapshot whose join_type
    # differs from the CURRENT live join_type for ANY join were built with the
    # pre-Bug-8628 renderer on the old orientation, and are now misaligned
    # with the query path — which binds against the live orientation — even
    # when both old and new tokens map to the same CTAS keyword (e.g.,
    # snapshot ``right`` -> LEFT JOIN under the legacy map, live ``left`` ->
    # LEFT JOIN under the corrected map, same keyword, but the artifact
    # rows were built with NO flip and the query path NOW flips
    # ``right``). Compare deployed-snapshot join_type per join_id to live;
    # any model with a mismatch is stale-marked.
    if _table_exists("model_versions") and _has_column("models", "deployed_version_id"):
        deployed_rows = bind.execute(
            sa.text(
                "SELECT m.id AS model_id, mv.snapshot_json "
                "FROM models m "
                "JOIN model_versions mv ON mv.id = m.deployed_version_id "
                "WHERE mv.snapshot_unavailable IS NOT TRUE"
            )
        ).fetchall()
        # Build live join_type lookup: (model_id, join_id) -> join_type
        live_by_model: dict[object, dict[str, str]] = defaultdict(dict)
        for row in rows:
            live_by_model[row.model_id][str(row.id)] = (row.join_type or "")
        for deployed in deployed_rows:
            mid = deployed.model_id
            snapshot = deployed.snapshot_json
            if not isinstance(snapshot, dict):
                continue
            live_joins = live_by_model.get(mid, {})
            for join in snapshot.get("joins") or []:
                if not isinstance(join, dict):
                    continue
                jid = str(join.get("id") or "")
                snap_type = str(join.get("join_type") or "")
                live_type = live_joins.get(jid, "")
                if jid and snap_type != live_type:
                    affected.add(mid)
                    break  # one mismatch per model is enough

    log = logging.getLogger("alembic.runtime.migration")
    if not affected:
        # Log the no-op too: this migration MUTATES rows, so "it ran and
        # decided to change nothing" is the fact an operator needs recorded.
        log.info(
            "0191 join-orientation staling: no model matched; nothing staled."
        )
        return
    model_ids = [str(m) for m in affected]

    # OPERATOR NOTE (blast radius).
    #
    # This gate is BROADER than 0199's. 0199 fires only on structurally rare
    # shapes (a zero-fact multi-table model, a cyclic join graph). This one
    # fires whenever a deployed snapshot's ``join_type`` differs from live for
    # ANY join — i.e. on every model edited but not yet redeployed. On a tenant
    # with a normal editing cadence that can be a large fraction of all models,
    # and every active aggregate and fresh pocket on them is staled in one
    # statement.
    #
    # Each staled row rebuilds on the scheduler's next sweep; until then the
    # matchers fail closed and queries fall back to SOURCE — correct numbers,
    # no acceleration, and rebuild compute on the tenant's warehouse. That is
    # the intended trade (an artifact built on the pre-Bug-8628 orientation can
    # return WRONG NUMBERS), but the counts are logged so an operator can see
    # the cost before the sweep starts and stage it if the number is large.
    staled_aggregates = 0
    staled_pockets = 0

    if _table_exists("aggregate_definitions"):
        # Mirrors ``versions._stale_incompatible_artifacts``: ``is_stale`` is
        # the aggregate matcher's own hard refusal (aggregate_matcher.py:153)
        # AND the scheduler's always-due signal, so one flag both stops the
        # artifact serving and queues its rebuild.
        staled_aggregates = bind.execute(
            sa.text(
                "UPDATE aggregate_definitions SET is_stale = true "
                "WHERE status = 'active' AND is_stale = false "
                "AND model_id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": model_ids},
        ).rowcount

    if _table_exists("pocket_definitions"):
        # The pocket matcher only considers ``status='fresh'`` rows, so 'stale'
        # makes the pocket non-servable and eligible for rebuild.
        staled_pockets = bind.execute(
            sa.text(
                "UPDATE pocket_definitions SET status = 'stale' "
                "WHERE status = 'fresh' AND retired_at IS NULL "
                "AND model_id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": model_ids},
        ).rowcount

    log.warning(
        "0191 join-orientation staling: %d model(s) matched; staled %d "
        "aggregate(s) and %d pocket(s). These rebuild on the next scheduler "
        "sweep; queries fall back to source (correct but unaccelerated) "
        "until they do.",
        len(model_ids), staled_aggregates, staled_pockets,
    )


def downgrade() -> None:
    if not _table_exists(_JOINS):
        return
    op.alter_column(_JOINS, "join_type", server_default="many_to_one")
    if _has_column(_JOINS, _CARDINALITY):
        op.drop_column(_JOINS, _CARDINALITY)
    # The staleness flags are deliberately NOT reverted: an artifact built by
    # the pre-fix builder is wrong under the fixed one and wrong again if the
    # code is rolled back to build it a third way. Leaving it stale forces one
    # rebuild under whichever builder is actually running.
