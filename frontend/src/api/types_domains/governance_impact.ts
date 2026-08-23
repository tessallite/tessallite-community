// ---------------------------------------------------------------------------
// Model Parameters
// ---------------------------------------------------------------------------
export type ParamType = "string" | "number" | "multi_value" | "date_range" | "boolean";

export interface ModelParameter {
  id: string;
  model_id: string;
  name: string;
  display_name: string | null;
  param_type: ParamType;
  default_value: unknown;
  allowed_values: unknown[] | null;
  description: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface ModelParameterCreate {
  name: string;
  display_name?: string;
  param_type: ParamType;
  default_value?: unknown;
  allowed_values?: unknown[];
  description?: string;
}

export interface ModelParameterUpdate {
  display_name?: string;
  param_type?: ParamType;
  default_value?: unknown;
  allowed_values?: unknown[];
  description?: string;
}

export interface PersonaParameterCollision {
  persona_id: string;
  persona_name: string;
  persona_slug: string;
  default_filter_key: string;
  parameter_name: string;
  suggested_key: string;
}

export interface PersonaParameterCollisionPreflightResponse {
  model_id: string;
  deployed_version_id: string | null;
  collisions: PersonaParameterCollision[];
}

// ---------------------------------------------------------------------------
// Schema drift
// ---------------------------------------------------------------------------

export interface SchemaChangeEvent {
  id: string;
  model_id: string;
  source_id: string | null;
  table_name: string | null;
  change_type: "column_added" | "column_removed" | "type_changed";
  is_breaking: boolean;
  detail: Record<string, unknown>;
  detected_at: string;
  acknowledged_at: string | null;
}

export interface SchemaChangeEventListResponse {
  items: SchemaChangeEvent[];
  total: number;
}

export interface MeasureRenameImpactItem {
  consumer_type: string;
  consumer_id: string;
  consumer_name: string | null;
  field: string;
}

export interface MeasureRenameImpactResponse {
  measure_id: string;
  current_name: string;
  new_name: string;
  safe: boolean;
  rewrites: MeasureRenameImpactItem[];
  blockers: MeasureRenameImpactItem[];
}

// ---------------------------------------------------------------------------
// Derived-grain relationship health (Model Health surface)
// ---------------------------------------------------------------------------

export type RelationshipHealthState =
  | "healthy"
  | "broken"
  | "stale"
  | "error"
  | "pending";

export interface RelationshipHealthItem {
  relationship_id: string;
  dimension_id: string;
  dimension_name: string | null;
  detail_column_name: string | null;
  key_column_name: string | null;
  cardinality: string;
  enabled: boolean;
  state: RelationshipHealthState;
  last_checked_at: string | null;
  error_code: string | null;
}

export interface RelationshipHealthResponse {
  model_id: string;
  items: RelationshipHealthItem[];
}

// ---------------------------------------------------------------------------
// Join population governance health (Model Health surface)
//
// Mirrors services/model-service/src/api/join_population_health.py exactly —
// same field names, same OK/WARNING/BLOCKED vocabulary. Read-only: computed
// at deploy time (docs/architecture/architecture_join-population-governance.md),
// never on the query path, and this call never changes what a query returns.
// ---------------------------------------------------------------------------

export type JoinPopulationStatus = "OK" | "WARNING" | "BLOCKED";

export interface JoinPopulationHealthItem {
  join_id: string;
  left_table_name: string | null;
  right_table_name: string | null;
  left_column_name: string | null;
  right_column_name: string | null;
  join_type: string;
  // The join's declaration as it stands right now.
  population_participation: string;
  // The declaration the stored verdict was actually computed against; null
  // when this join has no verdict yet.
  checked_population_participation: string | null;
  // True when the live declaration differs from the one the verdict was
  // computed against — i.e. redeploy to refresh it.
  declaration_changed_since_check: boolean;
  // True when the join's type or either join column moved since the verdict
  // was measured (a PATCH that does not bump the deploy epoch).
  inputs_changed_since_check: boolean;
  // neutral | filtering | multiplying — null when unmeasured.
  classification: string | null;
  status: JoinPopulationStatus | null;
  measured: boolean;
  row_loss_ratio: number | null;
  row_mult_ratio: number | null;
  row_effect_ratio: number | null;
  reason: string | null;
  checked_at: string | null;
  // True when the verdict no longer describes the join as it stands (either
  // the model moved to a newer deploy epoch, or a classifier input changed).
  stale: boolean;
}

export interface JoinPopulationHealthResponse {
  model_id: string;
  status: JoinPopulationStatus;
  // False when any join lacks a measured verdict.
  evaluated: boolean;
  join_count: number;
  evaluated_count: number;
  warning_count: number;
  blocked_count: number;
  // Compatibility posture field. G5 returns false: measured policy blockers
  // are enforced by deploy before publish state can commit.
  warn_only: boolean;
  items: JoinPopulationHealthItem[];
}

// ---------------------------------------------------------------------------
// Usage & Downstream Assets
// ---------------------------------------------------------------------------

export type AssetType = "dashboard" | "report" | "ml_job" | "api" | "other";

export interface DownstreamAssetCreate {
  asset_type: AssetType;
  asset_name: string;
  asset_url?: string | null;
  owner?: string | null;
  notes?: string | null;
  column_ids?: string[];
}

export interface DownstreamAssetUpdate {
  asset_type?: AssetType;
  asset_name?: string;
  asset_url?: string | null;
  owner?: string | null;
  notes?: string | null;
  column_ids?: string[];
}

export interface DownstreamAsset {
  id: string;
  model_id: string;
  asset_type: AssetType;
  asset_name: string;
  asset_url: string | null;
  owner: string | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
  column_ids: string[];
}

export interface DownstreamAssetSummary {
  total: number;
  by_type: Record<string, number>;
}

export interface GatewayQueryReference {
  id: string;
  model_id: string;
  queried_table: string;
  query_user: string | null;
  query_text_hash: string;
  last_seen_at: string;
  hit_count: number;
}

export interface ImpactScanResponse {
  references_upserted: number;
  tables_matched: number;
  columns_matched: number;
  /** Query-log rows examined by this pass. The scan is incremental. */
  logs_scanned: number;
  /** True when unscanned log rows remain; press Run scan again to continue. */
  more_remaining: boolean;
}

export interface ColumnUsageItem {
  column_name: string;
  /** Physical table the usage was attributed to; empty when `ambiguous`. */
  table_name: string;
  /** Bound semantic references; legacy logs use parsed SQL token occurrences. */
  hit_count: number;
  /** Distinct queries referencing this (table, column). */
  query_count: number;
  last_seen_at: string | null;
  /** True when an unqualified reference matched several model tables (Bug-8074). */
  ambiguous: boolean;
  /** Tables carrying this column name; populated only when `ambiguous`. */
  candidate_tables: string[];
}

export interface ColumnUsageResponse {
  model_id: string;
  total_queries_parsed: number;
  total_queries_skipped: number;
  /** Successful query-log rows that exist, regardless of how many were read. */
  logs_available: number;
  /** True when older rows exist beyond the examined window. */
  truncated: boolean;
  columns: ColumnUsageItem[];
}

// ---------------------------------------------------------------------------
// Data Tags — Column-Level Persona Security
// ---------------------------------------------------------------------------

export interface DataTagColumnInfo {
  column_id: string;
  table_name: string;
  column_name: string;
}

export interface DataTagCreate {
  tag_name: string;
  description?: string | null;
  column_ids?: string[];
}

export interface DataTagUpdate {
  tag_name?: string;
  description?: string | null;
  column_ids?: string[];
}

export interface DataTag {
  id: string;
  model_id: string;
  tag_name: string;
  description: string | null;
  created_at: string;
  columns: DataTagColumnInfo[];
}

// ---------------------------------------------------------------------------
// Named Sets
// ---------------------------------------------------------------------------

export interface BuilderDefinition {
  type: string;
  dimension?: string;
  hierarchy?: string;
  members?: (string | number | { key: string })[];
  entity?: string;
  count?: number;
  measure?: string;
  direction?: "top" | "bottom";
  conditions?: { field: string; operator: string; value: string | number }[];
  logic?: "AND" | "OR";
  /** UUID reference to the dimension column — used by sql_fixed lists for rename-proofing. */
  column_id?: string;
  /** Data type for sql_fixed list members: "string" or "number". */
  data_type?: "string" | "number";
  /** SQL query text for sql_query definition type. */
  query?: string;
  /** ISO timestamp of the last refresh (sql_query / sql_fixed). */
  last_refreshed_at?: string | null;
}

export interface NamedSetCreate {
  name: string;
  display_name?: string | null;
  description?: string | null;
  display_folder?: string | null;
  scope?: number;
  expression?: string | null;
  dimensions?: string | null;
  builder_definition?: BuilderDefinition | null;
  list_type?: string | null;
  owner_user_id?: string | null;
}

export interface NamedSetUpdate {
  name?: string;
  display_name?: string | null;
  description?: string | null;
  display_folder?: string | null;
  scope?: number;
  expression?: string;
  dimensions?: string | null;
  builder_definition?: BuilderDefinition | null;
  list_type?: string | null;
  certification_status?: string | null;
  owner_user_id?: string | null;
}

export interface TrustMeta {
  last_refreshed_at: string | null;
  source_system: string | null;
  owner: string;
}

export interface NamedSet {
  id: string;
  model_id: string;
  name: string;
  display_name: string | null;
  description: string | null;
  display_folder: string | null;
  scope: number;
  expression: string;
  dimensions: string | null;
  builder_definition: BuilderDefinition | null;
  list_type: string | null;
  certification_status: string;
  replacement_id: string | null;
  owner_user_id: string | null;
  /** Shared freshness/source/owner metadata for the served named-set object. */
  trust_meta?: TrustMeta | null;
  created_at: string;
  updated_at: string;
}

export interface NamedSetValidateRequest {
  builder_definition?: BuilderDefinition | null;
  expression?: string | null;
}

export interface NamedSetValidateResponse {
  is_valid: boolean;
  errors: string[];
  warnings: string[];
  explanation: string | null;
  compiled_expression: string | null;
  estimated_cost_band: string | null;
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
  warnings?: string[];
}

// ---------------------------------------------------------------------------
// Named Queries (strategy §12 — model-bound SQL record sets)
// ---------------------------------------------------------------------------

/** One derived output column of a Named Query definition. */
export interface NamedQueryOutputColumn {
  name: string;
  type: string;
}

/** Materialisation state of a Named Query. */
export interface NamedQueryArtifact {
  id: string;
  target_id: string;
  physical_table_name: string;
  target_schema: string | null;
  row_count: number | null;
  status: string;
  failure_reason: string | null;
  last_refresh_at: string | null;
  retired_at: string | null;
}

export interface NamedQueryRefreshPolicy {
  cron_expression: string | null;
  is_enabled: boolean;
}

// F-026-01 / F-101-07: PUT /refresh/policy body (partial upsert; unset fields
// are left unchanged on the persisted row).
export interface NamedQueryRefreshPolicyUpsert {
  cron_expression?: string | null;
  is_enabled?: boolean;
}

/** Existing QueryLog cost telemetry attributed to one Named Query. */
export interface NamedQueryFallbackReasonCount {
  reason: string;
  count: number;
}

export interface NamedQueryAnalytics {
  named_query_id: string;
  window_days: number;
  total_queries: number;
  materialized_queries: number;
  fallback_queries: number;
  fallback_failures: number;
  fallback_rate: number;
  avg_fallback_execution_ms: number | null;
  avg_fallback_bytes_processed: number | null;
  fallback_reasons: NamedQueryFallbackReasonCount[];
  recommendation: "none" | "repair_named_query_materialisation" | string;
  recommendation_reason: string | null;
}

export interface NamedQueryRefreshRun {
  id: string;
  named_query_id: string;
  refresh_mode: string;
  status: string;
  started_at: string;
  completed_at: string | null;
  rows_written: number | null;
  bytes_processed: number | null;
  error_message: string | null;
  triggered_by: string;
}

export interface NamedQuery {
  id: string;
  model_id: string;
  name: string;
  display_name: string | null;
  description: string | null;
  display_folder: string | null;
  definition_sql: string;
  output_columns: NamedQueryOutputColumn[] | null;
  shape: string;
  row_cap: number | null;
  column_cap: number | null;
  certification_status: string;
  created_by: string | null;
  artifact: NamedQueryArtifact | null;
  refresh_policy: NamedQueryRefreshPolicy | null;
  created_at: string;
  updated_at: string;
}

export interface NamedQueryCreate {
  name: string;
  display_name?: string | null;
  description?: string | null;
  display_folder?: string | null;
  definition_sql: string;
  row_cap?: number | null;
  column_cap?: number | null;
  certification_status?: string;
  refresh_policy?: string;
  refresh_cron?: string | null;
  refresh_policy_enabled?: boolean | null;
}

export interface NamedQueryUpdate {
  name?: string;
  display_name?: string | null;
  description?: string | null;
  display_folder?: string | null;
  definition_sql?: string;
  row_cap?: number | null;
  column_cap?: number | null;
  certification_status?: string | null;
}

export interface NamedQueryValidateRequest {
  definition_sql: string;
}

export interface NamedQueryValidateResponse {
  is_valid: boolean;
  errors: string[];
  warnings: string[];
  output_columns: NamedQueryOutputColumn[];
  shape: string | null;
}
