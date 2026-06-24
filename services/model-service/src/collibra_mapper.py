"""Collibra mapper — converts GovernanceGraph → Collibra payloads.

Phase 4: Maps canonical governance nodes/edges into Collibra assets,
relations, and responsibilities suitable for the Collibra REST / Import API.
Type mappings are configurable but default to sensible industry-standard names.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from shared.model_snapshot.governance_graph import GovernanceGraph, GovernanceNode


# ---------------------------------------------------------------------------
# Default type mappings (configurable per-connection in future phases)
# ---------------------------------------------------------------------------

COLLIBRA_ASSET_TYPE_MAP: dict[str, str] = {
    "domain": "Data Domain",
    "semantic_model": "Semantic Model",
    "source_system": "System",
    "table": "Table",
    "column": "Column",
    "dimension": "Data Attribute",
    "measure": "Metric",
    "kpi": "KPI",
    "glossary_term": "Business Term",
    "downstream_asset": "Report",
    "aggregate": "Table",
    "data_tag": "Data Classification",
    "data_target": "Data Store",
}

COLLIBRA_RELATION_TYPE_MAP: dict[str, str] = {
    "contains": "contains",
    "contains_table": "contains",
    "contains_column": "contains",
    "defines_dimension": "is source of",
    "defines_measure": "is source of",
    "derived_from": "is calculated from",
    "governed_by_term": "is defined by",
    "produces_aggregate": "produces",
    "materialized_to": "is stored in",
    "consumed_by": "is consumed by",
    "feeds_semantic_field": "is source of",
    "uses_measure": "is based on",
    "classified_by": "is classified by",
}

COLLIBRA_STATUS_MAP: dict[str | None, str] = {
    "active": "Accepted",
    "deployed": "Accepted",
    "draft": "Candidate",
    "deprecated": "Deprecated",
    "hidden": "Candidate",
    "certified": "Accepted",
    "invalid": "Candidate",
}


# ---------------------------------------------------------------------------
# Payload types
# ---------------------------------------------------------------------------


@dataclass
class CollibraAsset:
    external_id: str
    name: str
    display_name: str | None = None
    asset_type: str = "Asset"
    domain_id: str = ""
    status: str | None = None
    attributes: dict = field(default_factory=dict)


@dataclass
class CollibraRelation:
    external_id: str
    source_external_id: str
    target_external_id: str
    relation_type: str
    attributes: dict = field(default_factory=dict)


@dataclass
class CollibraResponsibility:
    asset_external_id: str
    role: str
    user_or_group: str


@dataclass
class CollibraPayload:
    assets: list[CollibraAsset] = field(default_factory=list)
    relations: list[CollibraRelation] = field(default_factory=list)
    responsibilities: list[CollibraResponsibility] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Mapper
# ---------------------------------------------------------------------------


def _collibra_status(node: GovernanceNode) -> str | None:
    """Derive a Collibra lifecycle status from a governance node."""
    raw = node.status
    return COLLIBRA_STATUS_MAP.get(raw, raw)


def map_graph_to_collibra(
    graph: GovernanceGraph,
    *,
    domain_id: str = "",
    responsibility_role: str = "Business Owner",
    asset_type_mapping: dict[str, str] | None = None,
    relation_type_mapping: dict[str, str] | None = None,
    responsibility_mapping: dict[str, str] | None = None,
) -> CollibraPayload:
    """Convert a canonical GovernanceGraph to a Collibra payload."""

    effective_asset_type_mapping = {
        **COLLIBRA_ASSET_TYPE_MAP,
        **(asset_type_mapping or {}),
    }
    effective_relation_type_mapping = {
        **COLLIBRA_RELATION_TYPE_MAP,
        **(relation_type_mapping or {}),
    }
    effective_responsibility_role = (
        responsibility_mapping or {}
    ).get("owner", responsibility_role)

    assets = [
        CollibraAsset(
            external_id=node.stable_key,
            name=node.label,
            display_name=node.label,
            asset_type=effective_asset_type_mapping.get(node.object_type, node.object_type),
            domain_id=domain_id,
            status=_collibra_status(node),
            attributes={
                "Tessallite Object Type": node.object_type,
                "Tessallite Object ID": node.object_id,
                "Tessallite Stable Key": node.stable_key,
                "Description": node.description or "",
                **node.properties,
            },
        )
        for node in graph.nodes
    ]

    relations = [
        CollibraRelation(
            external_id=edge.stable_key,
            source_external_id=edge.source_key,
            target_external_id=edge.target_key,
            relation_type=effective_relation_type_mapping.get(
                edge.relationship_type, edge.relationship_type
            ),
            attributes={
                "Tessallite Relationship Type": edge.relationship_type,
                **edge.properties,
            },
        )
        for edge in graph.edges
    ]

    responsibilities: list[CollibraResponsibility] = []
    for node in graph.nodes:
        if node.owner:
            responsibilities.append(
                CollibraResponsibility(
                    asset_external_id=node.stable_key,
                    role=effective_responsibility_role,
                    user_or_group=node.owner,
                )
            )

    return CollibraPayload(
        assets=assets,
        relations=relations,
        responsibilities=responsibilities,
    )
