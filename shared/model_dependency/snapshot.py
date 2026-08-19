"""Normalized dependency snapshot — the pure input contract (spec §6.2).

``ModelDependencySnapshot`` is an immutable, versioned input document containing
only the fields the edge catalogue (spec §5.3) needs. The model-service
``dependencies/loader.py`` produces it with a fixed number of batched queries; the
pure graph builder consumes it. Database row objects MUST NOT escape the loader
(spec §17), so every family here is a plain dataclass of scalars/IDs/JSON.

Every collection is ordered by stable object ID before it enters the graph, so
graph output is independent of database row order (spec §7.1 step 3, §13.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# --- Containment and physical layer ---------------------------------------


@dataclass(frozen=True)
class SourceRow:
    id: str
    name: str
    display_name: str
    project_connection_id: Optional[str]


@dataclass(frozen=True)
class TargetRow:
    id: str
    name: str
    display_name: str
    project_connection_id: Optional[str]


@dataclass(frozen=True)
class TableRow:
    id: str
    name: str
    display_name: str
    source_id: str
    # Set when this table is a calendar alias (ModelTable.calendar_table_id).
    calendar_table_id: Optional[str]


@dataclass(frozen=True)
class ColumnRow:
    id: str
    name: str
    display_name: str
    table_id: str


@dataclass(frozen=True)
class CalendarRow:
    id: str
    name: str
    display_name: str
    # Owning source (Data source -> calendar containment, spec §5.3). Optional
    # because a model-level calendar may not be source-bound.
    source_id: Optional[str] = None


# --- Attributes and semantic layer ----------------------------------------


@dataclass(frozen=True)
class UdaRow:
    id: str
    name: str
    display_name: str
    table_id: str
    # Persisted resolved column IDs (UserDefinedAttributeColumnRef).
    column_ref_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class DimensionRow:
    id: str
    name: str
    display_name: str
    source_column_id: Optional[str] = None
    display_column_id: Optional[str] = None
    user_defined_attribute_id: Optional[str] = None
    calc_expression: Optional[str] = None
    calc_expression_tables: tuple[str, ...] = ()
    # Calc-expression column references resolved to column IDs by the loader
    # (Column -> calculated dimension, hard/restrict, spec §5.3). Deleting a
    # column named only inside the expression must break the dimension.
    calc_expression_column_ids: tuple[str, ...] = ()
    hierarchy_json: Optional[dict[str, Any]] = None  # legacy field (§5.3 rule)


@dataclass(frozen=True)
class HierarchyRow:
    id: str
    name: str
    display_name: str


@dataclass(frozen=True)
class HierarchyLevelRow:
    id: str
    name: str
    display_name: str
    hierarchy_id: str
    key_attribute_id: str
    key_attribute_source: str  # physical_column | user_defined_attribute
    # (attribute_id, attribute_source, role) tuples for display/filter attrs.
    attributes: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class MeasureRow:
    id: str
    name: str
    display_name: str
    source_column_id: Optional[str] = None
    semi_additive_account_column_id: Optional[str] = None
    resolved_date_col_id: Optional[str] = None
    date_dimension_column_id: Optional[str] = None
    user_defined_attribute_id: Optional[str] = None
    calendar_model_table_id: Optional[str] = None
    hierarchy_id: Optional[str] = None
    resolved_calendar_id: Optional[str] = None
    variant_of_measure_id: Optional[str] = None
    cross_model_source_model_id: Optional[str] = None
    cross_model_source_measure_id: Optional[str] = None
    # Calculated-measure expression (parsed by the calc-measure extractor).
    calc_expression: Optional[str] = None
    expression_table_ids: tuple[str, ...] = ()
    # measure("name") references resolved to same-model measure IDs by the loader.
    calc_reference_ids: tuple[str, ...] = ()


# --- Relationships / accelerators -----------------------------------------


@dataclass(frozen=True)
class RelationshipRow:
    id: str
    name: str
    display_name: str
    left_table_id: str
    right_table_id: str
    left_column_id: Optional[str]
    right_column_id: Optional[str]


@dataclass(frozen=True)
class AggregateRow:
    id: str
    name: str
    display_name: str
    target_id: Optional[str] = None
    persona_id: Optional[str] = None
    # Grain dimension names resolved to dimension IDs by the loader.
    grain_dimension_ids: tuple[str, ...] = ()
    # IDs of aggregates this one depends on for refresh (RefreshDependency).
    refresh_dependency_ids: tuple[str, ...] = ()
    # §7.6: True only when losing a grain/measure dependency would leave this
    # aggregate serving stale results (no safe source fallback), keeping the
    # impact hard. Default False = automatic invalidation + safe fallback = soft.
    serves_when_stale: bool = False


@dataclass(frozen=True)
class AggregateColumnRow:
    id: str
    name: str
    display_name: str
    aggregate_id: str
    measure_id: Optional[str]


@dataclass(frozen=True)
class PocketRow:
    id: str
    name: str
    display_name: str
    target_id: Optional[str] = None
    persona_id: Optional[str] = None
    # Model object IDs referenced by the pocket's semantic SQL / predicates,
    # already resolved by the loader via the shared SQLGlot metadata extractor.
    referenced_dimension_ids: tuple[str, ...] = ()
    referenced_measure_ids: tuple[str, ...] = ()
    referenced_table_ids: tuple[str, ...] = ()


# --- Consumers -------------------------------------------------------------


@dataclass(frozen=True)
class KpiRow:
    id: str
    name: str
    display_name: str
    measure_ids: tuple[str, ...] = ()
    dimension_ids: tuple[str, ...] = ()
    time_dimension_id: Optional[str] = None
    referenced_kpi_ids: tuple[str, ...] = ()
    parent_kpi_id: Optional[str] = None
    replacement_kpi_id: Optional[str] = None


@dataclass(frozen=True)
class DrillThroughRow:
    id: str
    name: str
    display_name: str
    measure_id: Optional[str] = None
    source_table_id: Optional[str] = None
    detail_column_ids: tuple[str, ...] = ()
    joined_dimension_ids: tuple[str, ...] = ()
    join_path_relationship_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class NamedListRow:
    id: str
    name: str
    display_name: str
    dimension_ids: tuple[str, ...] = ()
    hierarchy_ids: tuple[str, ...] = ()
    measure_ids: tuple[str, ...] = ()
    replacement_id: Optional[str] = None


@dataclass(frozen=True)
class SavedQueryRow:
    id: str
    name: str
    display_name: str
    referenced_measure_ids: tuple[str, ...] = ()
    referenced_dimension_ids: tuple[str, ...] = ()
    referenced_named_list_ids: tuple[str, ...] = ()
    referenced_parameter_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SavedPivotRow:
    id: str
    name: str
    display_name: str
    measure_ids: tuple[str, ...] = ()
    row_dimension_ids: tuple[str, ...] = ()
    column_dimension_ids: tuple[str, ...] = ()
    referenced_named_list_ids: tuple[str, ...] = ()


# --- Security / classification / personas ----------------------------------


@dataclass(frozen=True)
class DataTagRow:
    id: str
    name: str
    display_name: str
    column_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PersonaRow:
    id: str
    name: str
    display_name: str
    # Included object IDs the persona restricts visibility over.
    included_dimension_ids: tuple[str, ...] = ()
    included_measure_ids: tuple[str, ...] = ()
    included_hierarchy_ids: tuple[str, ...] = ()
    # Dimension IDs named by Persona.default_filters JSON, resolved by the loader
    # (spec §5.3 "included-ID arrays AND default-filter JSON"). Deleting such a
    # dimension leaves a stale filter key; today the dimension-delete cleanup
    # (Bug-5607) strips it, so the graph must carry the soft/detach edge too.
    default_filter_dimension_ids: tuple[str, ...] = ()
    # Data-tag CLS restrictions (PersonaTagRestriction.data_tag_id).
    restricted_data_tag_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProjectPersonaScopeRow:
    id: str
    name: str
    display_name: str
    included_dimension_ids: tuple[str, ...] = ()
    included_measure_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RowSecurityRuleRow:
    id: str
    name: str
    display_name: str
    dimension_id: Optional[str] = None
    mapping_table_id: Optional[str] = None
    mapping_column_ids: tuple[str, ...] = ()


# --- Peripheral / soft-reference families ----------------------------------


@dataclass(frozen=True)
class ModelParameterRow:
    id: str
    name: str
    display_name: str


@dataclass(frozen=True)
class GlossaryAttachmentRow:
    id: str
    name: str
    display_name: str
    target_type: str
    target_id: str


@dataclass(frozen=True)
class DataQualityRuleRow:
    id: str
    name: str
    display_name: str
    target_type: str
    target_id: str
    block_on_failure: bool = False


@dataclass(frozen=True)
class ScratchpadMeasureRow:
    id: str
    name: str
    display_name: str
    calc_expression: Optional[str] = None
    # measure references resolved to measure IDs by the loader.
    reference_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class LineageMappingRow:
    id: str
    name: str
    display_name: str
    source_column_id: Optional[str] = None
    aggregate_col_id: Optional[str] = None


@dataclass(frozen=True)
class TranslationRow:
    id: str
    name: str
    display_name: str
    entity_type: str
    entity_id: str


@dataclass(frozen=True)
class UserPreferenceRow:
    id: str
    name: str
    display_name: str
    entity_type: str
    entity_id: str


@dataclass(frozen=True)
class AliasMapRow:
    id: str
    name: str
    display_name: str
    # Canonical-attribute object IDs the alias map resolves to.
    referenced_object_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CrossModelRecipeRow:
    id: str
    name: str
    display_name: str
    # Object IDs referenced by the recipe's steps/combine JSON.
    referenced_object_ids: tuple[str, ...] = ()
    referenced_model_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentGroundingRow:
    id: str
    name: str
    display_name: str
    # This grounding row references the whole model (soft / detach).


# --- The snapshot document -------------------------------------------------


@dataclass(frozen=True)
class ModelDependencySnapshot:
    """Immutable, versioned normalized input document (spec §6.2).

    ``cross_model_measures`` is the lightweight same-project reverse-reference
    index: measures in OTHER models of this project that reference measures in
    THIS model (spec §6.2 step 6, §12.3). Each is a ``MeasureRow`` carrying its
    own ``model_id`` via ``cross_model_measure_model_ids``.
    """

    tenant_id: str
    project_id: str
    model_id: str
    dependency_revision: int
    # Model.target_id — the model's default materialisation target (soft/detach).
    model_default_target_id: Optional[str] = None

    sources: tuple[SourceRow, ...] = ()
    targets: tuple[TargetRow, ...] = ()
    tables: tuple[TableRow, ...] = ()
    columns: tuple[ColumnRow, ...] = ()
    calendars: tuple[CalendarRow, ...] = ()
    udas: tuple[UdaRow, ...] = ()
    dimensions: tuple[DimensionRow, ...] = ()
    hierarchies: tuple[HierarchyRow, ...] = ()
    hierarchy_levels: tuple[HierarchyLevelRow, ...] = ()
    measures: tuple[MeasureRow, ...] = ()
    relationships: tuple[RelationshipRow, ...] = ()
    aggregates: tuple[AggregateRow, ...] = ()
    aggregate_columns: tuple[AggregateColumnRow, ...] = ()
    pockets: tuple[PocketRow, ...] = ()
    kpis: tuple[KpiRow, ...] = ()
    drill_through_sets: tuple[DrillThroughRow, ...] = ()
    named_lists: tuple[NamedListRow, ...] = ()
    saved_queries: tuple[SavedQueryRow, ...] = ()
    saved_pivots: tuple[SavedPivotRow, ...] = ()
    data_tags: tuple[DataTagRow, ...] = ()
    personas: tuple[PersonaRow, ...] = ()
    project_persona_scopes: tuple[ProjectPersonaScopeRow, ...] = ()
    row_security_rules: tuple[RowSecurityRuleRow, ...] = ()
    model_parameters: tuple[ModelParameterRow, ...] = ()
    glossary_attachments: tuple[GlossaryAttachmentRow, ...] = ()
    data_quality_rules: tuple[DataQualityRuleRow, ...] = ()
    scratchpad_measures: tuple[ScratchpadMeasureRow, ...] = ()
    lineage_mappings: tuple[LineageMappingRow, ...] = ()
    translations: tuple[TranslationRow, ...] = ()
    user_preferences: tuple[UserPreferenceRow, ...] = ()
    alias_maps: tuple[AliasMapRow, ...] = ()
    cross_model_recipes: tuple[CrossModelRecipeRow, ...] = ()
    agent_groundings: tuple[AgentGroundingRow, ...] = ()

    # Same-project cross-model reverse index (spec §12.3). Maps a measure in
    # another model (its own model_id) to the measure ID it references in THIS
    # model. Stored as (other_model_id, other_measure_id, other_measure_name,
    # referenced_measure_id).
    cross_model_measures: tuple[tuple[str, str, str, str], ...] = ()

    # Definition parse failures the loader could NOT resolve (spec §5.5, §12.7):
    # a KPI whose compiled expression is missing/unparseable, a pocket/named-list/
    # saved-query SQL/MDX parse failure. Each is (owner_object_type, owner_id,
    # field, reason_code). The builder emits an owner -> unresolved node so the
    # guard fails closed on a delete whose target could match the broken
    # definition, instead of treating the empty reference set as "no references".
    unresolved_definitions: tuple[tuple[str, str, str, str], ...] = ()

    # Navigation route templates keyed by object_type, used to build node routes
    # without hardcoding paths in the engine. e.g. {"measure": "/projects/{p}/..."}
    route_templates: dict[str, str] = field(default_factory=dict)
