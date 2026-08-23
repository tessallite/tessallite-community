// ---------------------------------------------------------------------------
// Glossary v1 (semantic-layer Phase 3)
// ---------------------------------------------------------------------------
export type GlossaryTargetType = "dimension" | "measure" | "column" | "concept";
export type GlossaryStatus = "pending_review" | "approved" | "rejected";
export type GlossarySource = "llm" | "user" | "llm_approved" | "heuristic";

export interface GlossaryAttachment {
  id: string;
  entry_id: string;
  target_type: GlossaryTargetType;
  target_id: string | null;
  // F-018-21: human name resolved by the list endpoint so the curation panel
  // shows the dimension/measure name instead of a truncated UUID.
  target_name?: string | null;
}

export type GlossaryVisibility = "show" | "hide" | "review";
export type GlossaryConfidence = "high" | "medium" | "low";

export interface GlossaryEntry {
  id: string;
  model_id: string;
  term: string;
  definition: string;
  context_notes?: string | null;
  source: GlossarySource;
  status: GlossaryStatus;
  version: number;
  superseded_by?: string | null;
  created_by?: string | null;
  proposed_is_hidden?: boolean | null;
  visibility?: GlossaryVisibility | null;
  confidence?: GlossaryConfidence | null;
  created_at: string;
  updated_at: string;
  sample_values?: unknown[] | null;
  synonyms: string[];
  attachments: GlossaryAttachment[];
}

export interface GlossaryEntryUpdate {
  term?: string;
  definition?: string;
  context_notes?: string | null;
  synonyms?: string[];
  proposed_is_hidden?: boolean | null;
  visibility?: GlossaryVisibility;
  confidence?: GlossaryConfidence;
}

export interface GlossaryEntryCreate {
  term: string;
  definition: string;
  context_notes?: string | null;
  synonyms?: string[];
  proposed_is_hidden?: boolean | null;
  target_type: GlossaryTargetType;
  target_id?: string | null;
}

export interface GlossaryBootstrapResponse {
  proposed_count: number;
  updated_count: number;
  skipped_count: number;
  llm_provider?: string | null;
  llm_model?: string | null;
  llm_error?: string | null;
  used_llm?: boolean;
  fallback_count?: number;
  job_id?: string | null;
  job_status?: string | null;
  message?: string | null;
}

// ---------------------------------------------------------------------------
// Logs
// ---------------------------------------------------------------------------
export interface QueryLog {
  id: string;
  model_id: string | null;
  user_identity: string | null;
  protocol: string;
  raw_query: string;
  query_fingerprint: string;
  route_type: string;
  aggregate_id: string | null;
  pocket_id?: string | null;
  // Bug-9172: existing QueryLog timing/byte telemetry can be attributed to a
  // Named Query. Ordinary rows and historical rows keep these fields null.
  named_query_id?: string | null;
  named_query_fallback_reason?: string | null;
  // F-030-20: row-security rules applied to this query (shape varies by rule).
  security_rules_applied?: unknown | null;
  persona_id?: string | null;
  client_kind?: "looker_studio" | "looker_cloud" | null;
  rewritten_query: string | null;
  execution_ms: number | null;
  rows_returned: number | null;
  bytes_processed: number | null;
  status: string;
  error_type: string | null;
  error_detail: string | null;
  created_at: string;
}

/** One stored parse/bind/route stage of a logged query's trace (F-030-25). */
export interface RouteTraceStage {
  route_stage: string;
  detail: Record<string, unknown>;
}

export interface QueryLogFilters {
  modelId?: string;
  status?: string;
  errorType?: string;
  routeType?: string;
  clientKind?: "looker_studio" | "looker_cloud";
  userIdentity?: string;
  dateFrom?: string;
  dateTo?: string;
}
export interface PaginatedQueryLogs {
  items: QueryLog[];
  total: number;
  page: number;
  page_size: number;
}
// ---------------------------------------------------------------------------
// Notification Routes
// ---------------------------------------------------------------------------
export interface NotificationRoute {
  id: string;
  event_type: string;
  channel_type: string;
  channel_config: Record<string, unknown>;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}
export interface NotificationRouteCreate {
  event_type: string;
  channel_type: string;
  channel_config: Record<string, unknown>;
  enabled?: boolean;
}
export interface NotificationRouteUpdate {
  event_type?: string;
  channel_type?: string;
  channel_config?: Record<string, unknown>;
  enabled?: boolean;
}

export interface NotificationDelivery {
  id: string;
  route_id?: string | null;
  project_id?: string | null;
  event_type: string;
  channel_type: string;
  target?: string | null;
  status: string;
  error_message?: string | null;
  created_at?: string | null;
}

export interface QueryMissLog {
  id: string;
  model_id: string;
  query_fingerprint: string;
  grain: string[];
  measures: string[];
  occurrence_count: number;
  missed_at: string;
}

// ---------------------------------------------------------------------------
// Metrics (Phase 7)
// ---------------------------------------------------------------------------
export interface HourlyVolume {
  hour: string;
  total: number;
  aggregate_hits: number;
  pocket_hits?: number;
  source_hits: number;
  // Bug-6426: result-cache re-serves for the hour, kept distinct from real
  // aggregate/pocket acceleration so the bars sum to total.
  cache_hits?: number;
}
export interface RefreshHealthItem {
  aggregate_id: string;
  physical_table_name: string;
  status: string;
  last_status: string | null;
  last_completed_at: string | null;
  next_refresh_cron: string | null;
  rows_written: number | null;
}
export interface MissSummaryItem {
  query_fingerprint: string;
  occurrence_count: number;
  last_seen_at: string;
  grain: string[];
  measures: string[];
}
export interface ModelMetrics {
  model_id: string;
  window_hours: number;
  total_queries: number;
  aggregate_hits: number;
  pocket_hits?: number;
  source_hits: number;
  // Bug-6426: queries re-served from the in-TTL result cache. Distinct from
  // real acceleration; total = aggregate_hits + pocket_hits + source_hits +
  // cache_hits, so surfacing this closes the "chips don't sum to total" gap.
  cache_hits?: number;
  hit_rate: number;
  // Bug-8180: structurally unacceleratable queries (route_type="raw" —
  // explicit ungrouped flat-row detail pulls) reported as a rate of total
  // traffic and a raw count, plus an eligibility-scoped hit rate that
  // excludes them from the denominator.
  unacceleratable: number;
  unacceleratable_queries: number;
  eligible_queries: number;
  eligible_hit_rate: number;
  bytes_avoided: number;
  hourly_volume: HourlyVolume[];
  refresh_health: RefreshHealthItem[];
  miss_summary: MissSummaryItem[];
  pocket_hit_rate?: number;
  pocket_time_saved_ms?: number;
  pocket_storage_bytes?: number;
  pocket_evictions_24h?: number;
  top_pockets?: Array<{
    pocket_id: string;
    physical_table_name: string;
    status: string;
    hit_count: number;
    ttl_days: number;
    last_refresh_at: string | null;
    last_access_at: string | null;
  }>;
}

// ---------------------------------------------------------------------------
// Optimizer / Scheduler triggers
// ---------------------------------------------------------------------------
export interface OptimizerRunEntry {
  ran_at: string;
  triggered_by: string;
  candidates_found: number;
  aggregates_created: number;
  errors: string[];
}
export interface OptimizeRunRequest {
  model_id: string;
  target_id: string;
  max_creates?: number;
  dry_run?: boolean;
}
export interface OptimizeRunResponse {
  candidates_found: number;
  aggregates_created: number;
  created_aggregate_ids: string[];
  dry_run: boolean;
  top_candidates: Array<{
    query_fingerprint: string;
    grain: string[];
    measures: string[];
    occurrence_count: number;
    score: number;
  }>;
  errors: string[];
}
