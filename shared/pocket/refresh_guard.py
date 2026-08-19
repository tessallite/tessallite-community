"""Pocket finalisation guard (Bug-8807) — the pocket half of one protocol.

Why this module exists
----------------------
Three artifact finalisation writers already implement ONE protocol, established
by Bug-8481/Bug-8602 over several review rounds and two live-reproduced
deadlocks: ``scheduler/src/jobs/full_refresh.py``,
``scheduler/src/jobs/incremental_refresh.py`` and
``optimizer/src/lifecycle/creator.py``. Each of them, after the physical build
and BEFORE dirtying the artifact row, takes every control-plane row lock in one
fixed order, re-reads the artifact's COMMITTED status, and re-proves the target
and source routing identities it built through — and only then decides whether
the artifact may return to a serving status.

``shared/pocket/refresh.py`` never joined that protocol. It committed
``status="invalidating"`` before the build and then wrote ``status="fresh"``
UNCONDITIONALLY at completion. Every control-plane invalidator writes
``status="stale"`` on the same row (``shared/artifact_target_binding.py::
_invalidate_artifacts``), so an invalidation that landed mid-build was silently
undone by the completion write, and the pocket went back into the serving pool
holding rows read from the previous database (Bug-8807; it is also what raised
the severity of Bug-8780).

That is a defect of the WRITER's protocol, not of any single line, so the fix is
to give the pocket writer the same three legs the other three already have.

Exclusion, not detection
------------------------
The property — "an invalidation that lands during a build is never undone by
that build" — is an exclusion property, so it needs a LOCK, not a comparison. A
plain non-locking re-read of the status is a lost update: the invalidator's
UPDATE can be issued and uncommitted while the re-read still observes
``invalidating``; the completion write then queues behind the invalidator's row
lock and overwrites ``stale`` with ``fresh`` the moment it commits. Reading the
status ``FOR UPDATE`` is what turns that into a plain wait in both directions:

* an invalidation that COMMITTED first is observed here and honoured;
* one that starts later blocks on this row lock until the refresh commits, and
  then applies its ``stale`` on top — the artifact ends non-serving either way.

No new column is needed and none was added. An epoch/generation counter would be
a second, weaker encoding of what the row lock already provides, and it would
have to be bumped by every one of the seven independent stalers to work.

The three legs, and why each is load-bearing
--------------------------------------------
1. :func:`~shared.artifact_target_binding.lock_finalization_rows` — the FIXED
   lock order every control-plane writer agrees with. Each writer locks ONE
   control-plane row (``project_connections`` / ``data_targets`` /
   ``data_sources``) and then UPDATEs the artifacts hanging off it, so acquiring
   those rows AFTER the pocket row would close a cycle with all of them.
2. The committed-status re-read under ``FOR UPDATE`` — the GENERIC leg. Pockets
   have seven independent stalers (routing invalidation, deploy/version
   staling, schema drift, definition edit, snapshot revert, TTL/event sweep,
   query-time missing table). This leg honours all of them, and any future one,
   without enumerating any.
3. The target + source binding re-proofs — the leg the status re-read cannot
   cover. A re-point that lands BEFORE the refresh commits ``invalidating`` is
   clobbered by that commit, and the build can still read a pre-repoint copy of
   the connection out of the session identity map (tenant sessions are
   ``expire_on_commit=False`` and the sweep reuses one session), so the rows can
   come from the old database with nothing left in the status to say so.

The refusal outcome is byte-identical to what ``_invalidate_artifacts`` writes
for a pocket — ``status="stale"``, a reason, no row manifest, no liveness
pointer — so the refresh sweep rebuilds it and the query-router refuses it.

What this does NOT hold locks across
------------------------------------
The locks are taken AFTER the physical materialisation returns, so a
control-plane edit never waits out the build itself (minutes). It can wait only
for the metadata tail — the manifest catalogue read-back and the commit — which
is the same window the aggregate writers already hold. An operator's
``PATCH /sources/{id}`` landing in that tail blocks briefly and then applies its
invalidation on top of a now-visible pocket; that ordering is the point, not a
side effect.

The OTHER window, and why it needs the same read (Bug-8827)
-----------------------------------------------------------
The refresh takes ``pocket_refresh_lock``, re-reads the pocket, validates
through the query-router (an HTTP round trip) and resolves its connections
BEFORE it commits ``status="invalidating"`` — and that commit is a second
unconditional clobber of exactly the same kind. It is the WORSE of the two,
because the value it erases is the only input
``should_refresh_incrementally`` consumes: a staler committing in that window
is deleted, the incremental gate still sees the pre-lock ``fresh``, and the
delta leg patches only the look-back window over rows the PREVIOUS definition
produced. The table published ``fresh`` is then physically mixed — Bug-8431's
defect shape through a different door, and a live wrong number rather than a
stale one.

``read_committed_pocket_status`` is therefore called TWICE by the writer: once
immediately before the ``invalidating`` write, whose result overrides the
pre-lock capture as the authoritative ``entry_status`` (so a staler observed
there withdraws the incremental shortcut and forces a FULL rebuild), and once
at finalisation as leg 2 above. A full rebuild from the current definition
legitimately clears staleness, so the first one refuses only the shortcut, never
the build. Note this is NOT what a fourth "definition re-proof" leg would do:
at finalisation the live definition IS the new one and the delta slice did use
it, so a build-start-vs-live comparison would match and admit the mixed table.

Stated boundary: a pocket RETIRED mid-build sets only ``retired_at``, not the
status, so this guard sees its own ``invalidating`` and admits the completion.
That is harmless — the matcher and the eviction janitor both key on
``retired_at IS NOT NULL``, so a retired pocket is never served regardless of
status — and deliberately not folded in here, because ``retired_at`` is not a
staleness signal and treating it as one would conflate two lifecycles.

Known, deliberate limit: a build that FAILS writes ``status="failed"`` from the
exception handler without consulting this guard, so an invalidation that landed
mid-build has its reason text replaced by the build error. Both states are
non-serving and the next sweep rewrites ``failed`` to ``stale`` and rebuilds, so
there is no exposure — only a less specific operator message on a path that
already failed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# The one status the pocket matcher serves. Every other status (stale / failed /
# invalidating) is a refusal signal — the pocket analogue of an aggregate's
# ``is_stale`` flag.
POCKET_STATUS_FRESH = "fresh"

# The status a refusal lands on: the established "rebuild me" state, and exactly
# what the control-plane invalidator writes.
POCKET_STATUS_STALE = "stale"

# The status ``refresh_pocket_definition`` commits before the physical build.
# Observing it again at finalisation is what proves nothing else wrote the row.
POCKET_STATUS_INVALIDATING = "invalidating"

_REASON_INVALIDATED = (
    "This cache was invalidated while it was being rebuilt, so the rows it just "
    "wrote are not provably current. It must be rebuilt again before it can "
    "serve."
)
_REASON_ROW_GONE = (
    "This cache could not be confirmed at the end of its rebuild, so it must be "
    "rebuilt before it can serve."
)
_REASON_TARGET_MOVED = (
    "The database this cache writes to changed while it was being rebuilt, so "
    "the rows were written to a different database. It must be rebuilt before "
    "it can serve."
)
_REASON_SOURCE_MOVED = (
    "The database this cache reads from changed while it was being rebuilt, so "
    "the rows came from a database this model no longer reads. It must be "
    "rebuilt before it can serve."
)


@dataclass(frozen=True)
class PocketFinalizationState:
    """Committed truth read under the finalisation locks.

    Plain scalars, deliberately: they are read once, under lock, and must not be
    re-derivable from an ORM object that a later flush or expiry could move.

    ``committed_status`` is ``None`` when the pocket row no longer exists.
    """

    committed_status: str | None
    target_binding_matches: bool
    source_binding_matches: bool


def resolve_pocket_serving_refusal(
    state: PocketFinalizationState,
    *,
    own_status: str = POCKET_STATUS_INVALIDATING,
) -> str | None:
    """``None`` when the pocket may return to ``fresh``, else the refusal reason.

    Pure: every input is already-read committed truth, so the decision can be
    tested without a database and cannot drift from what the reader observed.

    ``own_status`` has no non-default caller and is deliberately kept: with
    ``read_committed_pocket_status``'s ``lock_for_finalization`` flag it is the
    MUTATION SURFACE the guard's own tests flip to prove each leg is
    load-bearing. Do not delete either as a dead parameter.

    ``own_status`` is the status THIS refresh committed before the build. Seeing
    anything else — including ``None`` for a deleted row — means another writer
    owned the row during the build, and this build's completion must not speak
    for it. Fail closed: an unproven pocket is never returned to the serving
    pool.
    """
    if state.committed_status is None:
        return _REASON_ROW_GONE
    if state.committed_status != own_status:
        return _REASON_INVALIDATED
    if not state.target_binding_matches:
        return _REASON_TARGET_MOVED
    if not state.source_binding_matches:
        return _REASON_SOURCE_MOVED
    return None


async def read_committed_pocket_status(
    db: AsyncSession, pocket_id: Any, *, lock_for_finalization: bool = True,
) -> str | None:
    """The pocket's COMMITTED status, by default under a row lock.

    A scalar SELECT of the column issues a real query rather than returning the
    session's identity-map copy, so a long-lived scheduler session observes a
    concurrent COMMITTED change instead of its own stale in-memory attribute.

    ``FOR UPDATE`` is what makes the observation ACTIONABLE rather than merely
    informative — see the module docstring. Autoflush is suppressed for the same
    reason ``current_target_build_binding`` suppresses it: a finalisation path
    holds dirty artifact metadata, and flushing it before taking a control-plane
    lock can deadlock against a writer that already owns that row.

    Returns ``None`` when the row does not exist.
    """
    from shared.db.models import PocketDefinition

    with db.no_autoflush:
        stmt = select(PocketDefinition.status).where(
            PocketDefinition.id == pocket_id
        )
        if lock_for_finalization:
            stmt = stmt.with_for_update()
        return (await db.execute(stmt)).scalar_one_or_none()


async def read_pocket_finalization_state(
    db: AsyncSession,
    *,
    pocket_id: Any,
    target_binding: Any,
    source_binding: Any,
    connection_ids: Sequence[Any] = (),
    target_id: Any = None,
    model_id: Any = None,
) -> PocketFinalizationState:
    """Take the finalisation locks and read every committed input, in order.

    ORDER IS LOAD-BEARING and matches the three aggregate writers exactly:

    1. every control-plane row, in the one fixed order
       (:func:`~shared.artifact_target_binding.lock_finalization_rows`);
    2. the pocket row itself, ``FOR UPDATE``;
    3. the two binding re-proofs, which re-acquire locks this transaction
       already holds (a no-op) and compare against committed state.

    The caller MUST NOT have dirtied the pocket row before calling this, or step
    1 acquires control-plane rows after the pocket row and closes a deadlock
    cycle with every control-plane writer.

    Both re-proofs fail CLOSED on any error resolving live state (they log and
    return False), so an unreadable endpoint costs a rebuild, never a wrong
    number.
    """
    from shared.artifact_target_binding import (
        lock_finalization_rows,
        source_build_binding_matches_live,
        target_build_binding_matches_live,
    )

    await lock_finalization_rows(
        db,
        connection_ids=connection_ids,
        target_id=target_id,
        model_id=model_id,
    )
    committed_status = await read_committed_pocket_status(
        db, pocket_id, lock_for_finalization=True
    )
    target_matches = await target_build_binding_matches_live(
        db, target_binding, lock_for_finalization=True,
    )
    source_matches = await source_build_binding_matches_live(
        db, source_binding, lock_for_finalization=True,
    )
    return PocketFinalizationState(
        committed_status=committed_status,
        target_binding_matches=bool(target_matches),
        source_binding_matches=bool(source_matches),
    )
