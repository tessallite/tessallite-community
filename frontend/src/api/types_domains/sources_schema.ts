// ---------------------------------------------------------------------------
// Sources / Targets
// ---------------------------------------------------------------------------
export interface SourceCreate {
  project_connection_id: string;
  source_type: string;
  display_name: string;
  default_schema?: string;
  config?: Record<string, unknown>;
}
export interface Source {
  id: string;
  model_id: string;
  project_connection_id: string;
  display_name: string;
  source_type: string;
  default_schema?: string | null;
  config: Record<string, unknown>;
}

// Bug-5920: backend-computed calendar type availability (a canonical type
// can require an optional runtime dependency not installed in this
// deployment, e.g. hijri-converter for "hijri"). Frontend must render its
// picker from this instead of a hardcoded per-type flag.
export interface CalendarTypeAvailability {
  calendar_type: string;
  available: boolean;
}

export interface CalendarTable {
  id: string;
  data_source_id: string;
  table_name: string;
  dialect: string;
  calendar_type?: string;
  date_column: string;
  year_column?: string | null;
  half_column?: string | null;
  quarter_column?: string | null;
  month_column?: string | null;
  week_column?: string | null;
  day_column?: string | null;
  autocreated: boolean;
  auto_created_aliases?: string[];
  history_provenance?: { token: string } | null;
}

export interface CalendarScriptRequest {
  dialect: string;
  table_name: string;
  start_date: string;
  end_date: string;
  calendar_type?: string;
  fiscal_year_start_month?: number;
}

export interface CalendarScriptResponse {
  ddl: string;
}

export interface CalendarAutoCreateRequest {
  table_name: string;
  start_date: string;
  end_date: string;
  alias?: string;
  display_name?: string;
  fiscal_year_start_month?: number;
  calendar_type?: string;
}

export interface CalendarBindRequest {
  table_name: string;
  dialect?: string;
  calendar_type?: string;
  date_column?: string;
  year_column?: string;
  half_column?: string;
  quarter_column?: string;
  month_column?: string;
  week_column?: string;
  day_column?: string;
  alias?: string;
  display_name?: string;
  fiscal_year_start_month?: number;
  history_provenance?: string;
}

export interface CalendarUpdateRequest {
  date_column?: string | null;
  year_column?: string | null;
  half_column?: string | null;
  quarter_column?: string | null;
  month_column?: string | null;
  week_column?: string | null;
  day_column?: string | null;
}
// F-016-23: fact-vs-calendar date-range coverage check result.
export interface CalendarCoverageResponse {
  covered: boolean;
  calendar_min?: string | null;
  calendar_max?: string | null;
  fact_min?: string | null;
  fact_max?: string | null;
  gap?: "below" | "above" | "both" | null;
  warning?: string | null;
}
export interface TargetCreate {
  project_connection_id: string;
  target_type: string;
  display_name: string;
  config?: Record<string, unknown>;
}
export interface Target {
  id: string;
  model_id: string;
  project_connection_id: string;
  display_name: string;
  target_type: string;
  config: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
}

// ---------------------------------------------------------------------------
// ModelTable / ModelColumn
// ---------------------------------------------------------------------------
// Bug-8930 / Bug-8876: no `source_id` and no `calendar_table_id` here.
// `source_id` is a path parameter on the create route; `calendar_table_id` is
// derived server-side and can only be set afterwards through
// `ModelTableUpdate` (PATCH), which is ownership-guarded.
export interface ModelTableCreate {
  table_type: "fact" | "dim_aggregate" | "dim_detail" | "unclassified" | "calendar";
  physical_name: string;
  alias?: string;
  display_name: string;
  description?: string | null;
}
export interface ModelTableUpdate {
  table_type?: "fact" | "dim_aggregate" | "dim_detail" | "unclassified" | "calendar";
  alias?: string;
  display_name?: string;
  description?: string | null;
  calendar_table_id?: string | null;
}
export interface ModelTable {
  id: string;
  model_id: string;
  source_id: string;
  table_type: string;
  physical_name: string;
  alias: string;
  display_name: string;
  description?: string | null;
  row_count_estimate: number | null;
  last_stats_at: string | null;
  calendar_table_id?: string | null;
  created_at: string;
  updated_at: string;
}

/** Model-open batch payload (Bug-9158): table metadata plus its attributes. */
export interface ModelTableWithAttributes {
  table: ModelTable;
  attributes: TableAttribute[];
}
export interface ModelColumn {
  id: string;
  column_name: string;
  display_name?: string | null;
  description?: string | null;
  is_hidden?: boolean;
  hidden_reason?: string | null;
  is_primary_key?: boolean;
  data_type: string;
  is_nullable: boolean;
}
export interface ModelColumnUpdate {
  display_name?: string | null;
  description?: string | null;
  is_hidden?: boolean;
  is_primary_key?: boolean;
}
export interface ColumnSuggestion {
  column_id: string;
  column_name: string;
  suggested_role: string;
  reason: string;
}
export interface MeasureWarning {
  column_id: string;
  column_name: string;
  current_role: string;
  suggested_role: string;
  severity: string;
  reason: string;
}
export interface TableAnalysis {
  table_id: string;
  suggested_table_type: string;
  confidence: string;
  reasoning: string;
  column_suggestions: ColumnSuggestion[];
  date_columns: string[];
  potential_calendar_column: string | null;
  measure_warnings?: MeasureWarning[];
}

export interface RenamePreviewItem {
  type: "dimension" | "measure";
  id: string;
  source_column_name: string;
  current_name: string;
  suggested_name: string;
}

export interface TableAttribute {
  kind: "physical" | "user_defined";
  id: string;
  table_id: string;
  name: string;
  display_name?: string | null;
  description?: string | null;
  is_hidden?: boolean;
  hidden_reason?: string | null;
  is_primary_key?: boolean;
  data_type: string;
  is_user_defined: boolean;
  is_generated?: boolean;
  expression?: string | null;
  validated: boolean | null;
  validation_error: string | null;
}
export interface DiscoveredColumn {
  column_name: string;
  data_type: string;
  is_nullable: boolean;
  /**
   * PRIMARY KEY membership read from the source catalogue (Bug-8618).
   * Absent when the catalogue read failed — that is "unknown", NOT "not a
   * key", and the sync endpoint leaves the stored flag alone for it.
   */
  is_primary_key?: boolean;
}

export interface TablePreviewResponse {
  columns: string[];
  rows: Record<string, unknown>[];
  page: number;
  page_size: number;
  has_more: boolean;
  total_rows: number | null;
}

// ---------------------------------------------------------------------------
// User-defined Attributes
// ---------------------------------------------------------------------------
export interface UserDefinedAttributeCreate {
  name: string;
  expression: string;
  output_data_type: "varchar" | "integer" | "numeric" | "date";
  description?: string;
}
export interface UserDefinedAttributeUpdate {
  name?: string;
  expression?: string;
  output_data_type?: "varchar" | "integer" | "numeric" | "date";
  description?: string;
}
export interface UserDefinedAttribute {
  id: string;
  table_id: string;
  model_id: string;
  name: string;
  expression: string;
  output_data_type: "varchar" | "integer" | "numeric" | "date";
  description: string | null;
  validated: boolean;
  validation_error: string | null;
  is_generated?: boolean;
  referenced_columns: string[];
  created_at: string;
  updated_at: string;
}
export interface UserDefinedAttributeValidateRequest {
  expression: string;
  output_data_type: "varchar" | "integer" | "numeric" | "date";
}
export interface UserDefinedAttributeValidateResponse {
  parse_valid: boolean;
  columns_resolved: boolean;
  referenced_columns: string[];
  unsupported_functions: string[];
  live_validation: {
    executed: boolean;
    success: boolean;
    error: string | null;
    sample_value: string | null;
  };
}
export interface UserDefinedAttributeFunctionOption {
  name: string;
  signature: string;
  template: string;
  description: string;
}

// ---------------------------------------------------------------------------
// Hierarchies
// ---------------------------------------------------------------------------
export type HierarchyDimensionKind = "time" | "geo" | "entity";
export type HierarchyTimeUnit =
  | "year"
  | "half"
  | "quarter"
  | "month"
  | "week"
  | "day"
  | "hour"
  | "none";
export type HierarchyTimeCalc =
  | "lag"
  | "parallel_period"
  | "period_to_date"
  | "range"
  | "moving_window";

export type CalendarType =
  | "standard"
  | "fiscal"
  | "iso_week"
  | "retail_445"
  | "hijri"
  | "thai_buddhist";
export interface HierarchyCreate {
  name: string;
  type: "explicit" | "date_embedded" | "segment";
  dimension_kind?: HierarchyDimensionKind | null;
  description?: string;
  calendar_type?: CalendarType | null;
  fiscal_year_start_month?: number | null;
}
export interface HierarchyUpdate {
  name?: string;
  type?: "explicit" | "date_embedded" | "segment";
  dimension_kind?: HierarchyDimensionKind | null;
  description?: string | null;
  calendar_type?: CalendarType | null;
  fiscal_year_start_month?: number | null;
}
export interface Hierarchy {
  id: string;
  model_id: string;
  name: string;
  type: "explicit" | "date_embedded" | "segment";
  dimension_kind: HierarchyDimensionKind | null;
  description: string | null;
  calendar_type: CalendarType | null;
  fiscal_year_start_month: number | null;
  level_count: number;
  level_names: string[];
  created_at: string;
  updated_at: string;
}
export interface HierarchyAttributeRef {
  id: string;
  name: string;
  table_id: string;
  table_name: string;
  data_type: string;
  source: "physical_column" | "user_defined_attribute";
}
export interface HierarchyLevelAttributeCreate {
  attribute_id: string;
  attribute_source: "physical_column" | "user_defined_attribute";
  role: "display" | "filter";
}
export interface HierarchyLevelCreate {
  name: string;
  ordinal: number;
  key_attribute_id: string;
  key_attribute_source: "physical_column" | "user_defined_attribute";
  description?: string;
  time_unit?: HierarchyTimeUnit | null;
  allowed_time_calcs?: HierarchyTimeCalc[];
  attributes?: HierarchyLevelAttributeCreate[];
}
export interface HierarchyLevelUpdate {
  name?: string;
  ordinal?: number;
  key_attribute_id?: string;
  key_attribute_source?: "physical_column" | "user_defined_attribute";
  description?: string | null;
  time_unit?: HierarchyTimeUnit | null;
  allowed_time_calcs?: HierarchyTimeCalc[];
  attributes?: HierarchyLevelAttributeCreate[];
}
export interface HierarchyLevel {
  id: string;
  name: string;
  ordinal: number;
  key_attribute: HierarchyAttributeRef;
  attributes: Array<{
    id: string;
    attribute: HierarchyAttributeRef;
    role: "display" | "filter";
  }>;
  description: string | null;
  time_unit: HierarchyTimeUnit | null;
  allowed_time_calcs: HierarchyTimeCalc[];
}
export interface HierarchyDetail {
  id: string;
  model_id: string;
  name: string;
  type: "explicit" | "date_embedded" | "segment";
  dimension_kind: HierarchyDimensionKind | null;
  description: string | null;
  calendar_type: CalendarType | null;
  fiscal_year_start_month: number | null;
  segment_config?: Record<string, unknown> | null;
  date_config?: Record<string, unknown> | null;
  levels: HierarchyLevel[];
  created_at: string;
  updated_at: string;
}
export interface HierarchyPreviewResponse {
  hierarchy_id: string;
  hierarchy_name: string;
  sample_size: number;
  warnings: Array<{ level_name: string; type: string; message: string }>;
  levels_summary: Array<{ ordinal: number; name: string; estimated_members?: number | null }>;
  members: Array<{
    level_ordinal: number;
    level_name: string;
    key_value: string;
    attributes: Record<string, string>;
    parent_key?: string | null;
    children_loaded: boolean;
    child_count_estimate?: number | null;
  }>;
}
export interface HierarchyReorderRequest {
  level_ids_in_order: string[];
}
export interface HierarchyGenerateDateRequest {
  name: string;
  source_attribute_id: string;
  source_attribute_source: "physical_column" | "user_defined_attribute";
  template: "y_m_d" | "y_q_m_d" | "y_h_q_m_d" | "y_w_d" | "y_m_w_d";
  description?: string;
  calendar_type?: CalendarType | null;
  fiscal_year_start_month?: number | null;
}
export interface HierarchyGenerateSegmentRequest {
  name: string;
  source_attribute_id: string;
  source_attribute_source: "physical_column" | "user_defined_attribute";
  mode: "delimiter" | "positional";
  delimiter?: string;
  levels?: Array<{ name: string }>;
  segments?: Array<{ name: string; start: number; length: number }>;
  description?: string;
}
export interface HierarchyGeneratedResponse {
  hierarchy: HierarchyDetail;
  generated_attributes: HierarchyAttributeRef[];
}
export interface UnassignedDateColumn {
  column_id: string;
  column_name: string;
  display_name: string | null;
  data_type: string;
  table_id: string;
  table_alias: string;
  is_uda?: boolean;
}
export interface HierarchyBatchDateRequest {
  grain: string;
  calendar_table_id: string;
  column_ids?: string[];
}
export interface HierarchyBatchDateSkipped {
  column_name: string;
  reason: string;
}
export interface HierarchyBatchDateResponse {
  created_hierarchies: number;
  created_aliases: number;
  skipped: HierarchyBatchDateSkipped[];
}

export interface GrainSuggestion {
  label: string;
  grain: string[];
  source: string;
  hierarchy_id: string;
}

export interface HierarchyHealthStatus {
  hierarchy_id: string;
  hierarchy_name: string;
  /**
   * Verdict of the checks that RAN — not of every check that exists. F-016-08:
   * a clean metadata check with no member probe reports `unverified_members`
   * (not `ok`), so the status field itself is honest that member integrity was
   * never looked at. `ok` is only reported when the probe ran and covered every
   * adjacent level pair.
   */
  status: "ok" | "warning" | "error" | "unverified_members";
  /**
   * Bug-8510: authoritative statement from the API that the live
   * member-integrity probe ran for this hierarchy AND scanned every adjacent
   * level pair. Only `status === "ok" && members_probed` is genuinely healthy.
   * Never re-derive this from the request, the caller's role, or the issue
   * list.
   *
   * Declared required because every supported backend sends it, but test for
   * it as `members_probed === true`, never as `!== false`: during a rolling
   * deploy the served build can be older than this contract and omit the
   * field, and "the server did not say" must read as not checked.
   */
  members_probed: boolean;
  issues: Array<{ issue_type: string; severity: string; detail: Record<string, unknown> }>;
}

// ---------------------------------------------------------------------------
// Data Quality
// ---------------------------------------------------------------------------

export type DataQualityRuleType = "not_null" | "unique" | "range" | "regex" | "custom_sql";
export type DataQualityTargetType = "dimension" | "measure" | "column";
export type DataQualitySeverity = "info" | "warn" | "error";

export interface DataQualityRule {
  id: string;
  model_id: string;
  name: string;
  target_type: DataQualityTargetType;
  target_id: string;
  rule_type: DataQualityRuleType;
  rule_config: Record<string, unknown> | null;
  severity: DataQualitySeverity;
  is_enabled: boolean;
  block_on_failure: boolean;
  last_checked_at: string | null;
  last_violation_count: number | null;
  created_at: string;
  updated_at: string;
}
export interface DataQualityRuleCreate {
  name: string;
  target_type: DataQualityTargetType;
  target_id: string;
  rule_type: DataQualityRuleType;
  rule_config?: Record<string, unknown> | null;
  severity?: DataQualitySeverity;
  is_enabled?: boolean;
  block_on_failure?: boolean;
}
export interface DataQualityRuleUpdate {
  name?: string;
  rule_config?: Record<string, unknown> | null;
  severity?: DataQualitySeverity;
  is_enabled?: boolean;
  block_on_failure?: boolean;
}
export interface DataQualityViolation {
  id: string;
  rule_id: string;
  detected_at: string;
  violation_count: number;
  sample_values: Record<string, unknown> | null;
  aggregate_id: string | null;
}
export interface DataQualityValidateResponse {
  rules_checked: number;
  violations_found: number;
  rule_results: Array<{ rule_id: string; rule_name: string; violation_count: number }>;
}
