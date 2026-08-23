"""Deployed-snapshot authority for KPI serving (F-017-01 / F-013-04).

The deployed model snapshot is the SINGLE serving authority for a KPI's
DEFINITION. A KPI definition edit is only live to BI/scorecard consumers after
a model Save + Deploy — never on the strength of the per-KPI ``is_deployed``
publication flag alone. ``KPI.is_deployed`` remains a governance/visibility flag
layered ON TOP of the deployed snapshot; it never sources definition fields.

This module resolves the *served* KPI: definition fields come from the deployed
snapshot's pinned KPI (matched by id), governance/lifecycle fields are overlaid
from the live ORM row. It mirrors the semantic-shape authority A1 established for
measures/dimensions (``snapshot_resolver.resolve_snapshot_authority``) and the
KPI-governance split the rehydrator already codifies (``_KPI_GOVERNANCE_FIELDS``).

Fail-closed contract for a DEPLOYED model (Bug-7987 / Bug-7978, aligned with
F-013-05):
- deployed pointer set but the version row / snapshot is missing/empty/malformed
  → ``SnapshotInvalid`` — serving surfaces raise a stable error, never fall back
  to live/draft definitions.
- snapshot present but the KPI id is absent → ``Withheld`` — the KPI is not part
  of the deployed version, so it is withheld from every gateway/BI consumer.
- deployed and the KPI is pinned → ``ResolvedKpi`` carrying a detached KPI ORM
  instance built from the snapshot definition + live governance overlay, plus the
  ``(deployed_version_id, deploy_epoch)`` identity used for cache keying.

An UNDEPLOYED model has no serving authority: builder-only surfaces continue to
show the live draft (never reachable through the gateway). ``resolve_served_kpi``
returns ``Undeployed`` for that case so the caller can decide (the model builder
serves the live draft; a gateway/BI producer must not).

Bug-8384: the family-agnostic half of this contract (loading + validating the
deployed snapshot, indexing it by id, and building the detached
definition+governance instance) now lives in ``deploy_resolver_core`` and is
shared with ``named_set_deploy_resolver``. This module keeps only what is
KPI-specific: the governance field list and the KPI-typed public API.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import KPI, Model
from shared.deploy_resolver_core import (
    build_served_row,
    index_snapshot_rows,
    load_deployed_snapshot,
)

_SNAPSHOT_FAMILY = "kpis"

# Governance/lifecycle fields overlaid from the LIVE row (never from the
# snapshot). Kept in lockstep with the rehydrator's revert contract
# (``rehydrator._KPI_GOVERNANCE_FIELDS``) plus the identity columns a detached
# instance needs. Everything NOT in this set is a DEFINITION field sourced from
# the deployed snapshot.
_LIVE_GOVERNANCE_FIELDS: tuple[str, ...] = (
    "certification_status",
    "replacement_id",
    "owner_user_id",
    "is_deployed",
    "deployed_at",
    "snapshot_frequency",
    "snapshot_retention",
    "created_at",
    "updated_at",
    "created_by",
)

# Identity columns that must always come from the live row (a snapshot KPI dict
# carries the same id/model_id, but we anchor on the live row so a re-keyed
# import can never mis-bind).
_IDENTITY_FIELDS: tuple[str, ...] = ("id", "model_id")


class KpiSnapshotInvalidError(Exception):
    """Raised when a DEPLOYED model's snapshot cannot authorise KPI serving.

    Distinguishes DEPLOYED_SNAPSHOT_INVALID from UNDEPLOYED (F-013-05): a
    serving surface must fail closed rather than fall back to live drafts.
    """

    error_code = "DEPLOYED_SNAPSHOT_INVALID"


@dataclass(frozen=True)
class ResolvedKpi:
    """A KPI resolved from the deployed snapshot for serving."""

    kpi: KPI                      # detached ORM instance: snapshot def + live governance
    deployed_version_id: UUID
    deploy_epoch: int


@dataclass(frozen=True)
class Withheld:
    """The model is deployed but this KPI id is absent from the snapshot."""

    reason: str = "kpi_not_in_deployed_snapshot"


@dataclass(frozen=True)
class Undeployed:
    """The model has no deploy pointer — no serving authority exists."""


def build_served_kpi(snapshot_kpi: dict[str, Any], live_kpi: KPI) -> KPI:
    """Build a detached KPI ORM instance: snapshot definition + live governance.

    Definition fields are taken from ``snapshot_kpi`` (the deployed authority);
    governance/lifecycle + identity fields are taken from ``live_kpi``.
    """
    return build_served_row(
        KPI,
        snapshot_kpi,
        live_kpi,
        governance_fields=_LIVE_GOVERNANCE_FIELDS,
        identity_fields=_IDENTITY_FIELDS,
    )


async def _load_deployed_snapshot(
    db: AsyncSession, model: Model
) -> Optional[dict[str, Any]]:
    """Return the deployed snapshot dict, or None if the model is undeployed.

    Raises ``KpiSnapshotInvalidError`` when a DEPLOYED model's version row or
    snapshot is missing / empty / malformed (fail closed, never live fallback).
    """
    return await load_deployed_snapshot(
        db, model, family=_SNAPSHOT_FAMILY, error_cls=KpiSnapshotInvalidError,
    )


def _index_snapshot_kpis(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return index_snapshot_rows(snapshot, _SNAPSHOT_FAMILY)


async def resolve_served_kpi(
    db: AsyncSession, model: Model, live_kpi: KPI
) -> ResolvedKpi | Withheld | Undeployed:
    """Resolve the served definition for one KPI against the deployed snapshot.

    Raises ``KpiSnapshotInvalidError`` for a deployed model whose snapshot is
    invalid. Returns ``Undeployed`` when the model has no deploy pointer,
    ``Withheld`` when the KPI id is absent from the deployed snapshot, or a
    ``ResolvedKpi`` with the snapshot-pinned definition.
    """
    snapshot = await _load_deployed_snapshot(db, model)
    if snapshot is None:
        return Undeployed()
    indexed = _index_snapshot_kpis(snapshot)
    snap_kpi = indexed.get(str(live_kpi.id))
    if snap_kpi is None:
        return Withheld()
    served = build_served_kpi(snap_kpi, live_kpi)
    return ResolvedKpi(
        kpi=served,
        deployed_version_id=model.deployed_version_id,
        deploy_epoch=int(getattr(model, "deploy_epoch", 0) or 0),
    )


async def resolve_served_kpis(
    db: AsyncSession, model: Model, live_kpis: list[KPI]
) -> tuple[list[ResolvedKpi], list[UUID]]:
    """Resolve a batch of KPIs against the deployed snapshot.

    Returns (resolved, withheld_ids). For an undeployed model, ``resolved`` is
    empty and every KPI id is withheld from serving surfaces (the caller decides
    whether to serve the live draft — only the model builder may). Raises
    ``KpiSnapshotInvalidError`` for a deployed-but-invalid snapshot.
    """
    snapshot = await _load_deployed_snapshot(db, model)
    if snapshot is None:
        return [], [k.id for k in live_kpis]
    indexed = _index_snapshot_kpis(snapshot)
    version_id = model.deployed_version_id
    epoch = int(getattr(model, "deploy_epoch", 0) or 0)
    resolved: list[ResolvedKpi] = []
    withheld: list[UUID] = []
    for live in live_kpis:
        snap_kpi = indexed.get(str(live.id))
        if snap_kpi is None:
            withheld.append(live.id)
            continue
        resolved.append(
            ResolvedKpi(
                kpi=build_served_kpi(snap_kpi, live),
                deployed_version_id=version_id,
                deploy_epoch=epoch,
            )
        )
    return resolved, withheld


async def resolve_served_filter_metadata(
    db: AsyncSession, model: Model,
) -> tuple[dict[str, str], dict[str, str]] | None:
    """Return deployed dimension names and source types for request filters.

    Request slicers are part of the served definition, so a deployed model
    must resolve their ids from the same immutable snapshot as its KPIs.  The
    live ``dimensions`` rows are deliberately not consulted in this branch:
    an edited draft must not silently rename a deployed filter or turn an
    unknown id into an unsliced query.  ``None`` means the model is
    undeployed, in which case the builder-owned live draft remains the
    authority.
    """
    snapshot = await _load_deployed_snapshot(db, model)
    if snapshot is None:
        return None

    dimensions = snapshot.get("dimensions")
    if not isinstance(dimensions, list):
        raise KpiSnapshotInvalidError("The deployed dimension snapshot is malformed.")
    columns = snapshot.get("columns") or []
    if not isinstance(columns, list) or any(not isinstance(row, dict) for row in columns):
        raise KpiSnapshotInvalidError("The deployed column snapshot is malformed.")
    column_types = {
        str(row.get("id")): str(row.get("data_type") or "").lower()
        for row in columns
        if row.get("id") is not None
    }
    names: dict[str, str] = {}
    types: dict[str, str] = {}
    for row in dimensions:
        if not isinstance(row, dict) or not row.get("id") or not row.get("name"):
            raise KpiSnapshotInvalidError(
                "The deployed dimension snapshot contains an invalid row."
            )
        dimension_id = str(row["id"])
        names[dimension_id] = str(row["name"])
        source_column_id = row.get("source_column_id")
        if source_column_id is not None:
            types[dimension_id] = column_types.get(str(source_column_id), "")
    return names, types
