"""The v1 edge catalogue (spec §5.3) as declarative edge specs.

Each entry describes ONE edge family: how to find (dependency, dependent) pairs
from the normalized snapshot and the fixed strength/policy/effect/resolution the
family carries. The graph builder (``graph.py``) iterates these so adding a new
metadata object is adding an extractor entry + fixture, not editing a central
``if`` nest (spec §6.1).

All strength/policy/effect values here are the source-of-truth encoding of the
catalogue table in spec §5.3. Severity that depends on runtime state (the §7.6
aggregate-invalidation rule) is resolved by the simulator, not baked here; the
catalogue records the DEFAULT edge strength and the simulator may relax it.
"""

from __future__ import annotations

from .types import EdgeKind, ObjectType

# Reason i18n keys per edge family (spec §9.3 reason_key). Consumed by the API to
# populate ``reason_key`` without emitting English sentences.
REASON_KEYS: dict[EdgeKind, str] = {
    EdgeKind.CONTAINMENT: "impactAnalysis.reason.containment",
    EdgeKind.CONNECTION_BINDING: "impactAnalysis.reason.connectionBinding",
    EdgeKind.TARGET_BINDING: "impactAnalysis.reason.targetBinding",
    EdgeKind.CALENDAR_ALIAS_BINDING: "impactAnalysis.reason.calendarAliasBinding",
    EdgeKind.RELATIONSHIP_ENDPOINT: "impactAnalysis.reason.relationshipEndpoint",
    EdgeKind.UDA_COLUMN_REFERENCE: "impactAnalysis.reason.udaColumnReference",
    EdgeKind.DIMENSION_BINDING: "impactAnalysis.reason.dimensionBinding",
    EdgeKind.CALCULATED_DIMENSION_REFERENCE: "impactAnalysis.reason.calcDimensionReference",
    EdgeKind.MEASURE_BINDING: "impactAnalysis.reason.measureBinding",
    EdgeKind.HIERARCHY_MEASURE_BINDING: "impactAnalysis.reason.hierarchyMeasureBinding",
    EdgeKind.CALENDAR_MEASURE_BINDING: "impactAnalysis.reason.calendarMeasureBinding",
    EdgeKind.VARIANT_MEASURE: "impactAnalysis.reason.variantMeasure",
    EdgeKind.CALCULATED_MEASURE_REFERENCE: "impactAnalysis.reason.expressionReference",
    EdgeKind.CROSS_MODEL_MEASURE_REFERENCE: "impactAnalysis.reason.crossModelMeasureReference",
    EdgeKind.HIERARCHY_LEVEL_ATTRIBUTE: "impactAnalysis.reason.hierarchyLevelAttribute",
    EdgeKind.DIMENSION_LEVEL_ASSOCIATION: "impactAnalysis.reason.dimensionLevelAssociation",
    EdgeKind.AGGREGATE_GRAIN: "impactAnalysis.reason.aggregateGrain",
    EdgeKind.AGGREGATE_MEASURE: "impactAnalysis.reason.aggregateMeasure",
    EdgeKind.PERSONA_AGGREGATE_SCOPE: "impactAnalysis.reason.personaAggregateScope",
    EdgeKind.REFRESH_DEPENDENCY: "impactAnalysis.reason.refreshDependency",
    EdgeKind.POCKET_REFERENCE: "impactAnalysis.reason.pocketReference",
    EdgeKind.RELATIONSHIP_PATH: "impactAnalysis.reason.relationshipPath",
    EdgeKind.KPI_MEASURE_REFERENCE: "impactAnalysis.reason.kpiMeasureReference",
    EdgeKind.KPI_DIMENSION_REFERENCE: "impactAnalysis.reason.kpiDimensionReference",
    EdgeKind.KPI_KPI_REFERENCE: "impactAnalysis.reason.kpiKpiReference",
    EdgeKind.DRILL_THROUGH_REFERENCE: "impactAnalysis.reason.drillThroughReference",
    EdgeKind.NAMED_LIST_REFERENCE: "impactAnalysis.reason.namedListReference",
    EdgeKind.NAMED_LIST_SAVED_REFERENCE: "impactAnalysis.reason.namedListSavedReference",
    EdgeKind.NAMED_LIST_REPLACEMENT: "impactAnalysis.reason.namedListReplacement",
    EdgeKind.SAVED_QUERY_REFERENCE: "impactAnalysis.reason.savedQueryReference",
    EdgeKind.SAVED_PIVOT_REFERENCE: "impactAnalysis.reason.savedPivotReference",
    EdgeKind.DATA_TAG_MEMBERSHIP: "impactAnalysis.reason.dataTagMembership",
    EdgeKind.DATA_TAG_RESTRICTION: "impactAnalysis.reason.dataTagRestriction",
    EdgeKind.ROW_SECURITY_BINDING: "impactAnalysis.reason.rowSecurityBinding",
    EdgeKind.PERSONA_SCOPE: "impactAnalysis.reason.personaScope",
    EdgeKind.PROJECT_PERSONA_SCOPE: "impactAnalysis.reason.projectPersonaScope",
    EdgeKind.TRANSLATION_REFERENCE: "impactAnalysis.reason.translationReference",
    EdgeKind.ALIAS_MAP_REFERENCE: "impactAnalysis.reason.aliasMapReference",
    EdgeKind.CROSS_MODEL_RECIPE_REFERENCE: "impactAnalysis.reason.crossModelRecipeReference",
    EdgeKind.GLOSSARY_ATTACHMENT_REFERENCE: "impactAnalysis.reason.glossaryAttachmentReference",
    EdgeKind.DATA_QUALITY_REFERENCE: "impactAnalysis.reason.dataQualityReference",
    EdgeKind.SCRATCHPAD_MEASURE_REFERENCE: "impactAnalysis.reason.scratchpadMeasureReference",
    EdgeKind.LINEAGE_REFERENCE: "impactAnalysis.reason.lineageReference",
    EdgeKind.AGENT_GROUNDING: "impactAnalysis.reason.agentGrounding",
    EdgeKind.MODEL_PARAMETER_REFERENCE: "impactAnalysis.reason.modelParameterReference",
    EdgeKind.UNRESOLVED_REFERENCE: "impactAnalysis.reason.unresolvedReference",
}


def reason_key(kind: EdgeKind) -> str:
    """i18n reason key for an edge family; falls back to a generic key."""
    return REASON_KEYS.get(kind, "impactAnalysis.reason.generic")


# Object types whose deletion the guard treats as security-critical: an
# unresolved or broken edge into these is always hard and fails closed (§7.6,
# §12.6). Used by the simulator/guard, declared here so the rule has one home.
SECURITY_OBJECT_TYPES: frozenset[ObjectType] = frozenset(
    {ObjectType.ROW_SECURITY_RULE, ObjectType.DATA_TAG, ObjectType.PERSONA}
)
