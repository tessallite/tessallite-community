// ---------------------------------------------------------------------------
// KPIs
// ---------------------------------------------------------------------------

/** Canonical KPI type identifiers. */
export type KpiType =
  | "simple_measure"
  | "ratio"
  | "variance"
  | "growth_rate"
  | "moving_window"
  | "composite";

/** Aggregation modes for KPI calculation. */
export type CalcAggMode =
  | "automatic"
  | "aggregate_first"
  | "row_first"
  | "aggregate_of_aggregate"
  | "pre_aggregated";

/** Performance direction semantics. */
export type Direction =
  | "higher_is_better"
  | "lower_is_better"
  | "closer_is_better";

/** Target value source type. */
export type TargetType =
  | "none"
  | "static"
  | "measure"
  | "prior_period"
  | "expression";

/** Visual presentation type for status indicator. */
export type PresentationType =
  | "traffic_light"
  | "gauge"
  | "reverse_gauge" // deprecated — treated as "gauge" at runtime
  | "bullet_chart"
  | "rag_bar"
  | "progress_ring"
  | "thermometer"
  | "speedometer"; // deprecated — treated as "gauge" at runtime

/** Value formatting tokens. */
export type FormatToken =
  | "currency"
  | "currency_k"
  | "percent"
  | "percent_decimal"
  | "decimal_0dp"
  | "decimal_1dp"
  | "decimal_2dp"
  | "integer"
  | "custom";

/** Indicator classification. */
export type IndicatorType = "none" | "leading" | "lagging";

/** Certification lifecycle status. */
export type CertificationStatus =
  | "draft"
  | "shared"
  | "certified"
  | "deprecated";

/** A single threshold band. */
export interface KpiThresholdBand {
  label: string;
  color: string;
  min: number | null;
  max: number | null;
}

/** Presentation metadata stored on the KPI. */
export interface KpiPresentationMeta {
  bands?: KpiThresholdBand[];
  peer_dimension?: string;
  evaluation_type?: string;
  colorblind?: boolean;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// Business Builder types (v3)
// ---------------------------------------------------------------------------

export type BusinessFormulaType =
  | "single_measure"
  | "ratio"
  | "count_records"
  | "count_distinct"
  | "moving_average"
  | "compare_periods"
  | "compare_measures"
  | "target_comparison"
  | "exception_sla"
  | "share_rank"
  | "composite_score";

export type TimeCalculationType =
  | "current"
  | "prior_period"
  | "period_to_date"
  | "trailing_sum"
  | "moving_average"
  | "lag"
  | "lead"
  | "percentage_change"
  | "yoy_value"
  | "yoy_growth_pct"
  | "cagr";

export type TimeWindowPreset =
  | "today"
  | "this_week"
  | "last_week"
  | "last_complete_week"
  | "this_month"
  | "last_month"
  | "last_complete_month"
  | "this_quarter"
  | "last_quarter"
  | "last_complete_quarter"
  | "this_year"
  | "last_year"
  | "last_complete_year"
  | "last_7_days"
  | "last_14_days"
  | "last_30_days"
  | "last_60_days"
  | "last_90_days"
  | "last_3_months"
  | "last_6_months"
  | "last_12_months"
  | "custom_range";

// Bug-5924: top_n/bottom_n were advertised in this union and the backend
// FILTER_OPERATORS set, but the compiler always rejected them ("requires
// measure-based ranking" — not implemented) and the UI never rendered
// them. Removed from the public contract rather than left half-advertised;
// see docs/execution/execution_future-features.md for the ranking-filter
// feature request if it is approved for a later phase.
export type BusinessFilterOp =
  | "eq"
  | "ne"
  | "gt"
  | "gte"
  | "lt"
  | "lte"
  | "in"
  | "not_in"
  | "between"
  | "like"
  | "not_like"
  | "is_null"
  | "is_not_null";

export type PeriodGrain = "day" | "week" | "month" | "quarter" | "year";

export interface BusinessTimeCalculation {
  type: TimeCalculationType;
  grain?: PeriodGrain;
  periods?: number;
  period?: string;
}

export interface BusinessFormula {
  type: BusinessFormulaType;
  measure_id?: string;
  numerator_measure_id?: string;
  denominator_measure_id?: string;
  measure_a_id?: string;
  measure_b_id?: string;
  dimension_id?: string;
  aggregation?: string;
  window_size?: number;
  grain?: PeriodGrain;
  comparison?: string;
  mode?: string;
  sla_type?: string;
  comparator?: string;
  threshold_value?: number;
  condition?: Record<string, unknown>;
  share_type?: string;
  n?: number;
  by_dimension_id?: string;
  by_measure_id?: string;
  components?: Array<{
    measure_id?: string;
    kpi_name?: string;
    weight?: number;
  }>;
  time_calculation?: BusinessTimeCalculation;
  numerator_time_calculation?: BusinessTimeCalculation;
  denominator_time_calculation?: BusinessTimeCalculation;
}

export interface BusinessTimeWindow {
  dimension_id?: string;
  preset?: TimeWindowPreset;
  start?: string;
  end?: string;
  include_incomplete_period?: boolean;
}

export interface BusinessFilter {
  dimension_id: string;
  operator: BusinessFilterOp;
  values?: string[];
  value?: string;
  mode?: "fixed" | "relative" | "parameter";
  parameter_name?: string;
  default_value?: string | string[];
  label?: string;
  description?: string;
}

export interface BusinessTarget {
  type: "static" | "measure" | "expression";
  value?: number;
  measure_id?: string;
  expression?: string;
}

export interface BusinessDefinition {
  builder: "business_kpi";
  version: 1;
  formula: BusinessFormula;
  time_window?: BusinessTimeWindow;
  filters?: BusinessFilter[];
  target?: BusinessTarget;
  direction?: Direction;
  _compiled?: {
    expression: string;
    filter_predicates: string[];
    time_window_predicates: string[];
    where_clause: string | null;
    summary: string;
    summary_tokens?: Record<string, unknown>;
  };
}

export interface KpiCreate {
  name: string;
  display_name?: string | null;
  description?: string | null;
  display_folder?: string | null;
  // v2 expression DSL
  kpi_type?: KpiType | null;
  expression?: string | null;
  calc_agg_mode?: CalcAggMode;
  inner_agg?: string | null;
  inner_grain?: string | null;
  outer_agg?: string | null;
  // Semi-additive
  at_grain?: string | null;
  non_additive_agg?: string | null;
  carry_forward?: boolean;
  // Target
  target_type?: TargetType | null;
  target_value?: number | null;
  target_measure_id?: string | null;
  target_expression?: string | null;
  target_period?: string | null;
  // Direction and thresholds
  direction?: Direction;
  presentation_type?: PresentationType | null;
  presentation_meta?: KpiPresentationMeta | null;
  // Trend
  trend_period?: string;
  trend_threshold?: number;
  trend_sparkline_periods?: number;
  // Formatting
  format_token?: FormatToken | null;
  format_custom?: string | null;
  unit_label?: string | null;
  null_display_value?: string;
  // Hierarchy / composition
  weight?: number | null;
  parent_kpi_id?: string | null;
  indicator_type?: IndicatorType;
  // Time dimension binding
  time_dimension_id?: string | null;
  // Business builder definition (v3)
  business_definition?: BusinessDefinition | null;
  // Governance
  owner_user_id?: string | null;
  // Snapshots
  snapshot_frequency?: string | null;
  snapshot_retention?: number;
  status_graphic?: string;
  trend_graphic?: string;
}

export interface KpiUpdate {
  name?: string;
  display_name?: string | null;
  description?: string | null;
  display_folder?: string | null;
  // v2 expression DSL
  kpi_type?: KpiType | null;
  expression?: string | null;
  calc_agg_mode?: CalcAggMode | null;
  inner_agg?: string | null;
  inner_grain?: string | null;
  outer_agg?: string | null;
  // Semi-additive
  at_grain?: string | null;
  non_additive_agg?: string | null;
  carry_forward?: boolean | null;
  // Target
  target_type?: TargetType | null;
  target_value?: number | null;
  target_measure_id?: string | null;
  target_expression?: string | null;
  target_period?: string | null;
  // Direction and thresholds
  direction?: Direction | null;
  presentation_type?: PresentationType | null;
  presentation_meta?: KpiPresentationMeta | null;
  // Trend
  trend_period?: string | null;
  trend_threshold?: number | null;
  trend_sparkline_periods?: number | null;
  // Formatting
  format_token?: FormatToken | null;
  format_custom?: string | null;
  unit_label?: string | null;
  null_display_value?: string | null;
  // Hierarchy / composition
  weight?: number | null;
  parent_kpi_id?: string | null;
  indicator_type?: IndicatorType | null;
  // Time dimension binding
  time_dimension_id?: string | null;
  // Business builder definition (v3)
  business_definition?: BusinessDefinition | null;
  // Governance
  certification_status?: CertificationStatus | null;
  owner_user_id?: string | null;
  // Deployment
  is_deployed?: boolean | null;
  // Snapshots
  snapshot_frequency?: string | null;
  snapshot_retention?: number | null;
  status_graphic?: string | null;
  trend_graphic?: string | null;
}

export interface Kpi {
  id: string;
  model_id: string;
  name: string;
  display_name: string | null;
  description: string | null;
  display_folder: string | null;
  // v2 fields
  kpi_type: KpiType | null;
  expression: string | null;
  calc_agg_mode: CalcAggMode;
  inner_agg: string | null;
  inner_grain: string | null;
  outer_agg: string | null;
  at_grain: string | null;
  non_additive_agg: string | null;
  carry_forward: boolean;
  target_type: TargetType | null;
  target_value: number | null;
  target_measure_id: string | null;
  target_expression: string | null;
  target_period: string | null;
  direction: Direction;
  presentation_type: PresentationType | null;
  presentation_meta: KpiPresentationMeta | null;
  trend_period: string;
  trend_threshold: number;
  trend_sparkline_periods: number;
  format_token: FormatToken | null;
  format_custom: string | null;
  unit_label: string | null;
  null_display_value: string;
  weight: number | null;
  parent_kpi_id: string | null;
  indicator_type: IndicatorType | null;
  evaluation_order: number | null;
  time_dimension_id: string | null;
  business_definition: BusinessDefinition | null;
  certification_status: CertificationStatus;
  replacement_id: string | null;
  owner_user_id: string | null;
  is_deployed: boolean | null;
  deployed_at: string | null;
  snapshot_frequency: string | null;
  snapshot_retention: number | null;
  created_at: string;
  updated_at: string;
  created_by: string | null;
  // Legacy v1 fields — deprecated, retained for API response backward compatibility
  value_measure_id: string | null;
  goal_measure_id: string | null;
  status_expression: string | null;
  trend_expression: string | null;
  status_graphic: string | null;
  trend_graphic: string | null;
}

export interface KpiTrendPoint {
  period: string;
  value: number | null;
}

export interface KpiEvaluateResponse {
  kpi_id: string | null;
  value: number | null;
  value_str: string | null;
  target: number | null;
  status: number | null;
  status_label: string | null;
  status_color: string | null;
  // Bug-1226: the authoritative gauge position the backend matched to `status`,
  // and the bands it matched against. The gauge plots its needle from
  // `status_position` against `status_bands` so the needle, band colour and
  // status badge share one scale and cannot disagree for any evaluation type.
  status_position?: number | null;
  status_bands?: KpiThresholdBand[] | null;
  trend: number | null;
  trend_label: string | null;
  trend_pct: number | null;
  // F-017-02 (Bug-7238/Bug-7988): direction-normalised percentage change.
  // Positive means IMPROVING, negative means DECLINING, regardless of the KPI's
  // direction preference. The scorecard improvement chip must render THIS field
  // so the sign always agrees with the colour; raw `trend_pct` is only for a
  // labelled raw-change detail. For a lower-is-better cost falling 100 -> 80 the
  // backend emits trend_pct=-0.2 (raw) and trend_pct_normalised=+0.2 (improving).
  trend_pct_normalised: number | null;
  formatted_value: string | null;
  formatted_target: string | null;
  formatted_variance: string | null;
  trend_series: KpiTrendPoint[] | null;
  evaluation_ms: number | null;
  compiled_expression: string | null;
  compiled_scope: Record<string, unknown> | null;
  // Actual SQL sent to the query gateway for execution (may be several
  // statements for time-intelligence KPIs).
  compiled_sql?: string | null;
  // Bug-4255 / Bug-8449: composite-KPI health. "restricted" means row security
  // withheld at least one child, so the backend refused to publish a partial
  // score. `errored_children` lists broken inputs for degraded/error outcomes.
  composite_status?: "ok" | "degraded" | "error" | "restricted" | null;
  errored_children?: KpiErroredChild[] | null;
  // Bug-8449 / Bug-8427: true when this KPI produced no value BECAUSE the
  // model's row-level security denied the caller every row, rather than
  // because the slice is empty or the expression is broken. Render an explicit
  // "restricted" state, never the generic "N/A" no-data state — otherwise a
  // modeller debugs a perfectly correct measure. Absent/false does NOT mean
  // the caller saw every row: a narrowing rule may still have applied and the
  // value is then correct FOR THAT CALLER.
  row_security_restricted?: boolean | null;
  // F-017-09: which engine produced this value — "sql" (compiled + executed on
  // the gateway), "python" (SQL compiler could not handle the expression so the
  // model-service Python evaluator answered), or "refused". Shown as a chip when
  // not "sql" so a SQL-vs-Python divergence is visible. fallback_reason explains.
  evaluation_path?: string | null;
  fallback_reason?: string | null;
  // Legacy fields for v1 compatibility
  goal: number | null;
  formatted_goal: string | null;
}

/** A composite child KPI whose evaluation failed (Bug-4255). */
export interface KpiErroredChild {
  kpi_id: string;
  kpi_name: string;
  error_reason: string;
}

export interface KpiValidationDiagnostic {
  code: string;
  message: string;
  position: { line: number; column: number; offset: number } | null;
  suggestion: string | null;
}

export interface KpiValidationResponse {
  valid: boolean;
  errors: KpiValidationDiagnostic[];
  warnings: KpiValidationDiagnostic[];
  referenced_measures: string[];
  referenced_kpis: string[];
  referenced_dimensions: string[];
  has_time_intelligence: boolean;
  requires_time_dimension: boolean;
  detected_agg_mode: CalcAggMode | null;
  expression_tree: Record<string, unknown> | null;
  compiled_sql_preview: string | null;
}

export interface KpiValidateExpressionRequest {
  expression: string;
  target_expression?: string | null;
  direction?: Direction;
}

export interface KpiAdhocRequest {
  expression?: string | null;
  target_expression?: string | null;
  calc_agg_mode?: CalcAggMode;
  direction?: Direction;
  threshold_preset?: string | null;
  format_token?: FormatToken | null;
  format_custom?: string | null;
  unit_label?: string | null;
  presentation_meta?: KpiPresentationMeta | null;
  trend_period?: string | null;
  filters?: Record<string, unknown>[] | null;
  time_dimension?: string | null;
  business_definition?: BusinessDefinition | null;
}

export interface KpiBatchRequest {
  kpi_ids: string[];
  filters?: Record<string, unknown>[] | null;
}

export interface KpiBatchResponse {
  results: KpiEvaluateResponse[];
  evaluation_ms: number | null;
}

export interface KpiSnapshotResponse {
  id: string;
  kpi_id: string;
  snapshot_at: string;
  value: number | null;
  target: number | null;
  status: number | null;
  status_label: string | null;
  trend_pct: number | null;
  filters_applied: Record<string, unknown> | null;
  evaluation_ms: number | null;
  created_at: string;
}

export interface VersionEntry {
  id: string;
  version_number: number;
  changed_by: string | null;
  changed_at: string;
  change_summary: string | null;
  snapshot: Record<string, unknown>;
}

export interface DeprecateRequest {
  replacement_id?: string | null;
}

export interface EntityUsageCreate {
  workbook_id?: string;
  worksheet?: string;
  cell_reference?: string;
  usage_type: string;
}

export interface EntityUsageEntry {
  id: string;
  workbook_id: string | null;
  worksheet: string | null;
  cell_reference: string | null;
  usage_type: string;
  reported_by: string | null;
  reported_at: string;
}

// Mirrors UserPreferenceToggle.entity_type in
// shared/schemas/domains/governance_advanced.py. "model" scopes by identity —
// entity_id must be the model in the path (Bug-8183 / Bug-8899).
export interface UserPreferenceToggle {
  entity_type: "kpi" | "named_set" | "model";
  entity_id: string;
}

export interface UserPreferencesResponse {
  favourites: Record<string, string[]>;
  recently_used: Record<string, string[]>;
}

/** Every model in one project the calling user has favourited. */
export interface FavouriteModelsResponse {
  model_ids: string[];
}

export interface PersonaTagRestrictionRequest {
  tag_ids: string[];
}

export interface PersonaTagRestriction {
  tag_id: string;
  tag_name: string;
  description: string | null;
  column_count: number;
}
