"""One rule for both artifact families: a matcher-REFUSED artifact must be
rebuilt in FULL, never incrementally (Bug-8431).

Why this module exists
----------------------
An incremental refresh re-derives only a recent window of rows and leaves every
older row exactly as the previous build wrote it. That is sound only while the
rows already on disk were produced by the SAME definition the new slice is
produced from. If the artifact was built for a SUPERSEDED deployed pointer — a
deploy or revert changed a measure expression without changing the physical
shape, so no shape-based escalation fires — a partial rebuild leaves the older
rows computed under the OLD definition and then the success path stamps the
build binding and clears the staleness signal. The version gate
(:mod:`shared.artifact_version_gate`) then ACCEPTS the artifact and the runtime
matcher serves a table that mixes old-definition and new-definition rows: a
silent wrong number over JDBC/XMLA.

The aggregate incremental path has carried this guard since Bug-8431 round 2.
The pocket path did not, and the same live exposure re-appeared there through a
different primitive (Task #106 re-verification, 2026-08-05). Per CLAUDE.md's
shared-primitive hardening discipline the rule now lives in ONE place and every
caller evaluates the identical predicate, so the two families cannot silently
drift apart again.

Two layers, deliberately separate
---------------------------------
``artifact_refused_by_matcher`` answers the SERVE-side question — "would the
runtime matcher refuse this artifact right now?" — with the same two signals
both matchers use: the artifact's own staleness signal, and whether its recorded
build binding still names the model's deployed pointer. The rebuild sweeps use
it to decide due-ness (a refused artifact is always due).

``full_rebuild_required`` layers the BUILD-side policy on top: a refused
artifact may not be rebuilt incrementally. It additionally short-circuits when
the model is not deployed, because there is then no current pointer to compare
against and no query can reach a matcher in the first place.

Be precise about WHY that short-circuit is safe, because a wrong reason here is
more dangerous than no reason. It is NOT that the matchers refuse a NULL
pointer: both matchers guard their version check with ``if
<deployed_version_id> is not None``, so they never evaluate the gate for an
undeployed model. The real chain is (1) ``semantic/binder.py`` raises
``ModelNotDeployedError`` for every query against an undeployed model, so no
matcher runs at all, and (2) a build started while undeployed stamps a NULL
binding, which ``artifact_built_for_current`` refuses the moment the model IS
deployed — at which point the rebuild sweeps see the artifact as refused and
this very predicate forces its FULL rebuild. If (1) is ever relaxed (a preview
or authoring path that binds without a 409), this short-circuit must be
revisited before that lands.

Callers whose artifact carries a partial-write hazard while undeployed may add
their OWN precondition ahead of this call — ``shared/pocket/refresh.py`` does,
because a pocket left ``failed`` may have a half-mutated table on disk. That is
a tightening layered on this rule, never a second copy of it.

Freshness of the live pointer
-----------------------------
Callers should pass the SAME captured ``(deployed_version_id, deploy_epoch)``
they will later stamp onto the artifact via
``shared.artifact_build_binding.apply_build_binding``. That keeps the decision
and the stamp self-consistent: if the captured pointer turns out to be older
than committed truth, the guard may let an incremental slice through, but the
stamp then records that same older pointer and ``apply_build_binding``'s own
FRESH supersession probe marks the artifact stale — the matcher refuses it and
the next sweep rebuilds it in full. The degraded outcome is a wasted cycle, not
a wrong number. Passing a pointer NEWER than the one that will be stamped is the
combination that must never happen.
"""
from __future__ import annotations

from typing import Any

from shared.artifact_version_gate import artifact_built_for_current

__all__ = ["artifact_refused_by_matcher", "full_rebuild_required"]


def artifact_refused_by_matcher(
    *,
    artifact_is_stale: bool,
    built_for_version_id: Any,
    built_for_epoch: Any,
    deployed_version_id: Any,
    deploy_epoch: Any,
) -> bool:
    """True when the runtime matcher would currently refuse this artifact.

    Two independent refusal signals, identical for aggregates and pockets:

    * ``artifact_is_stale`` — the artifact's own staleness signal. For an
      aggregate that is ``AggregateDefinition.is_stale``; for a pocket it is
      ``PocketDefinition.status != "fresh"`` (``fresh`` is the only status the
      pocket matcher considers).
    * The recorded build binding no longer names the model's deployed pointer,
      evaluated through :func:`shared.artifact_version_gate.artifact_built_for_current`
      so serve-side and rebuild-side can never disagree.

    The binding comparison is the load-bearing one: a concurrent refresh's
    success path can clobber the staleness signal, but it cannot rewrite the
    pointer an artifact's rows were actually built for.
    """
    if artifact_is_stale:
        return True
    return not artifact_built_for_current(
        built_for_version_id,
        built_for_epoch,
        deployed_version_id,
        deploy_epoch,
    )


def full_rebuild_required(
    *,
    artifact_is_stale: bool,
    built_for_version_id: Any,
    built_for_epoch: Any,
    deployed_version_id: Any,
    deploy_epoch: Any,
) -> bool:
    """True when this artifact must be rebuilt in FULL rather than incrementally.

    ``deployed_version_id`` of None (an undeployed model) returns False: no
    query can reach a matcher for such a model (the binder raises
    ``ModelNotDeployedError`` first), so a partial rebuild cannot produce a
    mixed-definition serve, and whatever it writes is stamped with a NULL
    binding that ``artifact_built_for_current`` refuses as soon as the model IS
    deployed — forcing the full rebuild then. See the module docstring for why
    the naive "the version gate refuses a NULL pointer" reading is wrong, and
    for when a caller should add its own precondition ahead of this one.
    """
    if deployed_version_id is None:
        return False
    return artifact_refused_by_matcher(
        artifact_is_stale=artifact_is_stale,
        built_for_version_id=built_for_version_id,
        built_for_epoch=built_for_epoch,
        deployed_version_id=deployed_version_id,
        deploy_epoch=deploy_epoch,
    )
