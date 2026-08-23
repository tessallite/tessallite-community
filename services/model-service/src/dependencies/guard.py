"""Unified impact guard for destructive model mutations (Bug-7787, Phase 3).

Evaluates the impact of a proposed delete against the model dependency graph
and returns a guard decision. Delete endpoints call ``evaluate_delete_impact``
BEFORE executing the delete. If the decision is ``blocked`` or
``blocked_unresolved``, the endpoint returns a 409 with the impact response
embedded in a ``ModelDependencyConflict`` envelope. If
``acknowledgement_required``, the endpoint checks for an acknowledged flag
and blocks without it.

This module is the single source of truth for the guard decision. It does NOT
replace post-delete executors (persona strip, soft-reference purge,
revalidation) — those run after the guard allows the action.

Read-only: this module NEVER writes and NEVER touches a source/target DB.
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from fastapi import HTTPException, Request, status

from shared.model_dependency.graph import build_graph
from shared.model_dependency.impact import simulate_delete
from shared.model_dependency.types import NodeKey, ObjectType
from shared.schemas.domains.model_impact import (
    ImpactGuard,
    ImpactItem,
    ImpactObjectRef,
    ImpactPathEdge,
    ImpactPathModel,
    ImpactResponse,
    ImpactSummaryModel,
    ModelDependencyConflict,
)
from src.dependencies.loader import ModelDependencyLoader

logger = logging.getLogger(__name__)

# Header the client sends to acknowledge a soft-degrade impact.
_ACK_HEADER = "X-Impact-Acknowledged"


async def evaluate_delete_impact(
    db,
    tenant_id: str,
    project_id: UUID,
    model_id: UUID,
    object_type: str,
    object_id: UUID,
    request: Optional[Request] = None,
    acknowledged: bool = False,
) -> Optional[ImpactResponse]:
    """Evaluate the impact of deleting the given object.

    Returns ``None`` if the delete is allowed (no blocking impacts and either
    no acknowledgement required or already acknowledged).

    Raises ``HTTPException(409)`` with a ``ModelDependencyConflict`` body if
    the delete is blocked or requires unacknowledged acknowledgement.

    The caller can also pass ``acknowledged=True`` explicitly (e.g. from a
    request body flag) instead of using the header.
    """
    loader = ModelDependencyLoader(db)
    snapshot = await loader.load(project_id, model_id)
    graph = build_graph(snapshot)

    target_key = NodeKey(
        tenant_id=str(snapshot.tenant_id),
        project_id=str(snapshot.project_id),
        model_id=str(snapshot.model_id),
        object_type=ObjectType(object_type),
        object_id=str(object_id),
    )

    target_node = graph.node(target_key)
    if target_node is None:
        # Object not in the dependency graph — no dependents, allow.
        return None

    from shared.config.settings import get_settings

    settings = get_settings()
    max_paths = settings.IMPACT_MAX_PATHS_PER_OBJECT
    max_display = settings.IMPACT_MAX_DISPLAY_IMPACTS

    result = simulate_delete(
        graph, target_key,
        max_paths_per_object=max_paths,
        max_display_impacts=max_display,
    )

    # Compute guard decision (mirrors impact_analysis.py _guard_decision).
    hard_ids: list[str] = []
    ack_needed = False
    has_unresolved = False
    for imp in result.impacts:
        if imp.node.key.object_type == ObjectType.UNRESOLVED_REFERENCE:
            has_unresolved = True
        if imp.severity == "hard_break":
            hard_ids.append(imp.node.key.token())
        elif imp.severity == "soft_degrade":
            ack_needed = True
        elif imp.effect in ("detached", "stale") or imp.delete_policy in (
            "detach", "invalidate", "recompute"
        ):
            if imp.effect not in ("cascade_deleted", "cleanup"):
                ack_needed = True

    if not hard_ids and not ack_needed:
        # No blocking impacts and no acknowledgement needed — allow.
        return None

    # Build the wire-contract response.
    response = _build_response(result, snapshot, str(project_id), str(model_id))

    if hard_ids:
        all_unresolved = all(
            imp.node.key.object_type == ObjectType.UNRESOLVED_REFERENCE
            for imp in result.impacts
            if imp.node.key.token() in set(hard_ids)
        )
        decision = "blocked_unresolved" if has_unresolved and all_unresolved else "blocked"
        response.guard = ImpactGuard(
            decision=decision,
            blocking_impact_ids=hard_ids,
            acknowledgement_required=False,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ModelDependencyConflict(
                code="MODEL_DEPENDENCY_CONFLICT",
                message_key="impactGuard.blocked",
                dependency_revision=snapshot.dependency_revision,
                impact=response,
            ).model_dump(mode="json"),
        )

    # Soft impacts — check for acknowledgement.
    if ack_needed:
        is_acked = acknowledged
        if not is_acked and request is not None:
            is_acked = request.headers.get(_ACK_HEADER, "").lower() in ("true", "1", "yes")
        if is_acked:
            return None  # Acknowledged, proceed.

        response.guard = ImpactGuard(
            decision="acknowledgement_required",
            blocking_impact_ids=[],
            acknowledgement_required=True,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ModelDependencyConflict(
                code="IMPACT_ACKNOWLEDGEMENT_REQUIRED",
                message_key="impactGuard.acknowledgementRequired",
                dependency_revision=snapshot.dependency_revision,
                impact=response,
            ).model_dump(mode="json"),
        )

    return None


def _build_response(
    result, snapshot, project_id: str, model_id: str,
) -> ImpactResponse:
    """Map engine ImpactResult to the wire contract ImpactResponse."""

    def _ref(node) -> ImpactObjectRef:
        return ImpactObjectRef(
            object_type=node.key.object_type.value,
            object_id=node.key.object_id,
            model_id=node.key.model_id,
            name=node.name,
            display_name=node.display_name,
            route=node.route,
        )

    impacts = [
        ImpactItem(
            impact_id=imp.node.key.token(),
            object=_ref(imp.node),
            severity=imp.severity,
            effect=imp.effect,
            delete_policy=imp.delete_policy,
            direct=imp.direct,
            min_depth=imp.min_depth,
            reason_key=imp.reason_key,
            reason_params=dict(imp.reason_params),
            paths=[
                ImpactPathModel(
                    nodes=list(p.nodes),
                    edges=[
                        ImpactPathEdge(kind=e.kind.value, source_field=e.source_field)
                        for e in p.edges
                    ],
                )
                for p in imp.paths
            ],
            scc_id=imp.scc_id,
        )
        for imp in result.impacts
    ]
    summary = ImpactSummaryModel(
        total=result.summary.total,
        hard_break=result.summary.hard_break,
        soft_degrade=result.summary.soft_degrade,
        cascade_deleted=result.summary.cascade_deleted,
        direct=result.summary.direct,
        max_depth=result.summary.max_depth,
        by_object_type=dict(result.summary.by_object_type),
        truncated=result.summary.truncated,
        unresolved=result.summary.unresolved,
    )
    target_ref = _ref(result.target)
    return ImpactResponse(
        analysis_id="",
        authority="live_draft",
        project_id=project_id,
        model_id=model_id,
        dependency_revision=snapshot.dependency_revision,
        operation=result.operation,
        target=target_ref,
        guard=ImpactGuard(decision="allowed", blocking_impact_ids=[], acknowledgement_required=False),
        summary=summary,
        impacts=impacts,
        cycles=[list(c) for c in result.cycles],
        diagnostics=[dict(d) for d in result.diagnostics],
    )
