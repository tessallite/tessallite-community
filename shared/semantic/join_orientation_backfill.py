"""Backfill policy for joins whose ``join_type`` never declared an orientation.

Contract: ``docs/architecture/architecture_join-orientation-and-cardinality.md``
(invariants 1-4) and the decision recorded in
``docs/questions/questions_pocket-join-population.md`` ("RESOLVED 2026-08-04 —
user picks (iii), broadly").

Why this module exists
----------------------
Migration ``0191`` split ``Join.join_type`` (orientation) from
``Join.cardinality`` (fan-out) and backfilled the NEW column, but deliberately
left every existing ``join_type`` value untouched. The value is mirrored
verbatim into every deployed model-version snapshot, and
``shared/definition_closure.py`` diffs the live graph against that snapshot
before allowing any aggregate/pocket refresh — so rewriting one side alone
would make ``joins.join_type`` disagree and refuse EVERY refresh on the model
until a human redeployed it.

The cost of leaving it is that ``routing/pocket_population.py`` refuses to
prove row population for any plan containing an undeclared edge
(``is_orientation_declared`` is False), so pockets cannot accelerate a legacy
model at all. Migration ``0194`` closes it by doing BOTH halves in one
transaction: the live rewrite AND a forced republish of the deployed snapshot.

This module owns the pure half — WHICH rows are rewritten, to WHAT, and how
the deployed snapshot is patched to match — so the migration is I/O only and
the policy is unit-testable without a database. It mirrors how ``0191`` and
``0192`` consume ``join_orientation_invalidation``.

The target set is DERIVED, not restated
---------------------------------------
The obvious implementation is ``WHERE join_type = 'many_to_one'``. That token
list is the exact enumeration-blind-spot class this repo has been bitten by
repeatedly (see ``join_orientation_invalidation``'s module docstring). The
target set here is instead defined as ``not is_orientation_declared(token)`` —
the SAME predicate ``pocket_population`` uses to refuse a plan, and therefore
by construction exactly the rows whose refusal this migration exists to lift.
Adding a spelling to the orientation vocabulary shrinks this set
automatically; adding a cardinality spelling grows it automatically.

The inferred value
------------------
* A recognised CARDINALITY token routes through ``split_join_token``, i.e. the
  identical inference the ``0191`` ``cardinality`` backfill already applied:
  the many side is the preserved side (``many_to_one`` -> ``left``,
  ``one_to_many`` -> ``right``, ``one_to_one``/``many_to_many`` -> ``left``).
* Anything else unrecognised (``outer``, ``''``, an importer's private label)
  has no cardinality to infer from, so it takes
  :data:`UNRECOGNISED_TOKEN_JOIN_TYPE` — ``left``, which is precisely what
  ``join_keyword`` already coerces an unrecognised token to.

Blast radius, stated exactly rather than approximately
------------------------------------------------------
The point of the backfill is anchor-INDEPENDENCE: a declared token preserves
the same physical relation whichever end the compiler's traversal reached
first, and a legacy token does not (it preserves whatever was accumulated
first, so two plans over the same model preserve opposite sides — the actual
defect). Making an edge explicit therefore necessarily changes the rendering
in whichever traversal direction the legacy behaviour was the anchor-dependent
one:

* ``many_to_one``, ``one_to_one``, ``many_to_many`` and every unrecognised
  token become ``left``. The UN-FLIPPED rendering is byte-identical before and
  after (``LEFT JOIN``); only the flipped traversal moves. A conventionally
  drawn star schema traversed from the fact therefore does not move at all,
  which is why the acme-demo seed measured zero served-number change across
  all 20 of its legacy edges.
* ``one_to_many`` becomes ``right``, and that is the ONE token whose un-flipped
  rendering ALSO changes (``LEFT JOIN`` -> ``RIGHT JOIN``). It is not an
  oversight: on such an edge the modeller's RIGHT table is the many side, so
  the legacy un-flipped rendering was preserving the ONE side — exactly the
  anchor-dependent reading being corrected. ``test_join_orientation_backfill``
  pins this as the only token in that class, so any future edit that widens
  the set fails rather than silently moving more numbers.

Both are the accepted one-time correction the decision above signs off.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from shared.semantic.join_keyword import (
    is_orientation_declared,
    normalise_cardinality,
    normalise_token,
    split_join_token,
)

logger = logging.getLogger(__name__)

#: The orientation an unrecognised, non-cardinality token becomes. ``left`` is
#: not a guess: ``join_keyword`` already renders every unrecognised token as an
#: un-flipped ``LEFT JOIN`` (invariant 4), so this is the only value that
#: leaves the un-flipped rendering unchanged.
UNRECOGNISED_TOKEN_JOIN_TYPE = "left"


def resolves_no_fan_out(stored: Any) -> bool:
    """True when a stored ``cardinality`` value carries no usable fan-out.

    The ONE predicate both halves of the migration must select on. "No fan-out"
    is NOT "is null": ``rehydrator._insert_joins`` writes an imported bundle's
    ``cardinality`` verbatim — it is the only field on that insert with no
    coercion, while ``population_participation`` twelve lines above IS coerced
    for precisely this threat model ("a hand-edited or tampered bundle, which
    bypasses the API's Literal validation") — and the import route validates
    only ``schema_version``. So a NON-NULL string the vocabulary rejects is a
    producible stored value, and for such a row ``edge_cardinality`` falls
    THROUGH it to the legacy ``join_type`` token. Rewriting the token while
    leaving the unusable value in place therefore drops the fan-out to None on
    both sides at once: no drift is reported (the two sides still agree on the
    junk string) and ``field_compatibility``'s many-to-many guard stops
    refusing a fanning path, which double-counts silently.

    Testing storage state (``IS NULL``) rather than semantic state is the
    defect this contract has now hit at four widths — key-presence, null, and
    now unrecognised-non-null. Both the SQL live update and the snapshot patch
    bind their decision to THIS function so they cannot select differently.
    """
    return normalise_cardinality(stored) is None


@dataclass(frozen=True)
class JoinBackfill:
    """One join row's rewrite.

    ``new_cardinality`` is the fan-out the legacy token encoded, or ``None``
    when it encoded none. It is applied to the live row ONLY where
    ``Join.cardinality`` is still NULL, completing for post-``0191`` arrivals
    exactly what ``0191`` did for the rows that existed when it ran. Without
    it, a join inserted between the two migrations (a pre-``0191`` bundle
    imported after it — Bug-8698) would end up with the fan-out written into
    the deployed snapshot but not into the live row, and the closure would
    report drift on ``cardinality``.
    """

    join_id: str
    model_id: str
    old_token: Any
    new_token: str
    new_cardinality: str | None = None


def backfill_orientation(raw: Any) -> str | None:
    """The orientation token ``raw`` should become, or ``None`` to leave it.

    ``None`` means the row already declares an orientation — including a long
    spelling such as ``left_outer``. Canonicalising those is a DIFFERENT
    change (it would rewrite rows whose orientation is not in question, and
    move their snapshot values for no correctness gain), so this returns
    ``None`` for them deliberately.
    """
    if is_orientation_declared(raw):
        return None
    inferred, _cardinality = split_join_token(raw)
    return inferred or UNRECOGNISED_TOKEN_JOIN_TYPE


def plan_backfills(
    join_rows: Iterable[tuple[Any, Any, Any]],
) -> list[JoinBackfill]:
    """Given ``(join_id, model_id, join_type)`` rows, the rewrites to apply.

    Pure over rows the caller has already read, so the migration and its tests
    exercise the same policy. Rows that already declare an orientation are
    absent from the result, which is what makes the migration idempotent: a
    second run finds nothing to do.
    """
    plans: list[JoinBackfill] = []
    for join_id, model_id, join_type in join_rows:
        new_token = backfill_orientation(join_type)
        if new_token is None:
            continue
        plans.append(
            JoinBackfill(
                join_id=str(join_id),
                model_id=str(model_id),
                old_token=join_type,
                new_token=new_token,
                new_cardinality=split_join_token(join_type)[1],
            )
        )
    return plans


def patch_snapshot_join_types(
    snapshot: Mapping[str, Any],
    backfills_by_join_id: Mapping[str, JoinBackfill],
) -> tuple[dict[str, Any], int]:
    """Return ``(patched_copy, patched_count)`` for a deployed snapshot.

    A snapshot join is patched when it still AGREES with the live row's
    pre-backfill token, where "agrees" has two arms:

    * the stored token normalises equal to the live pre-backfill token; or
    * the snapshot join carries NO ``join_type`` key at all.

    The second arm is not leniency, it is the one case
    ``definition_closure`` cannot police. ``_compare_group`` iterates
    ``for fname in sorted(d_row)`` — only the fields the DEPLOYED row carries
    — so a missing ``join_type`` is never compared and reports no drift, ever.
    Hand-authored, imported and legacy bundles routinely omit fields (the
    shipped acme-demo snapshot joins carry no ``cardinality`` and no
    ``population_participation``). Skipping such a join would leave live on a
    flip-aware ``left`` while the router hydrates the snapshot to
    ``join_type=None`` and renders an un-flipped ``LEFT JOIN`` in BOTH
    directions — a live/deployed divergence this migration CREATED, invisible
    to the guard that exists to catch exactly that, and therefore a silently
    wrong artifact rather than a refused refresh. Before the backfill the two
    agreed (a legacy token also renders un-flipped ``LEFT JOIN`` both ways), so
    writing the key is what PRESERVES the agreement.

    A snapshot join carrying a DIFFERENT token is left alone. The model already
    has un-deployed join edits and the closure already refuses its refreshes;
    overwriting the snapshot value would silently change what the query-router
    BINDS (the router renders from the snapshot, not from the live graph) — a
    served-number change on a token this migration is not touching in the same
    direction. That is not a correction, it is a different edit, and it stays
    visible as the pre-existing drift it is.

    Comparison is on ``normalise_token`` so a padded/case-different spelling of
    the same token still counts as agreement.

    A patched join also gains an explicit ``cardinality`` when the snapshot
    does not already RESOLVE one (key absent, or present and null — see the
    inline comment for why "present and null" is the common case, not the
    exotic one): the legacy token was that snapshot's only carrier of fan-out,
    and dropping it makes the many-to-many compatibility guard fail OPEN.

    The value is derived from the SNAPSHOT'S OWN token, never from the live
    row, so no un-deployed edit is published. In the agreeing-token branch the
    two derivations are identical by construction, which is what keeps live and
    deployed equal on ``cardinality``. In the missing-``join_type``-key branch
    the snapshot encoded no fan-out and none is invented; that branch cannot
    also carry a ``cardinality`` key, because the serialiser emits both columns
    or neither, so it cannot produce a disagreement.

    One consequence is deliberate: on a model
    where a modeller set ``Join.cardinality`` alone (the PATCH route allows it
    without touching ``join_type``) and never redeployed, the newly written
    snapshot value will DISAGREE with live and ``definition_closure`` will
    refuse that model's refreshes until it is redeployed. That divergence is
    real and pre-existing — the two sides already described different fan-outs
    to different consumers — and it was only invisible because the field was
    absent from the comparison. Surfacing it fails CLOSED; leaving it hidden
    fails open into a wrong number, which is not a trade this contract makes.

    The input mapping is never mutated: the caller holds the row exactly as
    stored, and an aborted transaction must not leave a half-patched object
    behind. Only the containers that actually change are copied.
    """
    joins = snapshot.get("joins")
    if not isinstance(joins, list):
        return dict(snapshot), 0

    patched_joins: list[Any] = []
    patched = 0
    for join in joins:
        if not isinstance(join, Mapping):
            patched_joins.append(join)
            continue
        plan = backfills_by_join_id.get(str(join.get("id")))
        if plan is None:
            patched_joins.append(join)
            continue
        if not all(
            v is None or isinstance(v, str)
            for v in (join.get("join_type"), join.get("cardinality"))
        ):
            # A non-string value here is not readable by ``normalise_token``,
            # which does ``(value or "").strip()`` and raises AttributeError on
            # an int/list/dict. Such a snapshot is reachable:
            # ``rehydrator._restore_version_history`` persists a
            # schema_version>=2 bundle's own ``snapshot_json`` after PK
            # re-keying only, and ``_validate_snapshot_for_deploy`` never
            # inspects join fields. The model is already broken at query time
            # (the renderer raises identically), but letting it raise HERE
            # would abort the whole tenant's ``alembic upgrade`` mid-deploy and
            # take every other model down with it. "Leave alone what you cannot
            # read" is already this function's policy for a disagreeing token.
            logger.warning(
                "join %s carries a non-string join_type/cardinality in the "
                "deployed snapshot (%r/%r); leaving it unpatched.",
                join.get("id"),
                join.get("join_type"),
                join.get("cardinality"),
            )
            patched_joins.append(join)
            continue
        if "join_type" in join and normalise_token(
            join.get("join_type")
        ) != normalise_token(plan.old_token):
            patched_joins.append(join)
            continue
        new_join = dict(join)
        # ``resolves_no_fan_out`` and NOT ``"cardinality" not in new_join``,
        # and not ``is None`` either. The key is
        # almost always PRESENT: ``row_to_snapshot_dict`` emits every ORM
        # column, so any snapshot serialised after ``0191`` added the column
        # carries ``"cardinality": null`` for a row whose live value is NULL.
        # A key-presence test therefore skips exactly the shape the live write
        # in the migration exists for (a join that arrived AFTER 0191 with a
        # legacy token — an imported pre-0194 bundle, or a revert to a
        # pre-0191 version, then deployed): live would gain the fan-out and
        # the snapshot would not, which is a ``cardinality`` drift refusing
        # every refresh AND, for a ``many_to_many`` edge, the same fail-open
        # many-to-many guard. This predicate is byte-symmetric with the
        # migration's own ``CASE WHEN cardinality IS NULL`` live guard, so the
        # two sides cannot select differently.
        if resolves_no_fan_out(new_join.get("cardinality")):
            # The legacy token is this SNAPSHOT'S ONLY carrier of fan-out.
            # ``0191`` backfilled ``joins.cardinality`` on the LIVE rows only —
            # its own docstring says a column added after a snapshot was
            # written is invisible to that snapshot by design — so a snapshot
            # join still holding a cardinality token predates the split and has
            # no ``cardinality`` key. Rewriting ``join_type`` without
            # re-encoding it drops ``edge_cardinality`` to None for every
            # SNAPSHOT consumer, and the router binds the snapshot: the
            # many-to-many guard in ``semantic/field_compatibility.py`` then
            # stops refusing a fanning path and offers the measure/dimension
            # pair as compatible, which double-counts silently. The
            # drill-through path classifier degrades to ``mixed`` for the same
            # reason.
            #
            # Derived from the SNAPSHOT's own token, never from the live row:
            # the live value is what the deploy pointer has not published yet,
            # and copying it here would publish an un-deployed edit.
            _, snapshot_cardinality = split_join_token(join.get("join_type"))
            if snapshot_cardinality is not None:
                new_join["cardinality"] = snapshot_cardinality
            elif "cardinality" in join:
                # The snapshot's own token encodes no fan-out (it is the
                # missing-``join_type``-key branch, or an unrecognised token),
                # but the KEY IS PRESENT, so ``_compare_group`` compares it
                # against live — and the live row is gaining the plan's fan-out
                # in the same transaction. Moving one without the other would
                # create a ``cardinality`` drift on a model that had none,
                # refusing every refresh: the unattended regression 0191
                # stopped short to avoid. A snapshot join with no key at all
                # still gains nothing, because nothing compares it.
                new_join["cardinality"] = plan.new_cardinality
        new_join["join_type"] = plan.new_token
        patched_joins.append(new_join)
        patched += 1

    if patched == 0:
        return dict(snapshot), 0
    out = dict(snapshot)
    out["joins"] = patched_joins
    return out, patched
