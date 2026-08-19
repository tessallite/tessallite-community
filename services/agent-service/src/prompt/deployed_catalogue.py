"""Deployed-snapshot authority for the agent's prompt catalogue (Bug-8712).

The conversational agent is a CONSUMPTION surface. Its prompt lists the KPIs and
named sets a user may ask about, and the LLM picks tool arguments from that list,
so an entry in this catalogue is a promise the rest of the system has to keep.

The prompt assembler builds the catalogue by reading the tenant database
directly, which meant it read LIVE rows: a modeller renaming a named set, or
rewriting its description, changed what every user's agent saw with no Deploy —
and a set created since the last deploy was advertised to the model even though
the preview route (now `deployed_only`) will withhold it, so every tool call the
LLM made against it was guaranteed to fail.

The governing contract is not new. `architecture_model-versioning-and-deploy.md`:
"the deployed snapshot is the contract; the live state is editor-only" (F-013-01),
made concrete for these two families in
`architecture_kpi-deploy-serving-authority.md`.

This module is a thin ADAPTER over ``shared.deploy_resolver_core`` — the same
snapshot load, the same index, the same fail-closed rules the model-service
resolvers use. It is deliberately not a third hand-written copy of that logic:
the core was extracted (Bug-8384) precisely because copies drift, and a copy of
a fail-closed serving path drifts in the direction of serving.

What it pins, and what it does not:

- **Definition fields** (name, display_name, description) come from the deployed
  snapshot. They are what the prompt renders and what the LLM matches a user's
  words against.
- **Governance** (certification_status) stays LIVE, exactly as the model-service
  resolvers overlay it, so deprecating an entity takes effect without a redeploy.
- **Membership follows the live rows** for the same reason the sibling resolvers
  do: an entity absent from the snapshot is withheld, but a deleted live row
  disappears immediately. That is the known open gap Bug-8711 (recorded as "AKA
  source Bug-8753"); this module neither closes nor widens it.

Fail-closed behaviour differs from an HTTP route in FORM only, not in direction.
A route raises 409; a prompt has no error channel to the user, so an unreadable
deployed version yields an EMPTY catalogue plus an operator-visible warning. The
one thing it must never do — serve the live draft — is what both do.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Protocol, TypeVar
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Model
from shared.deploy_resolver_core import index_snapshot_rows, load_deployed_snapshot

logger = logging.getLogger(__name__)


class CatalogueSnapshotInvalidError(Exception):
    """A deployed model whose snapshot cannot authorise catalogue serving.

    Mirrors ``KpiSnapshotInvalidError`` / ``NamedSetSnapshotInvalidError``; the
    agent has no HTTP status to return, so it withholds instead of raising.
    """

    error_code = "DEPLOYED_SNAPSHOT_INVALID"


class _CatalogueRow(Protocol):
    """The shape the prompt catalogue entries share."""

    id: UUID
    name: str
    display_name: str | None
    description: str | None


RowT = TypeVar("RowT", bound=_CatalogueRow)

# Definition fields served from the deployed snapshot. `certification_status` is
# deliberately absent: it is governance and stays live (see module docstring).
_PINNED_DEFINITION_FIELDS: tuple[str, ...] = ("name", "display_name", "description")


async def pin_catalogue_to_deployed(
    db: AsyncSession,
    model: Model,
    *,
    family: str,
    rows: list[RowT],
) -> list[RowT]:
    """Return the rows the DEPLOYED model publishes, with pinned definitions.

    ``family`` is the snapshot key (``kpis`` / ``named_sets``). A row absent from
    the deployed snapshot is withheld. An undeployed model has no serving
    authority at all, so its catalogue is empty for this surface. An invalid
    snapshot withholds everything and logs — never a live fallback.

    Ordering is re-derived after pinning: the caller sorts by the LIVE name for
    cache-prefix byte-stability, and a draft rename would otherwise leak into the
    prompt through the ORDER alone even with every rendered name pinned.
    """
    if not rows:
        return []
    try:
        snapshot = await load_deployed_snapshot(
            db, model, family=family, error_cls=CatalogueSnapshotInvalidError,
        )
    except CatalogueSnapshotInvalidError as exc:
        logger.warning(
            "%s withheld from the agent catalogue for model %s: %s (%s). Deploy "
            "the model again to restore them.",
            family, model.id, exc, CatalogueSnapshotInvalidError.error_code,
        )
        return []
    if snapshot is None:
        logger.info(
            "%s withheld from the agent catalogue for model %s: the model has no "
            "deployed version, so there is no published definition to serve.",
            family, model.id,
        )
        return []

    indexed: dict[str, dict[str, Any]] = index_snapshot_rows(snapshot, family)
    served: list[RowT] = []
    withheld: list[str] = []
    for row in rows:
        snap_row = indexed.get(str(row.id))
        if snap_row is None:
            withheld.append(str(row.id))
            continue
        served.append(
            replace(
                row,
                **{
                    field: snap_row.get(field, getattr(row, field))
                    for field in _PINNED_DEFINITION_FIELDS
                },
            )
        )
    if withheld:
        logger.info(
            "%s withheld from the agent catalogue for model %s: %s not present in "
            "deployed version %s (Deploy the model to publish them).",
            family, model.id, withheld, model.deployed_version_id,
        )
    served.sort(key=lambda row: ((row.name or "").casefold(), str(row.id)))
    return served
