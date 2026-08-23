"""Encoding of ``ai_optimizer_runs.claimed_by`` (Bug-8034, review F-559-01).

One string column carries two facts about a dispatched advisor run:

* **who claimed it** — the scheduler worker that committed the
  ``queued -> running`` flip, and
* **whether an optimizer has accepted execution of that claim.**

Before this encoding existed, ``running`` conflated the two. A scheduler that
lost the optimizer's ``202`` could not tell "the optimizer never saw it" from
"the optimizer already started it", so it returned the row to ``queued``; the
next sweep reclaimed it and the execute route — which only checked
``status == 'running'`` — started a second LLM pass and a second
materialisation of the same run id.

The encoding is deliberately a prefix on the existing column, not a new column:
the chosen design (see
``docs/architecture/architecture_ai-advisor-durable-dispatch.md``) is that the
run row IS the queue, with no lease, no heartbeat and no migration.

::

    claimed_by = "scheduler:host:1234"        claimed, not yet accepted
    claimed_by = "exec|scheduler:host:1234"   an optimizer accepted this claim

Two invariants fall out of it, and both are load-bearing:

1. The scheduler's release is guarded by ``claimed_by == <its own claimant>``.
   Acceptance changes that value, so a run the optimizer accepted can never be
   returned to the queue — whatever the transport reported.
2. The execute route requires the claim owner in the request to match the row.
   A stale hand-off whose claim was already released (or re-claimed by another
   worker) is refused instead of started.

Both invariants rest on the optimizer STAMPING acceptance. A build that predates
this module accepts a hand-off and spawns without stamping anything, so against
it an unmarked claim means "never accepted" and "already running" at the same
time — and a lost response is enough to make the scheduler requeue work that is
already in flight. That is not hypothetical during a rolling upgrade, where a
current scheduler and a previous optimizer run side by side by design.

So the ROUTE PATH carries the protocol (review F-559-01-R2), and both services
take it from :data:`EXECUTE_CLAIM_ROUTE`. A previous optimizer does not serve
this path and answers 404, which the dispatcher classifies as REFUSED — the one
outcome that is a positive statement that nothing was started — so the claim is
released and waits for an optimizer that speaks the protocol. Routing is decided
before the body is read, which is why the path and not a new request FIELD is
the gate: an older FastAPI route with no matching parameter simply ignores an
unknown field and accepts anyway, which fails OPEN. The reverse pairing is
closed by the same constant: a previous scheduler calls the previous path, which
a current optimizer no longer serves, so it too is refused rather than executed
without a claim check.
"""
from __future__ import annotations

# The column is ``String(256)``. Reserving the marker's width for the owner
# keeps the encoded value round-trippable: a claim owner is never truncated by
# the act of marking it accepted.
CLAIMED_BY_MAX_LENGTH = 256
EXEC_ACCEPT_PREFIX = "exec|"
CLAIM_OWNER_MAX_LENGTH = CLAIMED_BY_MAX_LENGTH - len(EXEC_ACCEPT_PREFIX)

# The optimizer's execute route, below the service's ``/api/v1`` prefix. The
# suffix names the protocol this module defines: the hand-off is addressed to a
# CLAIM, and acceptance of that claim is stamped on the row before anything is
# spawned. A build that does not implement that cannot be reached at this path.
# Producer (optimizer route decorator) and consumer (scheduler dispatcher URL)
# both read it from here so the two can never drift apart silently.
EXECUTE_CLAIM_ROUTE = "/optimize/internal/ai/runs/{run_id}/execute-claim"


def is_execution_accepted(claimed_by: str | None) -> bool:
    """True when an optimizer has accepted execution of this claim."""
    return bool(claimed_by) and claimed_by.startswith(EXEC_ACCEPT_PREFIX)


def claim_owner(claimed_by: str | None) -> str | None:
    """The scheduler worker that owns the claim, accepted or not."""
    if not claimed_by:
        return None
    if claimed_by.startswith(EXEC_ACCEPT_PREFIX):
        return claimed_by[len(EXEC_ACCEPT_PREFIX):]
    return claimed_by


def mark_execution_accepted(claimed_by: str | None) -> str:
    """The value to persist when an optimizer accepts execution of a claim.

    Idempotent: marking an already-accepted value returns it unchanged, so a
    replayed hand-off cannot stack prefixes.
    """
    owner = (claim_owner(claimed_by) or "")[:CLAIM_OWNER_MAX_LENGTH]
    return f"{EXEC_ACCEPT_PREFIX}{owner}"
