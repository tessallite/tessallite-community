"""The named ``retail_impact_fixture`` with stable UUIDs (spec §13.1).

Deterministic, hand-built ``ModelDependencySnapshot`` covering the three fixture
chains in the spec plus the edge families needed by the known-fixture guard
cases. All IDs are stable string tokens so tests can assert exact impacted sets.
"""

from __future__ import annotations

from shared.model_dependency.snapshot import (
    AggregateColumnRow,
    AggregateRow,
    CalendarRow,
    ColumnRow,
    DataTagRow,
    AgentGroundingRow,
    AliasMapRow,
    CrossModelRecipeRow,
    DataQualityRuleRow,
    DimensionRow,
    DrillThroughRow,
    GlossaryAttachmentRow,
    HierarchyLevelRow,
    HierarchyRow,
    KpiRow,
    LineageMappingRow,
    MeasureRow,
    ModelDependencySnapshot,
    ModelParameterRow,
    NamedListRow,
    PocketRow,
    UdaRow,
    PersonaRow,
    ProjectPersonaScopeRow,
    RelationshipRow,
    RowSecurityRuleRow,
    SavedPivotRow,
    SavedQueryRow,
    ScratchpadMeasureRow,
    SourceRow,
    TableRow,
    TargetRow,
    TranslationRow,
)

T = "tenant-1"
P = "project-1"
M = "model-1"

# Stable object IDs.
CONN = "conn-1"
SRC = "src-1"
TGT = "tgt-1"
CAL = "cal-1"
TB_ORDERS = "tb-orders"
TB_CUSTOMER = "tb-customer"
COL_GROSS = "col-gross"
COL_CUSTKEY = "col-custkey"
COL_CUSTKEY_DIM = "col-custkey-dim"  # customer table PK
MSR_GROSS = "msr-gross"       # Gross Sales (base)
MSR_NET = "msr-net"           # Net Sales (calculated, refs Gross Sales)
KPI_MARGIN = "kpi-margin"     # Margin (refs Net Sales)
DIM_CUSTOMER = "dim-customer"
HIER_CUST = "hier-cust"
LVL_CUST = "lvl-cust"         # keyed by customer dimension backing column
AGG_CUST = "agg-cust"         # Customer Grain aggregate (grain = Customer dim)
AGG_COL = "aggcol-1"
TAG_CONF = "tag-conf"         # Confidential Customer tag on COL_CUSTKEY
PERSONA_ANALYST = "persona-analyst"  # Restricted Analyst, CLS restriction on tag
REL_OC = "rel-oc"             # Orders-Customer relationship
REL_ALT = "rel-alt"           # optional alternate path relationship
NL_TOP10 = "nl-top10"         # Customer Top 10 named list (refs Customer dim)
DT_DETAIL = "dt-detail"       # Customer detail drill-through
PPS = "pps-1"                 # project persona scope including Customer dim
# Peripheral / soft-reference families (spec §13.1 minimum fixtures).
PARAM = "param-1"             # @region parameter referenced by a saved query
SQ = "sq-1"                   # saved query referencing Gross Sales + @region
SP = "sp-1"                   # saved pivot referencing Customer dim
RSR = "rsr-1"                 # row-security rule on Customer dimension
GLOSS = "gloss-1"             # glossary attachment on Gross Sales measure
DQR = "dqr-1"                 # data-quality rule on gross_amount column
SCRATCH = "scratch-1"         # scratchpad measure referencing Gross Sales
LINEAGE = "lineage-1"         # lineage mapping on gross_amount column
TRANS = "trans-1"             # translation on Customer dimension
ALIAS = "alias-1"            # alias map referencing Customer dimension
RECIPE = "recipe-1"          # cross-model recipe referencing Gross Sales
AGENT = "agent-1"            # agent grounding on the model
# Catalogue-completeness coverage rows (exercise every builder-emitted family).
UDA = "uda-1"                # UDA over gross_amount column
DIM_CALC = "dim-calc"        # calculated dimension over the orders table
MSR_TIME = "msr-time"        # measure with calendar/hierarchy/date bindings
MSR_VARIANT = "msr-variant"  # variant of Gross Sales
KPI_PARENT = "kpi-parent"    # parent KPI referenced by Margin
NL_OLD = "nl-old"            # deprecated named list replaced by NL_TOP10
AGG_REFRESH = "agg-refresh"  # aggregate depending on AGG_CUST for refresh
POCKET = "pocket-1"          # pocket referencing Customer dim, persona-scoped
COL_DATE = "col-date"        # date column on orders


def build_retail_snapshot(*, with_alternate_path: bool = False) -> ModelDependencySnapshot:
    relationships = [
        RelationshipRow(id=REL_OC, name="orders_customer", display_name="Orders-Customer",
                        left_table_id=TB_ORDERS, right_table_id=TB_CUSTOMER,
                        left_column_id=COL_CUSTKEY, right_column_id=COL_CUSTKEY_DIM),
    ]
    if with_alternate_path:
        relationships.append(
            RelationshipRow(id=REL_ALT, name="orders_customer_alt",
                            display_name="Orders-Customer Alt",
                            left_table_id=TB_ORDERS, right_table_id=TB_CUSTOMER,
                            left_column_id=COL_CUSTKEY, right_column_id=COL_CUSTKEY_DIM),
        )
    return ModelDependencySnapshot(
        tenant_id=T, project_id=P, model_id=M, dependency_revision=1,
        model_default_target_id=TGT,
        sources=(SourceRow(id=SRC, name="warehouse", display_name="Warehouse",
                           project_connection_id=CONN),),
        targets=(TargetRow(id=TGT, name="target", display_name="Target",
                           project_connection_id=CONN),),
        calendars=(CalendarRow(id=CAL, name="calendar", display_name="Calendar",
                               source_id=SRC),),
        tables=(
            TableRow(id=TB_ORDERS, name="orders", display_name="Orders",
                     source_id=SRC, calendar_table_id=None),
            TableRow(id=TB_CUSTOMER, name="customer", display_name="Customer",
                     source_id=SRC, calendar_table_id=CAL),
        ),
        columns=(
            ColumnRow(id=COL_GROSS, name="gross_amount", display_name="Gross amount",
                      table_id=TB_ORDERS),
            ColumnRow(id=COL_CUSTKEY, name="customer_key", display_name="Customer key",
                      table_id=TB_ORDERS),
            ColumnRow(id=COL_CUSTKEY_DIM, name="customer_id", display_name="Customer id",
                      table_id=TB_CUSTOMER),
            ColumnRow(id=COL_DATE, name="order_date", display_name="Order date",
                      table_id=TB_ORDERS),
        ),
        udas=(UdaRow(id=UDA, name="net_uda", display_name="Net UDA", table_id=TB_ORDERS,
                     column_ref_ids=(COL_GROSS,)),),
        measures=(
            MeasureRow(id=MSR_GROSS, name="Gross Sales", display_name="Gross Sales",
                       source_column_id=COL_GROSS),
            MeasureRow(id=MSR_NET, name="Net Sales", display_name="Net Sales",
                       calc_reference_ids=(MSR_GROSS,)),
            # Time measure exercising calendar/hierarchy/date bindings + cross-model.
            MeasureRow(id=MSR_TIME, name="Gross YTD", display_name="Gross YTD",
                       source_column_id=COL_GROSS, resolved_date_col_id=COL_DATE,
                       hierarchy_id=HIER_CUST, resolved_calendar_id=CAL),
            # Variant of Gross Sales (variant_measure edge).
            MeasureRow(id=MSR_VARIANT, name="Gross Sales PY", display_name="Gross Sales PY",
                       variant_of_measure_id=MSR_GROSS),
        ),
        dimensions=(
            DimensionRow(id=DIM_CUSTOMER, name="Customer", display_name="Customer",
                         source_column_id=COL_CUSTKEY_DIM),
            # Calculated dimension over the orders table referencing gross_amount
            # (calc_dimension_reference via BOTH table and column).
            DimensionRow(id=DIM_CALC, name="Order Bucket", display_name="Order Bucket",
                         calc_expression="CASE WHEN gross_amount > 0 THEN 'a' END",
                         calc_expression_tables=(TB_ORDERS,),
                         calc_expression_column_ids=(COL_GROSS,)),
        ),
        hierarchies=(HierarchyRow(id=HIER_CUST, name="Customer Hier",
                                  display_name="Customer Hierarchy"),),
        hierarchy_levels=(
            HierarchyLevelRow(id=LVL_CUST, name="Customer", display_name="Customer",
                              hierarchy_id=HIER_CUST, key_attribute_id=COL_CUSTKEY_DIM,
                              key_attribute_source="physical_column"),
        ),
        relationships=tuple(relationships),
        aggregates=(
            AggregateRow(id=AGG_CUST, name="Customer Grain", display_name="Customer Grain",
                         target_id=TGT, grain_dimension_ids=(DIM_CUSTOMER,)),
            # Aggregate depending on AGG_CUST for refresh (refresh_dependency edge),
            # persona-scoped (persona_aggregate_scope edge).
            AggregateRow(id=AGG_REFRESH, name="Refresh Agg", display_name="Refresh Agg",
                         target_id=TGT, refresh_dependency_ids=(AGG_CUST,),
                         persona_id=PERSONA_ANALYST),
        ),
        aggregate_columns=(
            AggregateColumnRow(id=AGG_COL, name="gross_agg", display_name="Gross Agg",
                               aggregate_id=AGG_CUST, measure_id=MSR_GROSS),
        ),
        pockets=(PocketRow(id=POCKET, name="Cust Pocket", display_name="Cust Pocket",
                           target_id=TGT, persona_id=PERSONA_ANALYST,
                           referenced_dimension_ids=(DIM_CUSTOMER,)),),
        kpis=(
            KpiRow(id=KPI_MARGIN, name="Margin", display_name="Margin",
                   measure_ids=(MSR_NET,), dimension_ids=(DIM_CUSTOMER,),
                   parent_kpi_id=KPI_PARENT),
            KpiRow(id=KPI_PARENT, name="Parent KPI", display_name="Parent KPI",
                   measure_ids=(MSR_NET,)),
        ),
        named_lists=(
            NamedListRow(id=NL_TOP10, name="Customer Top 10",
                         display_name="Customer Top 10",
                         dimension_ids=(DIM_CUSTOMER,)),
            # Deprecated list replaced by NL_TOP10 (named_list_replacement edge).
            NamedListRow(id=NL_OLD, name="Customer Top 5", display_name="Customer Top 5",
                         dimension_ids=(DIM_CUSTOMER,), replacement_id=NL_TOP10),
        ),
        drill_through_sets=(DrillThroughRow(id=DT_DETAIL, name="Customer detail",
                                            display_name="Customer detail",
                                            source_table_id=TB_CUSTOMER,
                                            joined_dimension_ids=(DIM_CUSTOMER,)),),
        data_tags=(DataTagRow(id=TAG_CONF, name="Confidential Customer",
                              display_name="Confidential Customer",
                              column_ids=(COL_CUSTKEY,)),),
        personas=(PersonaRow(id=PERSONA_ANALYST, name="Restricted Analyst",
                             display_name="Restricted Analyst",
                             included_dimension_ids=(DIM_CUSTOMER,),
                             default_filter_dimension_ids=(DIM_CUSTOMER,),
                             restricted_data_tag_ids=(TAG_CONF,)),),
        project_persona_scopes=(ProjectPersonaScopeRow(id=PPS, name="Scope",
                                display_name="Scope", included_dimension_ids=(DIM_CUSTOMER,)),),
        model_parameters=(ModelParameterRow(id=PARAM, name="region", display_name="Region"),),
        saved_queries=(SavedQueryRow(id=SQ, name="Region Sales", display_name="Region Sales",
                                     referenced_measure_ids=(MSR_GROSS,),
                                     referenced_named_list_ids=(NL_TOP10,),
                                     referenced_parameter_ids=(PARAM,)),),
        saved_pivots=(SavedPivotRow(id=SP, name="Customer Pivot", display_name="Customer Pivot",
                                    row_dimension_ids=(DIM_CUSTOMER,)),),
        row_security_rules=(RowSecurityRuleRow(id=RSR, name="Analyst RLS",
                                               display_name="Analyst RLS",
                                               dimension_id=DIM_CUSTOMER,
                                               mapping_table_id=TB_CUSTOMER,
                                               mapping_column_ids=(COL_CUSTKEY_DIM,)),),
        glossary_attachments=(GlossaryAttachmentRow(id=GLOSS, name="gloss",
                                                    display_name="Glossary",
                                                    target_type="measure", target_id=MSR_GROSS),),
        data_quality_rules=(DataQualityRuleRow(id=DQR, name="not_null",
                                               display_name="Not Null",
                                               target_type="column", target_id=COL_GROSS),),
        scratchpad_measures=(ScratchpadMeasureRow(id=SCRATCH, name="scratch",
                                                  display_name="Scratch",
                                                  reference_ids=(MSR_GROSS,)),),
        lineage_mappings=(LineageMappingRow(id=LINEAGE, name="lineage",
                                            display_name="Lineage",
                                            source_column_id=COL_GROSS),),
        translations=(TranslationRow(id=TRANS, name="trans", display_name="Translation",
                                     entity_type="dimension", entity_id=DIM_CUSTOMER),),
        alias_maps=(AliasMapRow(id=ALIAS, name="alias", display_name="Alias Map",
                                referenced_object_ids=(DIM_CUSTOMER,)),),
        cross_model_recipes=(CrossModelRecipeRow(id=RECIPE, name="recipe",
                                                 display_name="Recipe",
                                                 referenced_object_ids=(MSR_GROSS,),
                                                 referenced_model_ids=(M,)),),
        agent_groundings=(AgentGroundingRow(id=AGENT, name="agent", display_name="Agent"),),
        # Same-project measure in another model referencing THIS model's Gross
        # Sales (cross_model_measure_reference reverse index, spec §12.3).
        cross_model_measures=(("model-2", "m2-gross", "M2 Gross", MSR_GROSS),),
    )
