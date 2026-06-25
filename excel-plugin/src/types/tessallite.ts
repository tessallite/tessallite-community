/**
 * Tessallite API type definitions.
 */

export type Zone = 'filters' | 'columns' | 'values' | 'rows';

export interface LoginRequest {
  tenant_id: string;
  email: string;
  password: string;
}

export interface LoginResponse {
  access_token?: string;
  token_type?: string;
}

export interface UserInfo {
  id: string;
  email: string;
  name: string;
  tenant_id: string;
}

export interface Project {
  id: string;
  name: string;
  slug: string;
}

export interface Model {
  id: string;
  name: string;
  slug: string;
  description?: string;
  deployed_version_id?: string;
  deployed: boolean;
}

export interface Measure {
  id: string;
  name: string;
  display_name: string;
  description?: string;
  effective_description?: string;
  default_agg: string;
  format?: string;
  measure_type: 'standard' | 'calculated' | 'variant';
  display_folder?: string;
  base_measure_id?: string;
  semi_additive_behavior?: string;
  cross_model_source?: string;
}

export interface Dimension {
  id: string;
  name: string;
  display_name: string;
  description?: string;
  effective_description?: string;
  data_type: string;
  source_type: 'dim' | 'calculated';
  is_time_dimension?: boolean;
  calendar_type?: string;
}

export interface Hierarchy {
  id: string;
  name: string;
  display_name?: string;
  type: 'date' | 'explicit' | 'segment';
  levels: HierarchyLevel[];
}

export interface HierarchyLevel {
  name: string;
  level_number: number;
  time_unit?: string;
  /**
   * Technical name of the level's key-attribute dimension (e.g.
   * `business_date_month`). This is the bindable dimension the query builder
   * uses when a level is dropped into a zone (F-025-11). Populated from the
   * hierarchy detail endpoint; the summary list endpoint does not carry it.
   */
  dimensionName?: string;
}

export interface Kpi {
  id: string;
  name: string;
  display_name: string | null;
  description: string | null;
  display_folder: string | null;
  value_measure_id: string | null;
  goal_measure_id: string | null;
  // Target (goal) definition. A KPI's goal is either a goal measure
  // (legacy `goal_measure_id`) or a v2 target: a static numeric value, or a
  // measure/expression. The KPI tab uses these to populate the Goal column
  // for static-target KPIs that have no goal measure (F-025-10).
  target_type: string | null;
  target_value: number | null;
  status_expression: string | null;
  trend_expression: string | null;
  status_graphic: string;
  trend_graphic: string;
  weight: number | null;
  parent_kpi_id: string | null;
  certification_status: string;
  replacement_id: string | null;
  owner_user_id: string | null;
  updated_at: string;
}

export interface KpiEvaluateResponse {
  value: number | null;
  goal: number | null;
  status: number | null;
  trend: number | null;
  status_label: string | null;
  trend_label: string | null;
  formatted_value: string | null;
  formatted_goal: string | null;
}

export interface KpiBatchResult {
  kpi_id: string;
  value: number | null;
  goal: number | null;
  status: number | null;
  trend: number | null;
  status_label: string | null;
  trend_label: string | null;
  formatted_value: string | null;
  formatted_goal: string | null;
}

/**
 * Canonical envelope returned by POST .../kpis/evaluate-batch.
 * Mirrors the backend `KPIBatchResponse` (governance_advanced.py): a
 * `results` list of per-KPI evaluations plus a total `evaluation_ms`.
 * Each result is a `KPIEvaluateResponse`; KpiBatchResult names the subset
 * the KPI tab consumes (value/goal/status/trend + formatted + labels —
 * `goal`/`formatted_goal` are the backend's populated legacy aliases of
 * `target`/`formatted_target`).
 */
export interface KpiBatchResponse {
  results: KpiBatchResult[];
  evaluation_ms: number | null;
}

export interface NamedSet {
  id: string;
  name: string;
  display_name: string | null;
  description: string | null;
  display_folder: string | null;
  scope: number;
  expression: string;
  dimensions: string | null;
  builder_definition: Record<string, unknown> | null;
  list_type: string | null;
  certification_status: string;
  replacement_id: string | null;
  owner_user_id: string | null;
  updated_at: string;
}

export interface NamedSetPreviewItem {
  ordinal: number;
  caption: string;
  key: string;
}

export interface NamedSetPreviewResponse {
  items: NamedSetPreviewItem[];
  total_count: number;
  truncated: boolean;
  explanation: string | null;
}

export interface Persona {
  id: string;
  name: string;
  slug: string;
  description?: string;
  audience?: string;
  included_measure_ids: string[];
  included_dimension_ids: string[];
  included_hierarchy_ids: string[];
}

export interface GlossaryEntry {
  id: string;
  term: string;
  definition: string;
  context_notes?: string;
  source: 'user' | 'llm' | 'llm_approved';
  status: 'approved' | 'pending' | 'rejected';
  synonyms: string[];
  sample_values?: string[];
}

export interface SemanticQuery {
  measures?: string[];
  dimensions?: string[];
  timeDimensions?: TimeDimension[];
  filters?: QueryFilter[];
  segments?: string[];
  order?: Record<string, 'asc' | 'desc'>;
  limit?: number;
  offset?: number;
  timezone?: string;
}

export interface TimeDimension {
  dimension: string;
  granularity?: string;
  dateRange?: string[];
}

export interface QueryFilter {
  member: string;
  operator: string;
  values?: string[];
}

export interface PluginRouteTrace {
  route_type: string;
  reason: string;
  aggregate_id?: string | null;
  pocket_id?: string | null;
  rewritten_query?: string | null;
}

export interface ExecuteResponse {
  query: SemanticQuery;
  data: Record<string, unknown>[];
  annotation?: {
    measures: Record<string, { title: string; type: string; format?: string }>;
    dimensions: Record<string, { title: string; type: string }>;
    timeDimensions: Record<string, { title: string; type: string }>;
  };
  // F-025-20: route decision (aggregate/pocket/source + rewritten SQL) for the
  // Query Trace modal.
  route?: PluginRouteTrace | null;
}

export interface AgentConfig {
  configured?: boolean;
  enabled?: boolean;
  provider?: string;
  model?: string;
  answer_llm_config_id?: string;
  display_name?: string;
}

export interface AgentPersona {
  id: string;
  name: string;
  description?: string;
}

export interface AgentConversation {
  id: string;
  title?: string;
  created_at: string;
  model_id: string;
  persona_id?: string;
}

export interface AgentMessage {
  id?: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  query_result?: ExecuteResponse;
  semantic_query?: SemanticQuery;
  judge_verdict?: JudgeVerdict;
  chart_type?: string;
}

export interface JudgeVerdict {
  // Matches the agent-service judge (judge.py) / turn.judged SSE event:
  // a verdict label, a reasoning narrative, and per-rubric-section scores.
  verdict: 'pass' | 'warn' | 'fail' | string;
  reasoning: string;
  metrics: Record<string, number>;
}

export interface DiscoverMembersResponse {
  members: { name: string; key: string }[];
}

export interface DrillOption {
  hierarchy_id: string;
  hierarchy_name: string;
  current_level_name: string;
  next_level_name: string;
}

export interface DrillThroughResponse {
  columns: string[];
  rows: Record<string, unknown>[];
  page: { cursor: string; next_cursor?: string; has_more: boolean };
  drill_mode: string;
  rows_returned: number;
}

export interface AliasMapEntry {
  alias: string;
  canonical: string;
  object_type: 'measure' | 'dimension' | 'hierarchy';
}

export interface DrillThroughSetColumn {
  id: string;
  name: string;
  display_name?: string;
  source_type: string;
}

export interface DrillThroughSet {
  id: string;
  measure_id: string;
  detail_columns: DrillThroughSetColumn[];
  join_paths?: string[];
}

export type FieldCompatibilityReasonCode =
  | 'NO_JOIN_PATH'
  | 'AMBIGUOUS_JOIN_PATH'
  | 'MANY_TO_MANY_UNSUPPORTED'
  | 'AGGREGATE_GRAIN_MISMATCH'
  | 'MEASURE_DEPENDENCY_UNREACHABLE'
  | 'PERSONA_FIELD_UNAVAILABLE'
  | 'HIDDEN_FIELD_UNAVAILABLE'
  | 'UNKNOWN_FIELD'
  | 'SEMANTIC_COMPATIBILITY_NOT_ANALYZED';

export interface FieldCompatibilityIssue {
  code: FieldCompatibilityReasonCode;
  severity?: 'error' | 'warning';
  message: string;
  measure_id: string;
  dimension_id: string;
  compatible_dimension_ids: string[];
  compatible_dimension_names: string[];
}

export interface MeasureFieldCompatibility {
  name: string | null;
  compatible_dimension_ids: string[];
  incompatible_dimensions: Record<string, FieldCompatibilityIssue>;
}

export interface MultiMeasureFieldCompatibility {
  selected_measure_ids: string[];
  common_dimension_ids: string[];
  common_dimension_names: string[];
  conflicts_by_measure: Array<{
    measure_id: string;
    measure_name: string;
    incompatible_dimension_names: string[];
    compatible_dimension_names: string[];
  }>;
  suggested_actions: string[];
}

export interface FieldCompatibilityResponse {
  model_id: string;
  version_id: string | null;
  generated_at: string;
  status: 'compatible' | 'incompatible';
  measures: Record<string, MeasureFieldCompatibility>;
  multi_measure: MultiMeasureFieldCompatibility | null;
}
