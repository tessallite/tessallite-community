"""Deployed-snapshot authority for named-set serving (Bug-8384 / F-013-08 part 4).

The deployed model snapshot is the SINGLE serving authority for a named set's
DEFINITION on BI surfaces. Editing a named set's expression changes what
MDSCHEMA_SETS advertises and what Execute-time MDX inlining substitutes, so an
UNDEPLOYED draft edit reaching a BI client changes production query semantics
before the modeller has clicked Deploy. That is a draft leak, not a staleness
nuisance: an Excel/Power BI/Tableau user sees numbers computed from a definition
nobody has published.

This closes the last part of F-013-08. Parts 1-3 (snapshot+restore the named-set
definition on revert, preserve only governance fields, validate detached
replacement references) landed with Bug-7982; the SQL side was already pinned
(``query-router/src/params/named_list_resolver._extract_lists_from_snapshot``
reads ``snapshot["named_sets"]``). The MDX side still read live rows, so the two
surfaces of the SAME named set could disagree — the SQL path serving the
deployed members while MDX served the draft expression.

Governance/lifecycle fields are overlaid from the LIVE row. That is deliberate
and mirrors the KPI resolver: an admin deprecating a set must remove it from the
BI catalogue immediately (the gateway's ``certification_status != "deprecated"``
filter reads this overlay), without needing a model redeploy.

Fail-closed contract, shared with ``kpi_deploy_resolver`` via
``deploy_resolver_core``:
- deployed pointer set but the version row / snapshot is missing, empty or
  malformed -> ``NamedSetSnapshotInvalidError``; never fall back to live drafts.
- snapshot present but the named-set id is absent -> withheld. The set was
  created after the last deploy, so no deployed definition exists for it and it
  must not reach a BI client.
- undeployed model -> no serving authority; every set is withheld from BI
  surfaces. Builder/modeller surfaces do not use this resolver and keep showing
  the live draft.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Model, NamedSet
from shared.deploy_resolver_core import (
    build_served_row,
    index_snapshot_rows,
    load_deployed_snapshot,
)

_SNAPSHOT_FAMILY = "named_sets"

# Governance/lifecycle fields overlaid from the LIVE row (never sourced from the
# snapshot). Kept in lockstep with the rehydrator's revert contract
# (``rehydrator._NAMED_SET_GOVERNANCE_FIELDS``) plus the timestamps a detached
# instance needs for its response model. Everything NOT listed here is a
# DEFINITION field and comes from the deployed snapshot: name, display_name,
# description, display_folder, scope, expression, dimensions,
# builder_definition, list_type.
_LIVE_GOVERNANCE_FIELDS: tuple[str, ...] = (
    "certification_status",
    "replacement_id",
    "owner_user_id",
    "created_at",
    "updated_at",
)

# Identity columns always come from the live row: a snapshot row carries the
# same id/model_id, but anchoring on the live row means a re-keyed import can
# never mis-bind a definition onto the wrong set.
_IDENTITY_FIELDS: tuple[str, ...] = ("id", "model_id")


class NamedSetSnapshotInvalidError(Exception):
    """Raised when a DEPLOYED model's snapshot cannot authorise named-set serving.

    Distinguishes DEPLOYED_SNAPSHOT_INVALID from UNDEPLOYED (F-013-05): a serving
    surface must fail closed rather than fall back to live drafts.
    """

    error_code = "DEPLOYED_SNAPSHOT_INVALID"


@dataclass(frozen=True)
class ResolvedNamedSet:
    """A named set resolved from the deployed snapshot for serving."""

    named_set: NamedSet   # detached ORM instance: snapshot def + live governance
    deployed_version_id: UUID
    deploy_epoch: int


def build_served_named_set(
    snapshot_named_set: dict[str, Any], live_named_set: NamedSet
) -> NamedSet:
    """Build a detached NamedSet: snapshot definition + live governance overlay."""
    return build_served_row(
        NamedSet,
        snapshot_named_set,
        live_named_set,
        governance_fields=_LIVE_GOVERNANCE_FIELDS,
        identity_fields=_IDENTITY_FIELDS,
    )


async def resolve_served_named_sets(
    db: AsyncSession, model: Model, live_named_sets: list[NamedSet]
) -> tuple[list[ResolvedNamedSet], list[UUID]]:
    """Resolve the served named sets for a model against the deployed snapshot.

    This resolver owns DEFINITION, not MEMBERSHIP. It iterates the live rows and
    replaces each one's definition fields with the deployed snapshot's:
    - live row present in the snapshot -> snapshot definition + live governance;
    - live row absent from the snapshot (created since the last deploy) ->
      withheld, and reported in ``withheld_ids``, because no deployed definition
      exists for it.

    MEMBERSHIP IS DELIBERATELY LEFT LIVE-DRIVEN, and that is a known gap rather
    than an oversight — see Bug-8753, which now covers both families. Deleting a
    deployed named set without deploying still removes it from BI immediately.
    Serving "snapshot orphans" (a row the deployed version still pins but which
    no longer exists live) was implemented and then deliberately REVERTED,
    because membership turns out to be a product decision, not a bug:
    - an orphan has no live governance row, so its certification would have to
      come from the snapshot — which makes DELETING a set an admin had already
      DEPRECATED silently un-deprecate it and put it back in the BI catalogue
      badged as certified. Fixing that properly needs a governance tombstone
      retained at delete time (a migration), not a resolver tweak.
    - the sibling ``kpi_deploy_resolver`` cannot adopt orphan-serving at all
      without a decision first: ``list_kpis`` pre-filters live rows by
      ``KPI.is_deployed`` and by certification BEFORE calling its resolver, so
      driving that loop from the snapshot would resurrect KPIs an admin had
      un-deployed or drafts a viewer must not see.
    Shipping orphan-serving for named sets alone would therefore have made the
    two families disagree about the same question while leaving a governance
    hole in the one that changed. Both halves are one decision; Bug-8753 holds
    it.

    For an undeployed model ``resolved`` is empty and every live id is withheld:
    a BI surface has no serving authority to read from. Raises
    ``NamedSetSnapshotInvalidError`` for a deployed model with an invalid
    snapshot.
    """
    snapshot = await load_deployed_snapshot(
        db, model, family=_SNAPSHOT_FAMILY, error_cls=NamedSetSnapshotInvalidError,
    )
    if snapshot is None:
        return [], [ns.id for ns in live_named_sets]
    indexed = index_snapshot_rows(snapshot, _SNAPSHOT_FAMILY)
    version_id = model.deployed_version_id
    epoch = int(getattr(model, "deploy_epoch", 0) or 0)
    resolved: list[ResolvedNamedSet] = []
    withheld: list[UUID] = []
    for live in live_named_sets:
        snap_row = indexed.get(str(live.id))
        if snap_row is None:
            withheld.append(live.id)
            continue
        resolved.append(
            ResolvedNamedSet(
                named_set=build_served_named_set(snap_row, live),
                deployed_version_id=version_id,
                deploy_epoch=epoch,
            )
        )
    # Ordering is part of the served definition contract. A live draft rename
    # must not alter the gateway's substitution sequence before deployment.
    resolved.sort(
        key=lambda row: (
            (row.named_set.name or "").casefold(),
            str(row.named_set.id),
        )
    )
    return resolved, withheld
