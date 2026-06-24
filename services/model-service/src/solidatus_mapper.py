"""Solidatus mapper — converts GovernanceGraph → Solidatus payloads.

Phase 4: Maps canonical governance nodes/edges into Solidatus node/edge
payloads suitable for the Solidatus API. Type mappings are configurable
but default to sensible industry-standard names.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from shared.model_snapshot.governance_graph import GovernanceGraph


# ---------------------------------------------------------------------------
# Default type mapping (configurable per-connection in future phases)
# ---------------------------------------------------------------------------

SOLIDATUS_TYPE_MAP: dict[str, str] = {
    "domain": "Business Domain",
    "semantic_model": "Semantic Model",
    "source_system": "Source System",
    "table": "Table",
    "column": "Column",
    "dimension": "Dimension",
    "measure": "Metric",
    "kpi": "KPI",
    "glossary_term": "Business Term",
    "downstream_asset": "Consumer Asset",
    "aggregate": "Materialized Aggregate",
    "data_tag": "Governance Tag",
    "data_target": "Data Store",
}

SOLIDATUS_EDGE_MAP: dict[str, str] = {
    "contains": "contains",
    "contains_table": "contains",
    "contains_column": "contains",
    "defines_dimension": "defines",
    "defines_measure": "defines",
    "derived_from": "derived_from",
    "governed_by_term": "governed_by",
    "produces_aggregate": "produces",
    "materialized_to": "stored_in",
    "consumed_by": "consumed_by",
    "feeds_semantic_field": "feeds",
    "uses_measure": "uses",
    "classified_by": "classifies",
}


# ---------------------------------------------------------------------------
# Payload types
# ---------------------------------------------------------------------------


@dataclass
class SolidatusNode:
    external_id: str
    type: str
    name: str
    description: str | None = None
    properties: dict = field(default_factory=dict)


@dataclass
class SolidatusEdge:
    external_id: str
    source_external_id: str
    target_external_id: str
    type: str
    properties: dict = field(default_factory=dict)


@dataclass
class SolidatusPayload:
    nodes: list[SolidatusNode] = field(default_factory=list)
    edges: list[SolidatusEdge] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Mapper
# ---------------------------------------------------------------------------


def map_graph_to_solidatus(graph: GovernanceGraph) -> SolidatusPayload:
    """Convert a canonical GovernanceGraph to a Solidatus API payload."""
    nodes = [
        SolidatusNode(
            external_id=node.stable_key,
            type=SOLIDATUS_TYPE_MAP.get(node.object_type, node.object_type),
            name=node.label,
            description=node.description,
            properties={
                "tessallite_object_type": node.object_type,
                "tessallite_object_id": node.object_id,
                "tessallite_stable_key": node.stable_key,
                **node.properties,
            },
        )
        for node in graph.nodes
    ]

    edges = [
        SolidatusEdge(
            external_id=edge.stable_key,
            source_external_id=edge.source_key,
            target_external_id=edge.target_key,
            type=SOLIDATUS_EDGE_MAP.get(edge.relationship_type, edge.relationship_type),
            properties={
                "tessallite_relationship_type": edge.relationship_type,
                **edge.properties,
            },
        )
        for edge in graph.edges
    ]

    return SolidatusPayload(nodes=nodes, edges=edges)
