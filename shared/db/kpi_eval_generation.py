"""Strictly-increasing, DB-issued ordering token for a KPI evaluation (Bug-7982 R7).

Every ``kpi_latest`` writer must be totally ordered against every other writer of
the same row. R6 used ``clock_timestamp()`` for that; the external gate showed it
is not a sequence — 82,242 duplicates in 100k live samples on this project's own
Postgres — so an exact tie degenerated to last-commit-wins, the very defect the
ordering guard exists to close.

``nextval`` on a Postgres sequence is atomic and never repeats, so it is a TOTAL
order over evaluation starts: if evaluation A allocates before evaluation B in
real time, ``A.generation < B.generation``, and two distinct evaluations can
never tie. The sequence lives in the tenant schema (created by migration 0183 and
attached to ``TenantBase.metadata`` so test harnesses that ``create_all`` get it
too) and is resolved through the connection's ``search_path`` exactly like every
other tenant-schema object.

The token is allocated ONCE, when an evaluation STARTS — before any source read —
so "allocated later" is a sound proxy for "read fresher data". It is threaded
unchanged through the whole evaluation (sweep -> evaluate-batch request -> both
upsert helpers) so one logical evaluation carries one token.

Degradation: a deployment whose migration has not been applied yet has no
sequence. Allocation then falls back to ``(clock_timestamp(), None)`` — the R6
timestamp-only ordering — inside a SAVEPOINT so the failed ``nextval`` cannot
poison the caller's transaction. That is strictly better than raising: the
publish path stays available and the ordering merely degrades to its previous
strength, loudly logged once per process.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Sequence, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import TenantBase

logger = logging.getLogger(__name__)

KPI_EVAL_GENERATION_SEQUENCE_NAME = "kpi_eval_generation_seq"

# Attached to TenantBase.metadata so ``metadata.create_all`` (used by the
# integration harnesses that build an isolated tenant schema) emits the sequence
# alongside the tables. Unqualified, so it lands in the connection's search_path
# schema — the same placement rule migration 0183 uses.
KPI_EVAL_GENERATION_SEQ = Sequence(
    KPI_EVAL_GENERATION_SEQUENCE_NAME, metadata=TenantBase.metadata
)

#: Re-arm window for the sequence-unavailable ERROR. R7 review round 1, finding 5:
#: this was a one-shot process-global flag, so a RECURRING degradation (which is
#: exactly the condition that makes the mixed-token hazard reachable) was silent
#: after its first occurrence.
_DEGRADATION_LOG_REARM_SECONDS = 300.0
_sequence_missing_last_logged: float | None = None


@dataclass(frozen=True)
class KpiEvalMarker:
    """The ordering token pair captured at evaluation start.

    ``generation`` is the authoritative order. ``started_at`` is metadata, and
    the documented fallback order when ``generation`` is None.
    """

    started_at: datetime
    generation: int | None


async def allocate_kpi_eval_marker(db: AsyncSession) -> KpiEvalMarker:
    """Allocate this evaluation's ordering token from the DB.

    One round-trip on the happy path (clock + nextval in a single SELECT), run
    inside a SAVEPOINT so a missing sequence degrades instead of aborting the
    caller's transaction.
    """
    global _sequence_missing_last_logged
    try:
        async with db.begin_nested():
            row = (
                await db.execute(
                    select(
                        func.clock_timestamp(),
                        KPI_EVAL_GENERATION_SEQ.next_value(),
                    )
                )
            ).one()
        return KpiEvalMarker(started_at=row[0], generation=int(row[1]))
    except Exception as exc:  # noqa: BLE001 — degrade, never fail the evaluation
        _now = time.monotonic()
        if (
            _sequence_missing_last_logged is None
            or (_now - _sequence_missing_last_logged) >= _DEGRADATION_LOG_REARM_SECONDS
        ):
            _sequence_missing_last_logged = _now
            logger.error(
                "Bug-7982: sequence %s is unavailable (%s). kpi_latest write "
                "ordering has DEGRADED to the non-unique clock_timestamp marker "
                "— two same-epoch evaluations that start in the same microsecond "
                "can once again resolve by commit order. Apply migration 0183.",
                KPI_EVAL_GENERATION_SEQUENCE_NAME, exc,
            )
        started = (await db.execute(select(func.clock_timestamp()))).scalar_one()
        return KpiEvalMarker(started_at=started, generation=None)


def clamp_supplied_marker(
    supplied_started_at: datetime | None,
    supplied_generation: int | None,
    marker: KpiEvalMarker,
) -> tuple[datetime, int | None]:
    """Reconcile a CALLER-SUPPLIED ordering token with a freshly allocated one.

    The scheduler sweep threads its own token into ``evaluate-batch`` so the
    handler's publish and the sweep's own write share ONE token and neither
    falsely suppresses the other. That makes the token caller-controlled, so it
    must be bounded: an unclamped far-future token would wedge the row, blocking
    every later legitimate same-epoch write for that KPI forever.

    Rules, and why each one:

    * ``started_at`` — ``min(supplied, freshly read server clock)``. The sweep's
      genuine value was read earlier, so ``min`` is a no-op on the legitimate
      path and only bites a bogus future value.
    * ``generation`` — ``min(supplied, freshly allocated)`` for the same reason:
      the sweep allocated first, so its value is strictly smaller.
    * a supplied generation with NO freshly allocated one to bound it against
      (the sequence is unavailable) is REFUSED, not persisted. An unverifiable
      token is worse than none: none degrades to timestamp ordering, whereas a
      bogus one is durable.

    Extracted as a named function (R7 review round 1, finding 6) because it lived
    inline in ``evaluate_batch``, which cannot be exercised without the full
    request stack — so the clamp, the security-relevant part, had no coverage.
    """
    started_at = (
        min(supplied_started_at, marker.started_at)
        if supplied_started_at is not None else marker.started_at
    )
    if supplied_generation is None:
        return started_at, marker.generation
    if marker.generation is None:
        return started_at, None
    return started_at, min(supplied_generation, marker.generation)
