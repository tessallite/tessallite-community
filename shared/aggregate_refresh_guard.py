"""Shared refresh pending-guard lifecycle helpers (Bug-7903 / DG99-CRITICAL-01).

The full and incremental refresh jobs replace an aggregate's physical target
table DURABLY (BigQuery CREATE OR REPLACE, cross-DB staging swap, same-DB
DROP+CTAS, incremental DELETE+INSERT) while the metadata transaction — the new
``active_refresh_run_id`` + manifest hash + VERIFIED evidence — is still
uncommitted. If the aggregate stays ``active`` across that window, a derived-grain
relabel/expression serve reads the NEW physical rows under the PRIOR run's proof
(wrong numbers). The fix is a UNIFORM pending-guard: snapshot the pre-refresh
status durably, flip ``active`` -> ``pending`` and COMMIT that flip BEFORE any
physical change, then restore to the durable prior status AFTER the new run +
manifest + evidence commit. ``pending`` is non-servable on every target (the
router binder loads ``status=="active"`` only; derived trust rule 5 +
build_serve_proof liveness gate reject a non-active artifact).

This module is the SINGLE home for that decision logic so the full and
incremental jobs cannot drift (Fable R1 #4). It performs no DDL; the caller owns
the physical build and the surrounding transaction/commit.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import AggregateDefinition, AggregateRefreshRun

# Statuses a servable aggregate can hold before a refresh and be faithfully
# restored to afterwards. "active" serves; "disabled" is a user-owned
# non-serving state that a rebuild must PRESERVE (never silently re-activate).
_RESTORABLE_PRIOR = ("active", "disabled")

# Terminal, USER-owned states. If a user moves an aggregate here out-of-band
# DURING a refresh, the restore must honour it rather than clobber it back to a
# scheduler-computed status (Fable R2 #2/#6 lost-update guard).
_USER_TERMINAL = ("disabled", "retired")


@dataclass
class PendingGuardDecision:
    """Outcome of :func:`enter_refresh_pending`.

    ``prior_status`` is the status the success path restores to. ``flipped`` is
    True when this run owns the pending window (a fresh flip OR a recovery of an
    already-non-servable aggregate whose durable snapshot is authoritative).
    ``needs_commit`` is True only when the caller must COMMIT the flip before the
    physical change (a fresh active->pending flip); a recovery needs no re-flip.
    """
    prior_status: str
    flipped: bool
    needs_commit: bool


def decide_refresh_pending(agg_def: AggregateDefinition) -> PendingGuardDecision:
    """Decide the pending-guard entry transition for one aggregate.

    A DURABLE recorded prior status (``refresh_prior_status``) ALWAYS wins — it
    survived a crash, an import-forced pending, or the early invalid->active
    self-heal — so a "disabled" aggregate rebuilt through any path is restored to
    DISABLED, never re-activated. Only a genuinely servable prior
    ("active"/"disabled") is honoured; anything else is a fresh refresh of a
    currently-servable aggregate (flip active) or a non-servable entry left
    untouched (invalid self-heal / out-of-band state).

    The caller applies the transition (sets ``status``/``refresh_prior_status``
    and commits when ``needs_commit``) — this function is pure.
    """
    recorded = getattr(agg_def, "refresh_prior_status", None)
    if recorded in _RESTORABLE_PRIOR:
        # Recovery: the durable snapshot is authoritative. A currently-active
        # aggregate (e.g. after the early self-heal cleared an invalid flag) still
        # needs the flip+commit so it is non-servable through the physical change;
        # an already non-servable one (pending/invalid) does not.
        return PendingGuardDecision(
            prior_status=recorded,
            flipped=True,
            needs_commit=(agg_def.status == "active"),
        )
    if agg_def.status == "active":
        return PendingGuardDecision(
            prior_status="active", flipped=True, needs_commit=True
        )
    # Not servable at entry and no durable snapshot: leave untouched; the legacy
    # invalid->active self-heal applies on success.
    return PendingGuardDecision(
        prior_status=agg_def.status, flipped=False, needs_commit=False
    )


async def resolve_success_restore_status(
    db: AsyncSession,
    agg_def: AggregateDefinition,
    decision: PendingGuardDecision,
    *,
    storage_binding_matches: bool = True,
    source_binding_matches: bool = True,
) -> Optional[str]:
    """Resolve the status a SUCCESSFUL refresh should restore the aggregate to.

    Returns the status to set, or ``None`` to leave the status unchanged.

    Honours an OUT-OF-BAND user disable/retire committed during the (possibly
    minutes-long) refresh window (Fable R2 #2/#6): the scheduler's in-memory
    ``agg_def.status`` is stale, so we re-read the COMMITTED status from the DB. If
    the user moved the aggregate to a terminal user-owned state
    ("disabled"/"retired") while the refresh ran, that wins — we never clobber a
    user's committed disable/retire back to "active". A successful physical
    build whose build-start storage binding no longer matches the live target
    routing stays ``pending`` (non-serving) so the next sweep rebuilds it at the
    new location; it can never self-heal or restore to ``active``. The SOURCE
    binding (``source_binding_matches``, Bug-8602) is treated identically and
    ANDed with it here rather than in each caller, so the two refresh writers
    cannot drift on which bindings gate a restore — this module is the single
    home for that decision. Otherwise:
      - a guard-owned run restores the DURABLE prior status ("active"/"disabled");
      - a legacy non-servable entry (invalid/pending with no durable prior)
        self-heals to "active" (Bug-7131).
    The caller clears ``refresh_prior_status`` when this returns non-None.
    """
    committed_status = await _read_committed_status(db, agg_def.id)
    # Row vanished (deleted mid-refresh): leave the status unchanged — do not
    # resurrect a deleted aggregate (Fable R2 #4: align code with the contract).
    if committed_status is None:
        return None
    # An out-of-band user disable/retire committed during the window is
    # authoritative — never clobber it back to active.
    if committed_status in _USER_TERMINAL:
        return committed_status

    # Bug-8481 R1: a control-plane invalidator from the first implementation
    # could change a refresh-owned pending row while its durable pre-refresh
    # status still recorded ``disabled``. That durable user choice remains
    # authoritative even when the storage proof fails: disabled + stale is safe
    # and must not be replaced by a rebuildable ``pending`` state that a later
    # sweep can restore to active.
    if decision.flipped and decision.prior_status == "disabled":
        return "disabled"

    # Bug-8481: a target/connection re-point that landed during CTAS means the
    # physical rows were written to the previous database. Even though the DDL
    # succeeded, restoring ACTIVE would make the aggregate's same-named table
    # resolve on the new database and can expose rows outside the RLS-admitted
    # population. Keep it pending for a rebuild; ``is_stale`` is set by the
    # caller in the same completion transaction.
    #
    # Bug-8602: a SOURCE re-point during the same window is the mirror failure —
    # the DDL succeeded, but the rows it wrote were READ FROM a database the
    # model no longer reads, so they are not the deployed definition's numbers.
    # Same verdict, same reason: non-serving until a rebuild.
    if not (storage_binding_matches and source_binding_matches):
        return "pending"

    if decision.flipped and agg_def.status in ("pending", "invalid"):
        prior = decision.prior_status
        return prior if prior in _RESTORABLE_PRIOR else "active"
    if agg_def.status in ("invalid", "pending"):
        # Legacy self-heal path (no durable prior).
        return "active"
    return None


async def recover_session_after_refresh_failure(
    db: AsyncSession,
    *,
    run_id: object,
    agg_def_id: object,
) -> tuple[Optional[AggregateRefreshRun], Optional[AggregateDefinition]]:
    """Roll back an aborted refresh transaction, then re-load the run + aggregate
    rows by PRIMARY KEY so the failure-persistence writes run on a CLEAN session.

    Bug-8774 / Bug-9415 (F-009-06): when the exception that ends a refresh is a
    database DRIVER error (a deadlock, a lost connection, a constraint violation),
    the metadata session is left ABORTED. Every subsequent write in the except
    handler — the FAILED run status, ``is_stale``, the ``record_alert`` INSERT,
    the ``dispatch_alert`` SELECT and the terminal commit — is then silently lost
    or raises ``PendingRollbackError``: the run stays "running", ``is_stale`` is
    never set, no alert fires. The failure becomes INVISIBLE.

    Rolling back ends the aborted transaction so the failure state can be
    committed. A rollback, however, EXPIRES every ORM instance in the session, so
    reading ``run.id`` / ``agg_def.status`` afterwards would trigger a lazy
    attribute load that raises ``MissingGreenlet`` under async. Re-fetching by the
    caller's CACHED primary keys returns freshly-loaded instances the caller can
    write and commit without touching an expired attribute.

    The pending-guard's earlier ``status=="pending"`` flip and the "running" run
    row were both COMMITTED in a PRIOR transaction (``aggregate_refresh_guard``
    line ~887 / the Bug-8822 run-row commit), so this rollback never undoes them:
    the re-fetched aggregate carries the durable guard state and the failure branch
    applies the terminal non-servable status on top of it. Either row may be
    ``None`` if it was deleted mid-refresh (a model/aggregate delete that raced the
    build); the caller persists whatever survived and lets the delete stand.
    """
    await db.rollback()
    run = await db.get(AggregateRefreshRun, run_id) if run_id is not None else None
    agg_def = (
        await db.get(AggregateDefinition, agg_def_id)
        if agg_def_id is not None
        else None
    )
    return run, agg_def


async def _read_committed_status(db: AsyncSession, agg_id: object) -> Optional[str]:
    """Read the COMMITTED status of the aggregate row from the DB.

    A scalar SELECT of the column issues a fresh query (it does not return the
    session identity-map copy), so the scheduler's long-lived session sees a
    concurrent COMMITTED change — a user disable/retire that landed during the
    refresh window — rather than its own stale in-memory ``agg_def.status``.
    Returns None if the row vanished (deleted mid-refresh) — the caller then
    leaves the status unchanged and lets the delete stand.
    """
    result = await db.execute(
        select(AggregateDefinition.status).where(
            AggregateDefinition.id == agg_id
        )
    )
    return result.scalar_one_or_none()
