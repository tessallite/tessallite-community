"""The field surface the query-router will actually accept for a caller.

Bug-9897 / persona-layering rule 4, audit row A45.

The agent used to ground its prompt on a catalogue narrowed by the
agent-service ``ProjectPersonaModelScope`` field lists, while every query it
issues is enforced by the query-router against the model ``Persona`` resolved
from the caller's own JWT. Two policies over two different objects: the planner
could be told a measure exists, plan around it, and discover only at a 403 that
the executor never had it. The rule is that a catalogue and its executor cannot
disagree, because both derive from the same authority.

This module IS that authority, asked directly. ``GET
/api/v1/headless/models/{id}/measures`` and ``.../dimensions`` run
``resolve_execution_persona`` -- the same resolution the governed execute
endpoint performs -- apply the persona allow-list, and withhold every object whose column
closure reaches a column-level-security restricted column -- against the
DEPLOYED snapshot when the model is deployed, which is the shape the binder
binds. The answer they return is therefore the executor's own answer for this
identity, obtained with the caller's own bearer and nothing else.

Fail closed: when the verdict cannot be obtained the model is reported as
unavailable and the caller drops it from the grounding catalogue rather than
grounding on an unverified list.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx

from shared.config.settings import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# The metadata hop is a small, indexed read on the query-router. Kept well
# under the execute timeout: an unavailable verdict must fail closed quickly
# rather than stall prompt assembly.
_SURFACE_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class ExecutorModelSurface:
    """What the executor will accept for one model, for one identity."""

    model_id: UUID
    measure_names: frozenset[str]
    dimension_names: frozenset[str]


async def _fetch_names(
    client: httpx.AsyncClient,
    model_id: UUID,
    leaf: str,
    headers: dict[str, str],
) -> frozenset[str]:
    url = (
        f"{settings.QUERY_ROUTER_URL}/api/v1/headless/models/{model_id}/{leaf}"
    )
    resp = await client.get(url, headers=headers)
    resp.raise_for_status()
    payload: Any = resp.json()
    if not isinstance(payload, list):
        raise ValueError(
            f"query-router /{leaf} returned {type(payload).__name__}, not a list"
        )
    return frozenset(
        str(row["name"])
        for row in payload
        if isinstance(row, dict) and row.get("name")
    )


async def load_executor_surfaces(
    model_ids: list[UUID],
    jwt_token: str | None,
) -> dict[UUID, ExecutorModelSurface]:
    """Return the executor-accepted field surface per model, for this caller.

    A model is ABSENT from the result when its verdict could not be obtained
    (no bearer, transport failure, the router refusing the model). The caller
    must treat an absent model as ungroundable and drop it -- an unverified
    catalogue is exactly the drift this closes.
    """
    if not model_ids:
        return {}
    if not jwt_token:
        # No caller identity means no verdict, and a verdict is the whole
        # point: never fall back to the unnarrowed model surface.
        logger.warning(
            "Bug-9897: no bearer available to resolve the executor field "
            "surface; grounding %d model(s) as unavailable.", len(model_ids),
        )
        return {}
    headers = {"Authorization": f"Bearer {jwt_token}"}
    surfaces: dict[UUID, ExecutorModelSurface] = {}
    async with httpx.AsyncClient(timeout=_SURFACE_TIMEOUT_S) as client:
        results = await asyncio.gather(
            *(
                asyncio.gather(
                    _fetch_names(client, mid, "measures", headers),
                    _fetch_names(client, mid, "dimensions", headers),
                )
                for mid in model_ids
            ),
            return_exceptions=True,
        )
    for model_id, result in zip(model_ids, results):
        if isinstance(result, BaseException):
            logger.warning(
                "Bug-9897: could not resolve the executor field surface for "
                "model %s (%s); dropping it from the grounding catalogue.",
                model_id, result,
            )
            continue
        measures, dimensions = result
        surfaces[model_id] = ExecutorModelSurface(
            model_id=model_id,
            measure_names=measures,
            dimension_names=dimensions,
        )
    return surfaces
