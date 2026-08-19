"""Impact Analysis wire contract (Bug-7787, spec §9).

Derived consumer of ``shared/model_dependency/types.py``: every enum literal here
MUST match the engine enums (a contract test pins this). This is the separate
Impact-Analysis contract; it deliberately does NOT live in the downstream-usage
``governance_impact`` file (spec §9.5).
"""
from __future__ import annotations

import uuid
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# Mirror of engine literals (shared.model_dependency.types). Kept as string
# Literals so the OpenAPI schema and the TS types line up 1:1.
ObjectTypeStr = str  # closed enum validated against the engine at the API layer
SeverityStr = Literal["hard_break", "soft_degrade", "informational"]
EffectStr = Literal[
    "breaks_reference", "changes_semantics", "loses_coverage", "loses_visibility",
    "cascade_deleted", "detached", "stale", "cleanup",
]
DeletePolicyStr = Literal["restrict", "cascade", "detach", "invalidate", "recompute"]
OperationStr = Literal["inspect", "delete", "change"]
GuardDecisionStr = Literal[
    "allowed", "blocked", "acknowledgement_required", "blocked_unresolved",
]


# --- request ---------------------------------------------------------------


class ImpactTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object_type: str
    object_id: uuid.UUID


class ImpactChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    change_kind: Literal["rename", "rebind", "definition", "relationship", "classification"]
    changed_fields: list[str] = Field(default_factory=list)
    proposed_values: dict[str, object] = Field(default_factory=dict)


class ImpactQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: ImpactTarget
    operation: OperationStr
    change: Optional[ImpactChange] = None
    include_cross_model: bool = True


# --- response --------------------------------------------------------------


class ImpactObjectRef(BaseModel):
    object_type: str
    object_id: str
    model_id: str
    name: str
    display_name: str
    route: Optional[str] = None


class ImpactPathEdge(BaseModel):
    kind: str
    source_field: str


class ImpactPathModel(BaseModel):
    nodes: list[str]
    edges: list[ImpactPathEdge]


class ImpactItem(BaseModel):
    impact_id: str
    object: ImpactObjectRef
    severity: SeverityStr
    effect: EffectStr
    delete_policy: DeletePolicyStr
    direct: bool
    min_depth: int
    reason_key: str
    reason_params: dict[str, str] = Field(default_factory=dict)
    paths: list[ImpactPathModel] = Field(default_factory=list)
    scc_id: Optional[int] = None


class ImpactGuard(BaseModel):
    decision: GuardDecisionStr
    blocking_impact_ids: list[str] = Field(default_factory=list)
    acknowledgement_required: bool = False


class ImpactSummaryModel(BaseModel):
    total: int
    hard_break: int
    soft_degrade: int
    cascade_deleted: int
    direct: int
    max_depth: int
    by_object_type: dict[str, int] = Field(default_factory=dict)
    truncated: bool = False
    unresolved: int = 0


class ImpactResponse(BaseModel):
    analysis_id: str
    authority: Literal["live_draft"] = "live_draft"
    project_id: str
    model_id: str
    dependency_revision: int
    operation: OperationStr
    target: ImpactObjectRef
    guard: ImpactGuard
    summary: ImpactSummaryModel
    impacts: list[ImpactItem] = Field(default_factory=list)
    cycles: list[list[str]] = Field(default_factory=list)
    diagnostics: list[dict[str, str]] = Field(default_factory=list)


# --- catalogue (GET /objects) ----------------------------------------------


class ImpactCatalogueItem(BaseModel):
    object_type: str
    object_id: str
    model_id: str
    name: str
    display_name: str
    container_ids: dict[str, str] = Field(default_factory=dict)
    route: Optional[str] = None


class ImpactCatalogueResponse(BaseModel):
    project_id: str
    model_id: str
    dependency_revision: int
    total: int
    items: list[ImpactCatalogueItem] = Field(default_factory=list)
    next_cursor: Optional[str] = None


# --- guard conflict envelope (spec §9.4) -----------------------------------


class ModelDependencyConflict(BaseModel):
    code: Literal[
        "MODEL_DEPENDENCY_CONFLICT",
        "IMPACT_REVISION_STALE",
        "IMPACT_UNRESOLVED_REFERENCE",
        "IMPACT_ACKNOWLEDGEMENT_REQUIRED",
    ]
    message_key: str
    dependency_revision: int
    impact: Optional[ImpactResponse] = None


__all__ = [
    "ImpactTarget",
    "ImpactChange",
    "ImpactQueryRequest",
    "ImpactObjectRef",
    "ImpactPathEdge",
    "ImpactPathModel",
    "ImpactItem",
    "ImpactGuard",
    "ImpactSummaryModel",
    "ImpactResponse",
    "ImpactCatalogueItem",
    "ImpactCatalogueResponse",
    "ModelDependencyConflict",
]
