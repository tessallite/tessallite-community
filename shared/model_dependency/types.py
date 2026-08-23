"""Node/edge/result enums and immutable dataclasses (spec §5.1, §5.2, §9.3).

Everything here is pure data. Object/edge/severity/effect enums are the single
producer of the wire contract; ``shared/schemas/domains/model_impact.py`` and
``frontend/src/api/types_domains/model_impact.ts`` are derived consumers and MUST
stay aligned (spec §9.5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Optional

# Engine contract version. Part of ``analysis_id`` (spec §9.3). Bump on any
# change to node/edge enums or traversal semantics that affects results.
CONTRACT_VERSION = "impact-1"


class ObjectType(str, Enum):
    """Closed enum of graph node object types (spec §5.1)."""

    MODEL = "model"
    PROJECT_CONNECTION = "project_connection"
    DATA_SOURCE = "data_source"
    DATA_TARGET = "data_target"
    TABLE = "table"
    COLUMN = "column"
    USER_DEFINED_ATTRIBUTE = "user_defined_attribute"
    DIMENSION = "dimension"
    HIERARCHY = "hierarchy"
    HIERARCHY_LEVEL = "hierarchy_level"
    MEASURE = "measure"
    RELATIONSHIP = "relationship"
    CALENDAR = "calendar"
    AGGREGATE = "aggregate"
    AGGREGATE_COLUMN = "aggregate_column"
    POCKET = "pocket"
    NAMED_LIST = "named_list"
    KPI = "kpi"
    DRILL_THROUGH_SET = "drill_through_set"
    ROW_SECURITY_RULE = "row_security_rule"
    DATA_TAG = "data_tag"
    PERSONA = "persona"
    PROJECT_PERSONA_SCOPE = "project_persona_scope"
    SAVED_QUERY = "saved_query"
    SAVED_PIVOT_VIEW = "saved_pivot_view"
    MODEL_ALIAS_MAP = "model_alias_map"
    CROSS_MODEL_RECIPE = "cross_model_recipe"
    TRANSLATION = "translation"
    USER_PREFERENCE = "user_preference"
    MODEL_PARAMETER = "model_parameter"
    GLOSSARY_ATTACHMENT = "glossary_attachment"
    DATA_QUALITY_RULE = "data_quality_rule"
    SCRATCHPAD_MEASURE = "scratchpad_measure"
    LINEAGE_MAPPING = "lineage_mapping"
    AGENT_MODEL_LINK = "agent_model_link"
    UNRESOLVED_REFERENCE = "unresolved_reference"


class EdgeKind(str, Enum):
    """Named edge families. The value is a stable i18n/telemetry token."""

    CONTAINMENT = "containment"
    CONNECTION_BINDING = "connection_binding"
    TARGET_BINDING = "target_binding"
    CALENDAR_ALIAS_BINDING = "calendar_alias_binding"
    RELATIONSHIP_ENDPOINT = "relationship_endpoint"
    UDA_COLUMN_REFERENCE = "uda_column_reference"
    DIMENSION_BINDING = "dimension_binding"
    CALCULATED_DIMENSION_REFERENCE = "calculated_dimension_reference"
    MEASURE_BINDING = "measure_binding"
    HIERARCHY_MEASURE_BINDING = "hierarchy_measure_binding"
    CALENDAR_MEASURE_BINDING = "calendar_measure_binding"
    VARIANT_MEASURE = "variant_measure"
    CALCULATED_MEASURE_REFERENCE = "calculated_measure_reference"
    CROSS_MODEL_MEASURE_REFERENCE = "cross_model_measure_reference"
    HIERARCHY_LEVEL_ATTRIBUTE = "hierarchy_level_attribute"
    DIMENSION_LEVEL_ASSOCIATION = "dimension_level_association"
    AGGREGATE_GRAIN = "aggregate_grain"
    AGGREGATE_MEASURE = "aggregate_measure"
    PERSONA_AGGREGATE_SCOPE = "persona_aggregate_scope"
    REFRESH_DEPENDENCY = "refresh_dependency"
    POCKET_REFERENCE = "pocket_reference"
    RELATIONSHIP_PATH = "relationship_path"
    KPI_MEASURE_REFERENCE = "kpi_measure_reference"
    KPI_DIMENSION_REFERENCE = "kpi_dimension_reference"
    KPI_KPI_REFERENCE = "kpi_kpi_reference"
    DRILL_THROUGH_REFERENCE = "drill_through_reference"
    NAMED_LIST_REFERENCE = "named_list_reference"
    NAMED_LIST_SAVED_REFERENCE = "named_list_saved_reference"
    NAMED_LIST_REPLACEMENT = "named_list_replacement"
    SAVED_QUERY_REFERENCE = "saved_query_reference"
    SAVED_PIVOT_REFERENCE = "saved_pivot_reference"
    DATA_TAG_MEMBERSHIP = "data_tag_membership"
    DATA_TAG_RESTRICTION = "data_tag_restriction"
    ROW_SECURITY_BINDING = "row_security_binding"
    PERSONA_SCOPE = "persona_scope"
    PROJECT_PERSONA_SCOPE = "project_persona_scope_ref"
    TRANSLATION_REFERENCE = "translation_reference"
    ALIAS_MAP_REFERENCE = "alias_map_reference"
    CROSS_MODEL_RECIPE_REFERENCE = "cross_model_recipe_reference"
    GLOSSARY_ATTACHMENT_REFERENCE = "glossary_attachment_reference"
    DATA_QUALITY_REFERENCE = "data_quality_reference"
    SCRATCHPAD_MEASURE_REFERENCE = "scratchpad_measure_reference"
    LINEAGE_REFERENCE = "lineage_reference"
    AGENT_GROUNDING = "agent_grounding"
    MODEL_PARAMETER_REFERENCE = "model_parameter_reference"
    # An owner object carries a reference to an object that does not exist in the
    # snapshot (spec §5.5). The owner -> unresolved edge makes the diagnostic
    # reachable so the guard can fail closed.
    UNRESOLVED_REFERENCE = "unresolved_reference"


EdgeStrength = Literal["hard", "soft"]
DeletePolicy = Literal["restrict", "cascade", "detach", "invalidate", "recompute"]
Effect = Literal[
    "breaks_reference",
    "changes_semantics",
    "loses_coverage",
    "loses_visibility",
    "cascade_deleted",
    "detached",
    "stale",
    "cleanup",
]
EdgeResolution = Literal["foreign_key", "persisted_ref", "name", "json", "derived"]

# Result-level severity buckets shown to the user (spec §5.2). Cascade / detach /
# cleanup / invalidate remain ``effect``/``delete_policy`` values, not severities.
Severity = Literal["hard_break", "soft_degrade", "informational"]

Operation = Literal["inspect", "delete", "change"]


@dataclass(frozen=True)
class NodeKey:
    """Stable identity of a graph node (spec §5.1).

    ``(tenant_id, project_id, model_id, object_type, object_id)``. Hashable and
    order-comparable so adjacency maps and sorted lock ordering are deterministic.
    """

    tenant_id: str
    project_id: str
    model_id: str
    object_type: ObjectType
    object_id: str

    def token(self) -> str:
        """Compact ``type:object_id`` token used in witness paths (spec §9.3)."""
        return f"{self.object_type.value}:{self.object_id}"

    @property
    def sort_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.tenant_id,
            self.project_id,
            self.model_id,
            self.object_type.value,
            self.object_id,
        )


@dataclass(frozen=True)
class DependencyNode:
    """Immutable safe metadata for one graph node (spec §5.1).

    Contains ONLY safe metadata: key, names, container IDs, validity, route. No
    expressions, credentials, query text, or filter values.
    """

    key: NodeKey
    name: str
    display_name: str
    # Container IDs for UI grouping / navigation (table for a column, etc.).
    container_ids: dict[str, str] = field(default_factory=dict)
    valid: bool = True
    route: Optional[str] = None
    # For unresolved_reference nodes only: the reason code (spec §5.5).
    unresolved_reason: Optional[str] = None


@dataclass(frozen=True)
class DependencyEdge:
    """One directed dependency edge (spec §5.2).

    Points from the object depended on (``dependency``) to the ``dependent``.
    """

    dependency: NodeKey
    dependent: NodeKey
    kind: EdgeKind
    source_field: str
    strength: EdgeStrength
    delete_policy: DeletePolicy
    effect: Effect
    resolution: EdgeResolution
    # Field names and safe IDs only — never values or expressions.
    evidence: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ImpactPath:
    """One deterministic witness path from target to an impacted object."""

    nodes: tuple[str, ...]  # NodeKey tokens
    edges: tuple[DependencyEdge, ...]


@dataclass(frozen=True)
class ImpactedObject:
    """One impacted object in a what-if result (spec §9.3 ``impacts[]``)."""

    node: DependencyNode
    severity: Severity
    effect: Effect
    delete_policy: DeletePolicy
    direct: bool
    min_depth: int
    reason_key: str
    reason_params: dict[str, str]
    paths: tuple[ImpactPath, ...]
    scc_id: Optional[int] = None


@dataclass(frozen=True)
class ImpactSummary:
    """Aggregate counts over the FULL computed set (never truncated) (spec §9.3)."""

    total: int
    hard_break: int
    soft_degrade: int
    cascade_deleted: int
    direct: int
    max_depth: int
    by_object_type: dict[str, int]
    truncated: bool
    unresolved: int


@dataclass(frozen=True)
class ImpactResult:
    """Full what-if result. Pure; the API layer maps this to the wire contract."""

    operation: Operation
    target: DependencyNode
    impacts: tuple[ImpactedObject, ...]
    summary: ImpactSummary
    cycles: tuple[tuple[str, ...], ...]  # each cycle = tuple of node tokens
    diagnostics: tuple[dict[str, str], ...]
    # Objects removed by the owned cascade closure (delete only). Reported as
    # ``cascade_deleted`` effect, informational severity — not broken survivors.
    cascade_closure: tuple[NodeKey, ...] = ()
