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
  rewritten_query?: string | null;
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
  // Bug-8103 / F-104-03: backend-authoritative freshness of the served result,
  // owned by the query-router/gateway serve path. Optional until that lane
  // populates it; the frontend FreshnessIndicator consumes it defensively.
  freshness?: ResultFreshness | null;
  /**
   * Bug-8449 / Bug-8453: the row-security rule ids the router applied to this
   * execution (empty when none fired). Rule IDS only — never predicate SQL.
   * Carries the `__deny_all__` sentinel when row security denied every row, so
   * a caller can tell "you are not permitted to see anything" from "there is
   * genuinely no data" instead of rendering both as an empty grid.
   */
  security_rules_applied?: string[] | null;
}

/** Result data-freshness signal (Bug-8103). See FreshnessIndicator. */
export interface ResultFreshness {
  last_refreshed_at?: string | null;
  is_live?: boolean;
  is_stale?: boolean;
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
  /** Bug-7265: the aggregate function the clicked pivot column uses (e.g. "AVG").
   *  When omitted the backend falls back to the measure's default_agg. */
  override_agg?: string | null;
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
  /**
   * Bug-8453 / R5 finding F2: the row-security denial channel on the drill
   * grid. The producer shipped in R4 with NO consumer on any client, so a
   * drill into a cell whose detail rows are all RLS-denied still read as
   * "no detail rows" -- a statement about the business, not about access.
   */
  security_rules_applied?: string[];
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
  /**
   * Read from each join's declared CARDINALITY, not its join type — those
   * are separate properties. "mixed" also covers a path whose fan-out is
   * undeclared on any hop.
   */
  cardinality_hint: "many-to-one" | "one-to-many" | "one-to-one" | "mixed";
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
  cls_blocked_measure_ids?: string[];
  cls_blocked_dimension_ids?: string[];
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
  // Bug-7051: persisted atomically with the persona row so create/update
  // is a single request — no two-step call that can leave a persona
  // without its intended restrictions.
  restricted_tag_ids?: string[];
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
  // Bug-7051: persisted atomically with the persona update.
  restricted_tag_ids?: string[];
}
export interface PersonaResolution {
  persona_id: string;
  measure_id: string;
  measure_allowed: boolean;
  reason: string | null;
}
