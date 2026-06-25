import type { FieldCompatibilityReasonCode } from "./dimensions";

// ---------------------------------------------------------------------------
// Lineage
// ---------------------------------------------------------------------------
export interface LineageNode {
  id: string;
  label: string;
  type: "source" | "semantic" | "aggregate" | "target" | "column" | "field";
  description?: string | null;
  meta?: Record<string, string>;
  creation_reason?: string | null;
  status?: string | null;
  last_refreshed_at?: string | null;
  downstream_asset_count?: number;
}
export interface LineageEdge {
  source: string;
  target: string;
  label: string;
}
export interface LineageGraph {
  nodes: LineageNode[];
  edges: LineageEdge[];
}

// ---------------------------------------------------------------------------
// Query Router — validate / explain / execute
// ---------------------------------------------------------------------------
export interface QueryRouterRequest {
  model_id: string;
  raw_query: string;
  protocol?: "jdbc" | "dax";
  dialect?: string;
  force_route?: "source" | "aggregate" | "pocket";
  persona_id?: string | null;
}
export interface TraceStep {
  stage: "parser" | "binder" | "router" | "rewriter" | "executor";
  title: string;
  detail: string;
  status: "ok" | "warn" | "error";
  data: Record<string, unknown>;
}
export interface TargetSystemInfo {
  name?: string | null;
  type?: string | null;
  location?: string | null;
}
export interface AggregateUsedInfo {
  id: string;
  physical_table_name: string;
  grain: string[];
  creation_reason?: string | null;
  status?: string | null;
}
export interface PocketUsedInfo {
  id: string;
  physical_table_name: string;
  status?: string | null;
  refresh_policy?: string | null;
}
export interface PipelineTrace {
  steps: TraceStep[];
  target_system?: TargetSystemInfo | null;
  aggregate_used?: AggregateUsedInfo | null;
  pocket_used?: PocketUsedInfo | null;
}
export interface QueryRouterFieldCompatibilityIssue {
  code: FieldCompatibilityReasonCode | string;
  severity?: "error" | "warning";
  measure_name?: string | null;
  dimension_name?: string | null;
  message: string;
  compatible_dimension_names?: string[];
}
export interface QueryRouterFieldCompatibilityFeedback {
  status: "compatible" | "incompatible" | "not_analyzed";
  issues: QueryRouterFieldCompatibilityIssue[];
}
export interface ValidateResponse {
  ok: boolean;
  errors: string[];
  warnings: string[];
  requested_measures: string[];
  requested_dimensions: string[];
  field_compatibility?: QueryRouterFieldCompatibilityFeedback | null;
}
export interface ExplainResponse {
  route_type: string;
  aggregate_id: string | null;
  pocket_id?: string | null;
  reason: string;
  rewritten_query: string;
  requested_measures: string[];
  requested_dimensions: string[];
  grain: string[];
  query_fingerprint: string;
  trace: PipelineTrace;
  field_compatibility?: QueryRouterFieldCompatibilityFeedback | null;
}
export interface ExecuteResponse {
  rows: Record<string, unknown>[];
  columns: string[];
  route_type: string;
  reason?: string;
  aggregate_id: string | null;
  pocket_id?: string | null;
  execution_ms: number;
  bytes_processed: number;
  rows_returned: number;
  trace: PipelineTrace;
  field_compatibility?: QueryRouterFieldCompatibilityFeedback | null;
}

// ---------------------------------------------------------------------------
// Drill-through (Phase 4C)
// ---------------------------------------------------------------------------
export interface DrillThroughFilter {
  column: string;
  op?: "eq" | "neq" | "gt" | "gte" | "lt" | "lte" | "like" | "ilike" | "in" | "between" | "is_null" | "is_not_null";
  value?: unknown;
}
export interface DrillThroughRequest {
  filters?: DrillThroughFilter[];
  grouping_levels?: DrillThroughFilter[];
  cursor?: string | null;
  limit?: number | null;
  persona_id?: string | null;
  hierarchy_id?: string | null;
  force_route?: "source" | "aggregate" | "pocket" | null;
}
export interface DrillThroughPageInfo {
  cursor: string;
  next_cursor: string | null;
  has_more: boolean;
}
export interface DrillDimensionOut {
  id: string;
  name: string;
  display_name: string;
}
export interface HierarchyPathEntry {
  level_name: string;
  dimension_name: string;
  value: unknown;
}
export interface DrillableHierarchy {
  hierarchy_id: string;
  hierarchy_name: string;
  current_level_name: string;
  next_level_name: string;
}
export interface DrillThroughResponse {
  columns: string[];
  rows: Record<string, unknown>[];
  page: DrillThroughPageInfo;
  drill_mode: "hierarchy" | "leaf";
  drill_dimension: DrillDimensionOut | null;
  hierarchy_path: HierarchyPathEntry[];
  drillable_hierarchies: DrillableHierarchy[];
  fact_table?: string | null;
  route_type: string;
  execution_ms: number;
  bytes_processed: number;
  rows_returned: number;
}
export interface DrillOptionsRequest {
  grouping_levels?: DrillThroughFilter[];
  persona_id?: string | null;
}
export interface DrillOptionsResponse {
  hierarchies: DrillableHierarchy[];
}

// Drill-through set curation (Phase 8.A)
export interface DrillThroughSet {
  id: string;
  measure_id: string;
  source_table_id: string | null;
  detail_columns: string[] | null;
  joined_dimension_ids: string[] | null;
  row_limit_override: number | null;
  source_join_path: string[] | null;
  created_at: string;
  updated_at: string;
}
export interface DrillThroughSetUpdate {
  source_table_id?: string | null;
  detail_columns?: string[] | null;
  joined_dimension_ids?: string[] | null;
  row_limit_override?: number | null;
  source_join_path?: string[] | null;
}

export interface DrillJoinPathHop {
  join_id: string;
  left_table_id: string;
  right_table_id: string;
}
export interface DrillJoinPath {
  hops: DrillJoinPathHop[];
  cardinality_hint: "many-to-one" | "one-to-many" | "mixed";
}
export interface DrillJoinPathsResponse {
  paths: DrillJoinPath[];
}

// Personas (Phase 8.B). A persona maps to a gateway catalog variant
// named `<model.slug>_<persona.slug>` — the technical persona (slug
// "technical", includes_hidden_columns=true) is seeded per model.
export interface Persona {
  id: string;
  model_id: string;
  slug: string;
  name: string;
  description: string | null;
  included_measure_ids: string[];
  included_dimension_ids: string[];
  included_hierarchy_ids: string[];
  audience_roles: string[];
  default_filters: Record<string, unknown>;
  bypass_row_security: boolean;
  includes_hidden_columns: boolean;
  restricted_column_ids?: string[];
  created_at: string;
  updated_at: string;
}
export interface PersonaCreate {
  slug: string;
  name: string;
  description?: string | null;
  included_measure_ids?: string[];
  included_dimension_ids?: string[];
  included_hierarchy_ids?: string[];
  audience_roles?: string[];
  default_filters?: Record<string, unknown>;
  bypass_row_security?: boolean;
  includes_hidden_columns?: boolean;
}
export interface PersonaUpdate {
  slug?: string;
  name?: string;
  description?: string | null;
  included_measure_ids?: string[];
  included_dimension_ids?: string[];
  included_hierarchy_ids?: string[];
  audience_roles?: string[];
  default_filters?: Record<string, unknown>;
  bypass_row_security?: boolean;
  includes_hidden_columns?: boolean;
}
export interface PersonaResolution {
  persona_id: string;
  measure_id: string;
  measure_allowed: boolean;
  reason: string | null;
}
