// ---------------------------------------------------------------------------
// AI Scheduler Config
// ---------------------------------------------------------------------------
export interface AISchedulerConfig {
  id: string;
  model_id: string;
  ai_enabled: boolean;
  cron_expression: string;
  lookback_hours: number;
  max_creates_per_run: number;
  min_confidence: number;
  dry_run: boolean;
  enable_ai_aggregation: boolean;
  llm_config_id: string | null;
  glossary_llm_config_id: string | null;
  created_at: string;
  updated_at: string;
}
export interface ModelAISchedulerConfigUpdate {
  ai_enabled?: boolean;
  cron_expression?: string;
  lookback_hours?: number;
  max_creates_per_run?: number;
  min_confidence?: number;
  dry_run?: boolean;
  enable_ai_aggregation?: boolean;
  llm_config_id?: string | null;
  glossary_llm_config_id?: string | null;
}

// ---------------------------------------------------------------------------
// LLM Provider Configs
// ---------------------------------------------------------------------------
export interface LLMProviderConfig {
  id: string;
  project_id: string;
  provider: string;
  display_name: string;
  base_url: string | null;
  model_name: string;
  max_tokens: number;
  temperature: number;
  timeout_seconds: number;
  config: Record<string, unknown>;
  has_api_key: boolean;
  created_at: string;
  updated_at: string;
}

// ---------------------------------------------------------------------------
// AI Optimizer
// ---------------------------------------------------------------------------
export interface AIOptimizerTriggerRequest {
  model_id: string;
  dry_run?: boolean;
}
// F-011-17 — manual runs return a 202 acknowledgement; the run executes in the
// background and the UI polls run history (Diagnostics) for progress.
export interface AIOptimizerRunStartResponse {
  id: string;
  model_id: string;
  status: string;
  accepted: boolean;
  poll_after_seconds: number;
}
export interface LLMProviderConfigCreate {
  provider: string;
  display_name: string;
  base_url?: string;
  api_key: string;
  model_name: string;
  max_tokens?: number;
  temperature?: number;
  timeout_seconds?: number;
  config?: Record<string, unknown>;
}
export interface LLMProviderConfigUpdate {
  provider?: string;
  display_name?: string;
  base_url?: string;
  api_key?: string;
  model_name?: string;
  max_tokens?: number;
  temperature?: number;
  timeout_seconds?: number;
  config?: Record<string, unknown>;
}
export interface LLMConnectionTestRequest {
  provider: string;
  base_url?: string;
  api_key: string;
  model_name: string;
  max_tokens?: number;
  temperature?: number;
  timeout_seconds?: number;
  config?: Record<string, unknown>;
}
export interface LLMConnectionTestResponse {
  success: boolean;
  message: string;
  latency_ms: number | null;
}
export interface AIRecommendationEntry {
  // The fingerprint / hit-rate / priority / impact fields were removed with the
  // grouped-pattern telemetry redesign (F-011-04): the LLM never produces them.
  id: string;
  grain: string[];
  measures: Array<{ name: string; aggregation_function?: string }>;
  rationale: string | null;
  status: string;
  aggregate_definition_id: string | null;
  created_at: string;
}
export interface AIOptimizerRun {
  id: string;
  model_id: string;
  triggered_by: string;
  status: string;
  is_dry_run: boolean;
  started_at: string;
  completed_at: string | null;
  llm_provider: string | null;
  llm_model: string | null;
  telemetry_snapshot_id: string | null;
  recommendations_count: number;
  aggregates_created: number;
  aggregates_skipped: number;
  error_message: string | null;
  raw_llm_response: string | null;
  diagnostics_log: Array<{ step: string; message: string; code_map?: Record<string, string> }> | null;
  analysis_notes: string | null;
  recommendations: AIRecommendationEntry[];
}

export interface SchedulerTriggerRequest {
  aggregate_id?: string;
  model_id?: string;
  mode?: "full" | "incremental";
}
export interface TriggerRefreshResponse {
  run_id: string;
  aggregate_id: string;
  status: string;
  refresh_mode: string;
  error_message: string | null;
}

export interface SLAConfig {
  id: string;
  model_id: string;
  target_completion_time: string; // "HH:MM"
  grace_period_minutes: number;
  max_retries: number;
  alert_on_breach: boolean;
}

export interface SLAConfigCreate {
  target_completion_time: string;
  grace_period_minutes?: number;
  max_retries?: number;
  alert_on_breach?: boolean;
}

// ---------------------------------------------------------------------------
// Row Security (Phase 5.1 + Phase 4 Block C)
// ---------------------------------------------------------------------------
export type RowSecurityRuleType = "role_predicate" | "user_mapping";
export type RowSecurityAttributeSource = "jwt_role" | "idp_group" | "saml_claim" | "oidc_scope";

export interface RowSecurityRule {
  id: string;
  model_id: string;
  name: string;
  dimension_path: string;
  rule_type: RowSecurityRuleType;
  predicate_expression: string | null;
  applies_to_roles: string[] | null;
  mapping_table_id: string | null;
  mapping_user_column: string | null;
  mapping_value_column: string | null;
  is_enabled: boolean;
  attribute_source: RowSecurityAttributeSource;
  attribute_claim_name: string | null;
  created_at: string;
  updated_at: string;
}

export interface RowSecurityRuleCreate {
  name: string;
  dimension_path: string;
  rule_type: RowSecurityRuleType;
  predicate_expression?: string | null;
  applies_to_roles?: string[] | null;
  mapping_table_id?: string | null;
  mapping_user_column?: string | null;
  mapping_value_column?: string | null;
  is_enabled?: boolean;
  attribute_source?: RowSecurityAttributeSource;
  attribute_claim_name?: string | null;
}

export interface RowSecurityRuleUpdate {
  name?: string;
  dimension_path?: string;
  predicate_expression?: string | null;
  applies_to_roles?: string[] | null;
  mapping_user_column?: string | null;
  mapping_value_column?: string | null;
  is_enabled?: boolean;
  attribute_source?: RowSecurityAttributeSource;
  attribute_claim_name?: string | null;
}

export interface RowSecuritySimulateRequest {
  user_identity: string;
  roles: string[];
  groups?: string[];
  claims?: Record<string, unknown>;
}

export interface RowSecuritySimulateResponse {
  user_identity: string;
  roles: string[];
  active_rule_ids: string[];
  compiled_predicate: string | null;
}

export interface SecurityAuditEntry {
  query_log_id: string;
  model_id: string | null;
  user_identity: string | null;
  protocol: string;
  route_type: string;
  security_rules_applied: Array<{ rule_id: string; rule_name: string; predicate_sql: string }>;
  created_at: string;
}

export interface SecurityAuditListResponse {
  items: SecurityAuditEntry[];
  total: number;
}

// ---------------------------------------------------------------------------
// Phase 9 / F1 — Source statistics
// ---------------------------------------------------------------------------

export interface ColumnStatistics {
  model_column_id: string;
  column_name: string;
  data_type: string | null;
  distinct_count: number | null;
  null_ratio: number | null;
  min_value: string | null;
  max_value: string | null;
  top_values: { value: unknown; frequency: number }[];
  row_count: number | null;
}

export type StatsRefreshCadence = "manual" | "daily" | "weekly" | "monthly";

export interface TableStatistics {
  model_table_id: string;
  physical_name: string;
  row_count: number | null;
  table_size_bytes: number | null;
  refresh_cadence: StatsRefreshCadence;
  last_refreshed_at: string | null;
  next_refresh_at: string | null;
  last_error: string | null;
  columns: ColumnStatistics[];
}

export interface JoinStatistics {
  join_id: string;
  selectivity: number | null;
  left_distinct_count: number | null;
  right_distinct_count: number | null;
  match_ratio: number | null;
}

export interface SourceStatistics {
  data_source_id: string;
  tables: TableStatistics[];
  joins: JoinStatistics[];
}

// ---------------------------------------------------------------------------
// Phase 9 / F4 + F7 — Predictive aggregates
// ---------------------------------------------------------------------------

export interface PredictiveCandidate {
  grain: string[];
  measure_names: string[];
  fact_table: string;
  score: number;
  score_pct: number;
  expected_hit_rate: number;
  row_reduction: number;
  estimated_rows: number;
  rationale: string;
}

export interface PredictivePreview {
  model_id: string;
  candidates: PredictiveCandidate[];
  had_stats?: boolean;
}

export interface PredictiveBuildResult {
  model_id: string;
  requested: number;
  created_aggregate_ids: string[];
  skipped_count: number;
  errors: string[];
  had_stats?: boolean;
}

// ---------------------------------------------------------------------------
// Phase 9 / F13 — Aggregate lifecycle log
// ---------------------------------------------------------------------------

export interface AggregateLifecycleEvent {
  id: string;
  aggregate_id: string | null;
  event_type: string;
  reason: string | null;
  payload: Record<string, unknown>;
  occurred_at: string;
}

export interface AggregateLifecycleResponse {
  model_id: string;
  events: AggregateLifecycleEvent[];
}

// ---------------------------------------------------------------------------
// Phase 9 / F12 — Cold-start latency dashboard
// ---------------------------------------------------------------------------

export interface ColdStartSample {
  sequence: number;
  occurred_at: string;
  execution_ms: number;
  fingerprint: string;
  aggregate_id: string | null;
}

export interface ColdStartResponse {
  model_id: string;
  last_deployed_at: string | null;
  sample_count: number;
  median_ms: number | null;
  p95_ms: number | null;
  baseline_median_ms: number | null;
  baseline_window_days: number;
  samples: ColdStartSample[];
}

// ---------------------------------------------------------------------------
// Audit events
// ---------------------------------------------------------------------------

export interface AuditEvent {
  id: string;
  timestamp: string;
  actor_id: string | null;
  actor_email: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  target_name: string | null;
  severity: string;
  detail: Record<string, unknown> | null;
  ip_address: string | null;
}

export interface AuditEventListResponse {
  items: AuditEvent[];
  total: number;
  limit: number;
  offset: number;
}

// ---------------------------------------------------------------------------
// SSO / Group Mappings
// ---------------------------------------------------------------------------
export interface AuthBackendsResponse {
  backends: string[];
  saml_enabled: boolean;
  oidc_enabled: boolean;
}

export interface GroupMapping {
  id: string;
  idp_group_name: string;
  project_id: string | null;
  role: string;
}

export interface GroupMappingCreate {
  idp_group_name: string;
  project_id?: string | null;
  role: string;
}

// ---------------------------------------------------------------------------
// Webhooks
// ---------------------------------------------------------------------------
export interface WebhookEndpoint {
  id: string;
  name: string;
  url: string;
  event_filters: string[];
  is_active: boolean;
  created_at: string | null;
  updated_at: string | null;
}

/** A subscribable event type served by the backend catalogue endpoint. */
export interface EventTypeOption {
  value: string;
  label: string;
}

export interface WebhookCreate {
  name: string;
  url: string;
  event_filters: string[];
}

export interface WebhookUpdate {
  name?: string;
  url?: string;
  event_filters?: string[];
  is_active?: boolean;
}

export interface WebhookDelivery {
  id: string;
  endpoint_id: string;
  event_type: string;
  payload: Record<string, unknown>;
  status: string;
  attempts: number;
  response_code: number | null;
  error_message: string | null;
  created_at: string | null;
}

