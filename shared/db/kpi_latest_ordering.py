"""Canonical write-ordering guard for ``kpi_latest`` upserts (Bug-7982).

Two independent writers can target the same ``(model_id, kpi_id)`` row: the
model-service ``evaluate-batch`` handler (``api/kpi_latest.py``) and the
scheduler snapshot sweep (``jobs/sweep.py``), plus the post-deploy re-eval
trigger which drives the former. They MUST order their writes identically, or a
stale value can overwrite a fresher one. This module is the single source of that
ordering so the two upsert helpers cannot drift (the R3 round shipped two copies
of the guard; R6 finding 1 showed the copy that only compared epoch was wrong).

Ordering key: the tuple ``(evaluated_for_epoch, eval_generation)``.

- ACROSS epochs: a write for an OLDER epoch never regresses a newer published
  row (the post-deploy availability race — a slow evaluation started under the
  previous epoch must not clobber a fresh row published under the new one).
- WITHIN one epoch: a later-STARTING evaluation is authoritative. An
  earlier-starting evaluation that merely COMMITS later (having read staler
  source data) must not overwrite it.

Why ``eval_generation`` and not ``eval_started_at`` (R7 finding 2)
-----------------------------------------------------------------
R6 ordered within an epoch by ``eval_started_at``, sourced from
``clock_timestamp()``. That is a real-time clock, not a sequence: the external
gate sampled it 100k times on this project's own Postgres and got 82,242
DUPLICATE values. Under a tie the ``<=`` comparison admits BOTH writers, so
whichever COMMITS last wins — precisely the last-commit-wins defect this guard
exists to close, merely made rarer.

The ordering token is therefore a strictly-increasing, DB-issued generation
allocated from ``kpi_eval_generation_seq`` (``shared/db/kpi_eval_generation.py``)
once, when an evaluation STARTS, before any source read. ``nextval`` is atomic
and never repeats, so:

* two DISTINCT evaluations can never tie — the ordering is total;
* an equal generation means the two writes belong to the SAME logical
  evaluation (the sweep threads its generation into the ``evaluate-batch`` call
  so the handler's publish and the sweep's own write share one token), which is
  exactly the case ``<=`` must admit so neither falsely suppresses the other.

``eval_started_at`` is retained as human-facing metadata AND as the ordering
fallback for rows/callers that carry no generation (a legacy row written before
migration 0183, or a deployment whose sequence is not yet created). A NULL
published token sorts oldest, so a stamped write always overwrites an unstamped
one. When the caller passes neither token, the guard degrades to epoch-only
ordering (the R3 behaviour) rather than blocking every write.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, or_

from shared.db.models import KPILatest


def _within_epoch_clause(
    eval_started_at: datetime | None, eval_generation: int | None
):
    """The same-epoch ordering predicate, or ``None`` when no token is available.

    Generation wins when present: it is a total order. ``eval_started_at`` is the
    documented fallback only (see the module docstring); it is NOT unique, so it
    is used only when there is no generation to compare.
    """
    if eval_generation is not None:
        return or_(
            KPILatest.eval_generation.is_(None),
            KPILatest.eval_generation <= eval_generation,
        )
    if eval_started_at is not None:
        return or_(
            KPILatest.eval_started_at.is_(None),
            KPILatest.eval_started_at <= eval_started_at,
        )
    return None


def kpi_latest_write_guard(
    eval_epoch: int | None,
    eval_started_at: datetime | None,
    eval_generation: int | None = None,
):
    """Return the ``ON CONFLICT DO UPDATE ... WHERE`` clause for a kpi_latest upsert.

    ``None`` means "no guard" (unconditional write) — used by the legacy/test
    callers that stamp no deploy binding at all. Otherwise the returned clause is
    true only when the incoming ``(eval_epoch, eval_generation)`` is >= the
    already-published row's, by the tuple ordering documented above.
    """
    if eval_epoch is None:
        return None
    within = _within_epoch_clause(eval_started_at, eval_generation)
    if within is None:
        # No within-epoch token available: order by epoch alone. Never regress
        # a strictly newer epoch; a same-or-older epoch write still applies.
        return or_(
            KPILatest.evaluated_for_epoch.is_(None),
            KPILatest.evaluated_for_epoch <= eval_epoch,
        )
    return or_(
        # Existing row carries no epoch binding: overwrite (establish a binding).
        KPILatest.evaluated_for_epoch.is_(None),
        # Strictly newer epoch: overwrite.
        KPILatest.evaluated_for_epoch < eval_epoch,
        # Same epoch: overwrite only when this evaluation's ordering token is at
        # or beyond the published row's. A NULL published token sorts oldest.
        and_(KPILatest.evaluated_for_epoch == eval_epoch, within),
    )


#: Columns a write must NOT touch when it carries no ordering generation of its
#: own. R7 review round 1, finding 5: a degraded writer (its ``nextval`` failed,
#: so ``eval_generation is None``) wrote ``eval_generation = NULL`` over a
#: stored token, after which the row sorted as "oldest" and ANY later writer —
#: however stale — could publish over it. A writer that cannot contribute a
#: token leaves the existing one alone rather than downgrading it.
#:
#: SCOPE OF THIS FIX, stated precisely (R7 review round 2, M2). Preserving the
#: stored token defeats writers allocated BEFORE it. It does NOT make a
#: generation-less write orderable against one allocated AFTER the stored token:
#:
#:     A(gen=10) publishes 250 -> C(gen=11, staler read) in flight
#:     -> B(no generation, started later) writes 250, keeps gen=10
#:     -> C commits: 10 <= 11 passes, 999 is served
#:
#: That is not a regression (NULL gave the same outcome) and it is not solvable
#: from within the guard: with no token, B is genuinely unorderable against C.
#: It is reachable ONLY while some writers can allocate a generation and others
#: cannot — i.e. a rolling deploy across migration 0183, or a partial sequence
#: outage, both of which log an ERROR from ``kpi_eval_generation``. Once every
#: writer carries a token the ordering is total. Do not restate this fix as
#: "a staler writer can no longer win"; it is narrower than that.
_GENERATION_COLUMN = "eval_generation"


def strip_ungenerated_token(values: dict, eval_generation: int | None) -> dict:
    """Remove ``eval_generation`` from an upsert's ``set_`` when it is None.

    The INSERT path still stores NULL (there is no prior token to preserve); it
    is only the UPDATE path that must not regress a stored token to NULL.
    """
    if eval_generation is not None:
        return values
    return {k: v for k, v in values.items() if k != _GENERATION_COLUMN}


def token_advances(stored, incoming) -> bool:
    """True when writing ``incoming`` would ADVANCE the stored ordering token.

    ``None`` incoming never advances (nothing to advance to); ``None`` stored is
    the oldest possible token, so any real incoming token advances it.
    """
    if incoming is None:
        return False
    if stored is None:
        return True
    return stored < incoming


def kpi_latest_write_is_noop(
    *,
    stored_value_tuple,
    stored_epoch: int | None,
    stored_version_id,
    stored_generation: int | None,
    stored_started_at: datetime | None,
    new_value_tuple,
    eval_epoch: int | None,
    eval_version_id,
    eval_generation: int | None,
    eval_started_at: datetime | None,
) -> bool:
    """Whether an upsert can be SKIPPED entirely as a true no-op.

    Bug-7982 R7 finding 1 (root cause). ``evaluate-batch``'s pre-upsert dedup
    compared only ``(value tuple, epoch, version_id)``. The ordering token is
    ALSO a stored, semantically load-bearing column, so a writer whose value
    happened to equal the published one was skipped and the stored token never
    advanced. The gate reproduced the consequence live:

        writer A publishes value=250, token T=10:00:00
        writer B evaluates LATER (T=10:00:20) and computes the same 250
                 -> old dedup: "unchanged", skipped; stored token stays 10:00:00
        writer C evaluated BETWEEN them (T=10:00:10) with a STALE value=999
                 -> passes the guard against the stranded 10:00:00 token and wins

    The served number is 999 even though B's later, fresher evaluation confirmed
    250. A skip is therefore only safe when the write would change NOTHING that
    is stored — value, deploy binding AND ordering token.

    Write amplification (the F-017-29 concern this dedup was added for) is not
    reintroduced: only an internal-service publish (the hourly sweep and the
    post-deploy trigger) ever reaches this helper — ``kpis.py::evaluate_batch``
    gates the upsert on ``is_service_context``, so a per-user scorecard render
    never writes ``kpi_latest`` at all.
    """
    if stored_value_tuple is None:
        return False
    if (stored_value_tuple, stored_epoch, stored_version_id) != (
        new_value_tuple, eval_epoch, eval_version_id
    ):
        return False
    # Value and binding are byte-equal. Still NOT a no-op if this write would
    # advance the ordering token — skipping strands it and reopens the race above.
    if token_advances(stored_generation, eval_generation):
        return False
    if eval_generation is None and token_advances(
        stored_started_at, eval_started_at
    ):
        return False
    return True
