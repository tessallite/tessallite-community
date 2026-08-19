"""Named Query finalisation guard — the same protocol as the pocket guard.

The Named Query refresh writer runs the SAME three legs the pocket writer
(Bug-8807) and the three aggregate writers (Bug-8481/Bug-8602) run: after the
physical build and BEFORE dirtying the artifact row, take every control-plane
row lock in the one fixed order (``lock_finalization_rows``), re-read the
artifact's COMMITTED status ``FOR UPDATE``, and re-prove the target and source
routing identities the build dialled. Only a row still carrying this run's own
``invalidating`` may return to ``fresh``; anything else lands ``stale`` with a
reason and no row manifest — byte-identical to what the control-plane
invalidator writes.

The committed-status reader is the only Named-Query-specific piece; every other
leg reuses the shared artifact binding module verbatim. The reader mirrors
``read_committed_pocket_status`` (same ``no_autoflush`` + scalar ``FOR UPDATE``
pattern) because the guard's exclusion property lives in the row lock, not in
any comparison.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# The one status the named-query resolver serves materialised. Every other
# status is a refusal signal.
NQ_STATUS_FRESH = "fresh"
NQ_STATUS_STALE = "stale"
NQ_STATUS_INVALIDATING = "invalidating"

_REASON_INVALIDATED = (
    "This Named Query was invalidated while it was being rebuilt, so the rows "
    "it just wrote are not provably current. It must be rebuilt again before "
    "it can serve."
)
_REASON_ROW_GONE = (
    "This Named Query could not be confirmed at the end of its rebuild, so it "
    "must be rebuilt before it can serve."
)
_REASON_TARGET_MOVED = (
    "The database this Named Query writes to changed while it was being "
    "rebuilt, so the rows were written to a different database. It must be "
    "rebuilt before it can serve."
)
_REASON_SOURCE_MOVED = (
    "The database this Named Query reads from changed while it was being "
    "rebuilt, so the rows came from a database this model no longer reads. It "
    "must be rebuilt before it can serve."
)


@dataclass(frozen=True)
class NamedQueryFinalizationState:
    """Committed truth read under the finalisation locks.

    Plain scalars, deliberately: read once, under lock, and never re-derivable
    from an ORM object a later flush could move. ``committed_status`` is
    ``None`` when the artifact row no longer exists.
    """

    committed_status: str | None
    target_binding_matches: bool
    source_binding_matches: bool


def resolve_named_query_serving_refusal(
    state: NamedQueryFinalizationState,
    *,
    own_status: str = NQ_STATUS_INVALIDATING,
) -> str | None:
    """``None`` when the artifact may return to ``fresh``, else the reason.

    Pure: every input is already-read committed truth. Fail closed — an
    unproven artifact is never returned to the serving pool.
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


async def read_committed_named_query_status(
    db: AsyncSession, artifact_id: Any, *, lock_for_finalization: bool = True,
) -> str | None:
    """The artifact's COMMITTED status, by default under a row lock.

    A scalar SELECT issues a real query rather than returning the session's
    identity-map copy. ``FOR UPDATE`` makes the observation actionable (see the
    module docstring); autoflush is suppressed for the same reason the pocket
    reader suppresses it — a finalisation path holds dirty artifact metadata.
    """
    from shared.db.models import NamedQueryArtifact

    with db.no_autoflush:
        stmt = select(NamedQueryArtifact.status).where(
            NamedQueryArtifact.id == artifact_id
        )
        if lock_for_finalization:
            stmt = stmt.with_for_update()
        return (await db.execute(stmt)).scalar_one_or_none()


async def read_named_query_finalization_state(
    db: AsyncSession,
    *,
    artifact_id: Any,
    target_binding: Any,
    source_binding: Any,
    connection_ids: Sequence[Any] = (),
    target_id: Any = None,
    model_id: Any = None,
) -> NamedQueryFinalizationState:
    """Take the finalisation locks and read every committed input, in order.

    ORDER IS LOAD-BEARING and matches the pocket/aggregate writers exactly:
    control-plane rows in the fixed order, then the artifact row ``FOR
    UPDATE``, then the two binding re-proofs. The caller MUST NOT have dirtied
    the artifact row before calling this. Both re-proofs fail CLOSED on any
    error resolving live state.
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
    committed_status = await read_committed_named_query_status(
        db, artifact_id, lock_for_finalization=True
    )
    target_matches = await target_build_binding_matches_live(
        db, target_binding, lock_for_finalization=True,
    )
    source_matches = await source_build_binding_matches_live(
        db, source_binding, lock_for_finalization=True,
    )
    return NamedQueryFinalizationState(
        committed_status=committed_status,
        target_binding_matches=bool(target_matches),
        source_binding_matches=bool(source_matches),
    )
