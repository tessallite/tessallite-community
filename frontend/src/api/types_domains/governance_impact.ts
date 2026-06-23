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

// ---------------------------------------------------------------------------
// Impact Analysis — Downstream Assets
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
  members?: (string | { key: string })[];
  entity?: string;
  count?: number;
  measure?: string;
  direction?: "top" | "bottom";
  conditions?: { field: string; operator: string; value: string | number }[];
  logic?: "AND" | "OR";
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

