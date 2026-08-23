"""Pure, framework-free model dependency graph engine (Bug-7787).

This package derives a model-internal dependency graph from authoritative
metadata and answers "what-if" impact questions (delete / change previews) and
guard decisions. It contains NO FastAPI or SQLAlchemy imports: database loading,
authorization, locking, and HTTP live in the model-service ``dependencies`` and
``api`` packages. Bug-7788 (source drift) and future governance exporters consume
the same pure graph.

Authority: docs/strategy/strategy_model-impact-analysis.md.
"""

from .graph import DependencyGraph, GraphBuilder, build_graph
from .impact import inspect, simulate_delete
from .snapshot import ModelDependencySnapshot
from .structural_paths import (
    RelationshipImpact,
    relationship_change_impact,
    relationship_removal_impact,
)
from .types import (
    CONTRACT_VERSION,
    DependencyEdge,
    DependencyNode,
    DeletePolicy,
    Effect,
    EdgeKind,
    EdgeResolution,
    EdgeStrength,
    ImpactedObject,
    ImpactPath,
    ImpactResult,
    ImpactSummary,
    NodeKey,
    ObjectType,
    Operation,
    Severity,
)

__all__ = [
    "CONTRACT_VERSION",
    "DependencyEdge",
    "DependencyGraph",
    "DependencyNode",
    "DeletePolicy",
    "Effect",
    "EdgeKind",
    "EdgeResolution",
    "EdgeStrength",
    "GraphBuilder",
    "ImpactedObject",
    "ImpactPath",
    "ImpactResult",
    "ImpactSummary",
    "ModelDependencySnapshot",
    "NodeKey",
    "ObjectType",
    "Operation",
    "RelationshipImpact",
    "Severity",
    "build_graph",
    "inspect",
    "relationship_change_impact",
    "relationship_removal_impact",
    "simulate_delete",
]
