"""Artifact-to-version compatibility gate (F-013-02 / F-013-03 / Bug-8250).

The single source of truth for deciding whether a materialised artifact (an
aggregate or a pocket) is compatible with the model version currently serving.

Core invariant: an artifact may serve a query only when it was BUILT FOR the
exact deployed ``(version_id, epoch)`` the query is bound to. Freshness is not
compatibility — a physically fresh artifact built under a previous definition
must not serve after a deploy or revert.

Comparison rules (fail closed)
------------------------------
* A NULL ``built_for_version_id`` means the artifact was never built for any
  deployed version (unmaterialised, built while undeployed, or import-cleared).
  It is NOT compatible with a deployed model.
* A NULL ``built_for_epoch`` is equally incompatible. Every writer stamps the
  pair together through ``shared.artifact_build_binding``, which never produces
  a non-NULL version with a NULL epoch, so a half-written binding is a row of
  unknown provenance — legacy data, a hand-written UPDATE, a partial migration.
  Treating it as epoch 0 (the pre-Bug-8250-re-gate behaviour) let such a row
  serve against a never-redeployed model, which is the fail-OPEN direction on a
  wrong-numbers gate. ``Model.deploy_epoch`` is ``NOT NULL DEFAULT 0``, so a
  NULL on the deployed side is likewise a state that should not exist and is
  refused rather than coerced.
* The version id AND the epoch must both match exactly. A revert-to-same-version
  bumps the epoch (content changed), so an epoch-only difference is still an
  incompatibility.

One rule, two encodings
-----------------------
The runtime matchers evaluate the rule in Python, row by row. Deploy/revert
staling evaluates it in SQL, as a set-based UPDATE over every artifact of a
model — it cannot call a Python predicate per row. Both encodings therefore live
HERE, in one module, and ``shared/tests/test_artifact_version_gate.py`` runs the
same truth table through both. A stricter ad-hoc SQL copy living next to the
deploy endpoint (what shipped before) is not a single source of truth; it is a
second rule that happens to agree today.
"""
from __future__ import annotations

from typing import Any


def coerce_epoch(value: Any) -> int | None:
    """Coerce an epoch to int, or None when it is absent/uninterpretable.

    ``None`` is NOT coerced to 0: see the fail-closed rules above.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def artifact_built_for_current(
    built_for_version_id: Any,
    built_for_epoch: Any,
    deployed_version_id: Any,
    deploy_epoch: Any,
) -> bool:
    """Return True iff the artifact's build binding matches the deployed pointer.

    All four arguments may be UUIDs, strings, ints, or None; comparison is by
    string form for the version id and by int for the epoch, so callers need not
    normalise. A NULL on ANY of the four is never compatible.
    """
    if built_for_version_id is None or deployed_version_id is None:
        return False
    if str(built_for_version_id) != str(deployed_version_id):
        return False
    built_epoch = coerce_epoch(built_for_epoch)
    live_epoch = coerce_epoch(deploy_epoch)
    if built_epoch is None or live_epoch is None:
        return False
    return built_epoch == live_epoch


def artifact_incompatible_sql(
    version_column: Any,
    epoch_column: Any,
    deployed_version_id: Any,
    deploy_epoch: Any,
):
    """The SQL encoding of ``not artifact_built_for_current(...)``.

    Returns a SQLAlchemy boolean expression selecting the artifact rows that are
    NOT compatible with ``(deployed_version_id, deploy_epoch)`` — i.e. exactly
    the rows a deploy or revert must stale.

    The explicit ``IS NULL`` arms are load-bearing, not defensive noise: in SQL's
    three-valued logic ``col != :value`` evaluates to NULL (not TRUE) when
    ``col`` is NULL, so a WHERE clause built only from ``!=`` silently SKIPS
    every NULL-bound artifact — the precise rows the Python gate refuses. Without
    them the two encodings disagree on the most dangerous input.

    ``deployed_version_id`` of None (an undeploy) makes every artifact
    incompatible, matching ``artifact_built_for_current``'s NULL-deployed rule.
    """
    from sqlalchemy import or_, true

    if deployed_version_id is None:
        return true()
    live_epoch = coerce_epoch(deploy_epoch)
    if live_epoch is None:
        # A NULL/uninterpretable live epoch is a state the schema forbids
        # (NOT NULL DEFAULT 0). Refuse everything rather than guess.
        return true()
    return or_(
        version_column.is_(None),
        version_column != deployed_version_id,
        epoch_column.is_(None),
        epoch_column != live_epoch,
    )
