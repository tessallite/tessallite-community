// ---------------------------------------------------------------------------
// Dimensions / Measures
// ---------------------------------------------------------------------------
import type { MeasureWarning } from "./sources_schema";
export interface DimensionCreate {
  name: string;
  display_name?: string;
  description?: string | null;
  display_folder?: string | null;
  source_table_id?: string;
  source_column_name?: string;
  /** Bug-5434: optional distinct DISPLAY column (caption source) for a flat dim. */
  display_column_name?: string | null;
  data_type?: string;
  user_defined_attribute_id?: string;
  is_time_dim?: boolean;
  time_grain?: string;
  calc_expression?: string | null;
}
export interface ModelAlert {
  id: string;
  model_id: string;
  severity: "info" | "warning" | "error" | "critical";
  category: string;
  title: string;
  detail: string | null;
  related_object_type: string | null;
  related_object_id: string | null;
  first_seen_at: string;
  last_seen_at: string;
  occurrence_count: number;
  resolved_at: string | null;
  dismissed_at: string | null;
  dismissed_by: string | null;
}
export interface ModelAlertCount {
  total: number;
  /** Count under the same filters as the list endpoint (drives pagination). */
  filtered_total?: number;
  by_severity: Record<string, number>;
}
export interface ModelRevalidationReport {
  invalid_dimension_count: number;
  invalid_measure_count: number;
  invalid_aggregate_count: number;
  newly_valid_dimension_count: number;
  newly_valid_measure_count: number;
  newly_valid_aggregate_count: number;
  unresolved_hierarchy_issue_count: number;
  failed_pocket_count: number;
  unacknowledged_schema_drift_count: number;
  latest_recorded_schema_drift_at: string | null;
  live_source_checked: boolean;
  measure_warnings?: MeasureWarning[];
}
export interface RedundantPartner {
  partner_column_name: string;
  partner_table_name: string;
  partner_physical_table: string;
  join_type: string;
  reason: string;
}

// ---------------------------------------------------------------------------
// Dimension attribute relationships (derived-grain routing, spec section 5.3)
// ---------------------------------------------------------------------------
// A modeller-declared key-to-detail relationship on a dimension. Distinct from
// display_column_id (a caption choice). Phase 1b: declaration only, no serving.

/** BIJECTION = exact 1:1 relabel; FUNCTIONAL_N_TO_1 = many keys -> one detail. */
export type AttributeRelationshipCardinality =
  | "BIJECTION"
  | "FUNCTIONAL_N_TO_1";

/**
 * DECLARED until the (Phase-2) verifier proves/breaks the relationship.
 * PENDING (Bug-7894): a text (VARCHAR/CHAR/STRING) 1:1 detail proven 1:1 by data
 * but whose serve-collation fold-safety can only be certified when the passenger
 * aggregate is built. It is non-serving and clears to VERIFIED after that build —
 * neither a defect (BROKEN) nor a fault (ERROR).
 */
export type AttributeRelationshipStatus =
  | "DECLARED"
  | "PENDING"
  | "VERIFIED"
  | "BROKEN"
  | "STALE"
  | "ERROR";

export interface DimensionAttributeRelationshipCreate {
  detail_column_name: string;
  cardinality: AttributeRelationshipCardinality;
  key_column_name?: string | null;
  enabled?: boolean;
}

export interface DimensionAttributeRelationshipUpdate {
  detail_column_name?: string | null;
  cardinality?: AttributeRelationshipCardinality | null;
  key_column_name?: string | null;
  enabled?: boolean | null;
}

export interface DimensionAttributeRelationship {
  id: string;
  model_id: string;
  dimension_id: string;
  key_column_id: string | null;
  key_column_name: string | null;
  detail_column_id: string | null;
  detail_column_name: string | null;
  cardinality: AttributeRelationshipCardinality;
  null_policy: string;
  enabled: boolean;
  declaration_hash: string;
  verification_status: AttributeRelationshipStatus;
  verified_at: string | null;
  created_at: string;
  updated_at: string;
}
export interface Dimension {
  id: string;
  name: string;
  display_name: string;
  description?: string | null;
  display_folder?: string | null;
  is_hidden?: boolean;
  source_column_id: string | null;
  source_column_name: string | null;
  /** Bug-5434: distinct DISPLAY column (caption source) for a flat dimension. */
  display_column_id?: string | null;
  display_column_name?: string | null;
  data_type?: string | null;
  source_table_id: string | null;
  source_table_alias: string | null;
  source_table_display_name: string | null;
  user_defined_attribute_id: string | null;
  user_defined_attribute_name: string | null;
  is_time_dim: boolean;
  time_grain: string | null;
  calc_expression?: string | null;
  calc_expression_tables?: string[] | null;
  is_invalid?: boolean;
  invalid_reason?: string | null;
  redundant_partner: RedundantPartner | null;
  high_cardinality?: boolean | null;
  /** Provenance: when this dimension was auto-added as a detail of another
   *  dimension's bijection relationship, these record the source relationship
   *  and owning dimension. Null for independently created dimensions. */
  detail_of_relationship_id?: string | null;
  detail_of_dimension_id?: string | null;
  detail_of_dimension_name?: string | null;
  /** Declared key-to-detail relationships (derived-grain routing). Empty for
   *  dimensions with no declared relationship. Distinct from display_column. */
  attribute_relationships?: DimensionAttributeRelationship[];
}
export type MeasureFormatToken =
  | "currency"
  | "percent"
  | "percent_2dp"
  | "integer"
  | "decimal_2dp"
  | "decimal_0"
  | "decimal_1"
  | "decimal_3"
  | "decimal_4"
  | "decimal_5"
  | "decimal_6";

// #10: "by_account" is not a supported behaviour (removed from
// VALID_SEMI_ADDITIVE_BEHAVIORS). It cannot be authored; the account-column
// field (semi_additive_account_column_id) is retained on the request/response
// types only for reading pre-existing data.
export type SemiAdditiveBehavior =
  | "last_non_empty"
  | "first_non_empty"
  | "avg_of_children"
  | "min"
  | "max";

export interface MeasureCreate {
  name: string;
  display_name?: string;
  description?: string | null;
  display_folder?: string | null;
  source_table_id?: string;
  source_column_name?: string;
  user_defined_attribute_id?: string;
  measure_type?: string;
  expression?: string;
  calc_agg_mode?: "expression_as_written" | "per_row_then_aggregate";
  default_agg: "sum" | "avg" | "count" | "count_distinct" | "min" | "max";
  data_type?: string;
  format?: MeasureFormatToken | null;
  is_additive?: boolean;
  semi_additive_behavior?: SemiAdditiveBehavior | null;
  semi_additive_account_column_id?: string | null;
  variant_kind?: string | null;
  variant_of_measure_id?: string | null;
  variant_n?: number | null;
  calendar_model_table_id?: string | null;
  hierarchy_id?: string | null;
  date_dimension_column_id?: string | null;
  cross_model_source_model_id?: string | null;
  cross_model_source_measure_id?: string | null;
}
export interface Measure {
  id: string;
  name: string;
  display_name: string;
  description?: string | null;
  display_folder?: string | null;
  is_hidden?: boolean;
  source_column_id: string | null;
  source_column_name: string | null;
  source_table_id: string | null;
  user_defined_attribute_id: string | null;
  user_defined_attribute_name: string | null;
  measure_type: string;
  expression: string | null;
  calc_agg_mode?: "expression_as_written" | "per_row_then_aggregate" | null;
  default_agg: string;
  data_type: string;
  format: MeasureFormatToken | null;
  is_additive: boolean;
  semi_additive_behavior?: SemiAdditiveBehavior | null;
  semi_additive_account_column_id?: string | null;
  is_invalid?: boolean;
  invalid_reason?: string | null;
  redundant_partner: RedundantPartner | null;
  variant_kind?: string | null;
  variant_of_measure_id?: string | null;
  variant_n?: number | null;
  eligible_variant_kinds?: string[] | null;
  calendar_model_table_id?: string | null;
  hierarchy_id?: string | null;
  resolved_calendar_id?: string | null;
  resolved_date_col_id?: string | null;
  date_dimension_column_id?: string | null;
  cross_model_source_model_id?: string | null;
  cross_model_source_measure_id?: string | null;
}

export interface ValidateMeasureExpressionRequest {
  expression: string;
  self_measure_id?: string | null;
}

export interface ValidateMeasureExpressionResponse {
  valid: boolean;
  referenced_measure_ids: string[];
  referenced_measure_names: string[];
  error: string | null;
}

export type FieldCompatibilityReasonCode =
  | "NO_JOIN_PATH"
  | "AMBIGUOUS_JOIN_PATH"
  | "MANY_TO_MANY_UNSUPPORTED"
  | "AGGREGATE_GRAIN_MISMATCH"
  | "MEASURE_DEPENDENCY_UNREACHABLE"
  | "PERSONA_FIELD_UNAVAILABLE"
  | "HIDDEN_FIELD_UNAVAILABLE"
  | "UNKNOWN_FIELD"
  | "SEMANTIC_COMPATIBILITY_NOT_ANALYZED";

export interface FieldCompatibilityIssue {
  code: FieldCompatibilityReasonCode;
  severity?: "error" | "warning";
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
  status: "compatible" | "incompatible";
  measures: Record<string, MeasureFieldCompatibility>;
  multi_measure: MultiMeasureFieldCompatibility | null;
}

// ---------------------------------------------------------------------------
// Joins
// ---------------------------------------------------------------------------
/**
 * How many rows on each side of a join match. SEPARATE from `join_type`,
 * which says which rows survive. Cardinality never changes the generated SQL;
 * it is fan-out metadata. `null` means the modeller has not declared it.
 * Mirrors `JoinCardinality` in shared/schemas/domains/aggregates_security.py.
 */
export type JoinCardinality =
  | "one_to_one"
  | "one_to_many"
  | "many_to_one"
  | "many_to_many";

/**
 * Whether the modeller INTENDS this join's row-filtering / row-multiplying
 * effect to define the model's population. A THIRD, independent property of
 * the same join (`join_type` says which rows survive, `cardinality` says how
 * many rows match, this says whether that effect is deliberate). Governs
 * whether the join may be elided by a query that does not reference it.
 * Mirrors `PopulationParticipation` in
 * shared/schemas/domains/aggregates_security.py.
 * docs/architecture/architecture_join-population-governance.md contract 2.
 */
export type PopulationParticipation =
  | "preserve_base_rows"
  | "population_defining"
  | "enrichment_only"
  | "undeclared";

export interface JoinCreate {
  left_table_id: string;
  right_table_id: string;
  join_type: "inner" | "left" | "right" | "full";
  cardinality?: JoinCardinality | null;
  // Optional on write — the backend defaults to "preserve_base_rows" (the
  // pre-existing elision behaviour) when omitted.
  population_participation?: PopulationParticipation;
  left_column_name: string;
  right_column_name: string;
}
export interface Join {
  id: string;
  left_table_id: string;
  right_table_id: string;
  join_type: string;
  cardinality?: string | null;
  // Always present on read (server default "preserve_base_rows"); free-form
  // string, not the write-side Literal — a historical/imported row may carry
  // a value read-side coercion has not yet folded onto the vocabulary.
  population_participation: string;
  left_column_id: string;
  right_column_id: string;
  left_column_name: string | null;
  right_column_name: string | null;
  warnings?: string[];
}

// ---------------------------------------------------------------------------
// Aggregates
// ---------------------------------------------------------------------------
export interface AggregateCreate {
  target_id: string;
  grain: string[];
  measure_names: string[];
  include_quantiles?: boolean;
  include_stats?: boolean;
  creation_reason?: string;
  confirm_redundant_grain?: boolean;
}
export interface AggregateDefinition {
  id: string;
  physical_table_name: string;
  grain: string[];
  grain_physical_cols: string[] | null;
  invalid_reason: string | null;
  measure_names: string[];
  status: string;
  // Derived by the backend from status: "healthy" = serving or paused-but-
  // materialised (active/disabled); "unhealthy" = pending/invalid/retired.
  health?: "healthy" | "unhealthy";
  include_quantiles: boolean;
  include_stats?: boolean;
  estimated_hit_rate: number | null;
  creation_reason: string;
  // F-010-18 — set by the feedback sweep when a predictive aggregate's grain
  // receives real query traffic; drives the "Validated" badge on the card.
  predictive_validated_at?: string | null;
  rationale: string | null;
  is_stale: boolean;
  created_at: string;
  retired_at: string | null;
  // Phase 8.C.2 — persona scope. Null = global (any persona), UUID =
  // scoped to that persona only. Surfaced in the Model Health panel.
  persona_id: string | null;
}
export interface AggregateUpdate {
  status?: string;
  include_quantiles?: boolean;
  include_stats?: boolean;
}
export interface RefreshPolicyCreate {
  refresh_mode: "scheduled" | "incremental";
  cron_expression?: string | null;
  incremental_column?: string | null;
  incremental_lookback?: number | null;
  incremental_append_only?: boolean;
  full_rebuild_interval_days?: number | null;
  is_enabled?: boolean;
}
export interface RefreshPolicy {
  id: string;
  aggregate_definition_id: string;
  refresh_mode: string;
  cron_expression: string | null;
  incremental_column: string | null;
  incremental_lookback: number | null;
  incremental_append_only: boolean;
  full_rebuild_interval_days: number | null;
  is_enabled: boolean;
  created_at: string;
  updated_at: string;
}
export interface RefreshRun {
  id: string;
  aggregate_definition_id: string;
  aggregate_table: string | null;
  status: string;
  refresh_mode: string;
  started_at: string;
  completed_at: string | null;
  duration_ms: number | null;
  rows_written: number | null;
  triggered_by: string;
  error_message: string | null;
}

export interface PocketPredicateInput {
  column_name: string;
  operator: string;
  value: unknown;
}

export interface PocketCreate {
  target_id: string;
  defining_sql: string;
  refresh_policy?: "schedule" | "manual" | "event";
  refresh_cron?: string | null;
  // Bug-7837: send the schedule enabled state atomically on create so the
  // backend provisions the PocketRefreshPolicy row in one transaction.
  refresh_policy_enabled?: boolean | null;
  incremental_column?: string | null;
  incremental_lookback_hours?: number | null;
  ttl_days?: number;
  predicates?: PocketPredicateInput[];
}

export interface PocketUpdate {
  defining_sql?: string;
  refresh_policy?: "schedule" | "manual" | "event";
  refresh_cron?: string | null;
  incremental_column?: string | null;
  incremental_lookback_hours?: number | null;
  ttl_days?: number;
  status?: string;
}

export interface PocketPredicate {
  id: string;
  pocket_definition_id: string;
  column_name: string;
  operator: string;
  value_json: Record<string, unknown>;
  created_at: string;
}

export interface PocketDefinition {
  id: string;
  model_id: string;
  target_id: string;
  physical_table_name: string;
  target_schema: string | null;
  defining_sql: string;
  query_fingerprint: string;
  predicate_set_hash: string;
  row_count: number | null;
  storage_bytes: number | null;
  refresh_policy: string;
  refresh_cron: string | null;
  incremental_column: string | null;
  incremental_lookback_hours: number | null;
  ttl_days: number;
  status: string;
  failure_reason: string | null;
  last_refresh_at: string | null;
  last_access_at: string | null;
  last_match_at: string | null;
  hit_count: number;
  time_saved_ms_total: number;
  // Bug-6991: align to PocketDefinitionResponse (aggregates_security.py).
  // Derived-grain row manifest (Bug-7359). NOT descriptive since
  // Bug-8018/Bug-8393: row_manifest.columns records the output columns the
  // built pocket table exposes and is what admits the pocket under active
  // row-level security. active_refresh_run_id is the live pointer to the
  // refresh run those columns describe; the pair is only trusted while the two
  // agree. Read-only in the UI.
  row_manifest?: Record<string, unknown> | null;
  active_refresh_run_id?: string | null;
  created_at: string;
  updated_at: string;
  retired_at: string | null;
  predicates: PocketPredicate[];
  // Optional child refresh-policy row (PocketRefreshPolicyResponse); null when
  // the pocket has no schedule configured.
  refresh_policy_row?: PocketRefreshPolicy | null;
}

export interface PocketRefreshRun {
  id: string;
  pocket_definition_id: string;
  refresh_mode: string;
  status: string;
  started_at: string;
  completed_at: string | null;
  rows_written: number | null;
  bytes_processed: number | null;
  error_message: string | null;
  triggered_by: string;
}

export interface PocketRefreshPolicy {
  id: string;
  pocket_definition_id: string;
  cron_expression: string | null;
  is_enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface PocketViolationItem {
  code: string;
  message: string;
  suggestion?: string | null;
}

export interface PocketValidateResponse {
  ok: boolean;
  stage: string;
  error?: string | null;
  warning?: string | null;
  columns?: string[] | null;
  violations?: PocketViolationItem[] | null;
}

export interface PocketDryRunResponse {
  ok: boolean;
  row_count?: number | null;
  elapsed_ms?: number | null;
  error?: string | null;
}
