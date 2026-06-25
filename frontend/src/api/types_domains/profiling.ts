// ---------------------------------------------------------------------------
// Cost / ROI Advisor
// ---------------------------------------------------------------------------
export interface AggregateROI {
  aggregate_id: string;
  physical_table_name: string;
  status: string;
  hit_count: number;
  storage_bytes: number | null;
  agg_row_count: number | null;
  roi_score: number;
}

export interface ROISummaryItem {
  model_id: string;
  model_name: string;
  aggregate_count: number;
  total_hit_count: number;
  total_storage_bytes: number;
  top_roi_score: number;
}

// ---------------------------------------------------------------------------
// Table Profiling
// ---------------------------------------------------------------------------
export interface ProfiledColumn {
  column_name: string;
  data_type: string;
  is_nullable: boolean;
  approx_distinct: number | null;
  cardinality_ratio: number | null;
  suggested_role: "measure" | "dimension" | "time_dimension";
  suggested_agg: string | null;
}
export interface ProfiledTable {
  schema: string;
  table: string;
  /**
   * F-014-13: the `/profile` endpoint only ever emits `fact`, `dim_aggregate`,
   * or `dim_detail` (see `_classify_table`). `unclassified` and `calendar` are
   * retained in the union so this type stays assignable to the shared
   * `table_type` vocabulary used by the SourcesPanel edit/guard logic
   * (e.g. the `classification !== "unclassified"` guard); they are never
   * produced by profiling itself.
   */
  classification: "fact" | "dim_aggregate" | "dim_detail" | "unclassified" | "calendar";
  row_count: number;
  /**
   * False when the source connector could not return per-column distinct counts
   * for this table, so cardinality-based auto-classify signals were skipped and
   * the suggestions are weaker. Surfaced as a degradation note in the wizard.
   */
  cardinality_available?: boolean;
  columns: ProfiledColumn[];
}

