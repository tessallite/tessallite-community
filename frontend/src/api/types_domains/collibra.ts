// ---------------------------------------------------------------------------
// Collibra Integration Types
// ---------------------------------------------------------------------------

export interface CollibraConnectionCreate {
  display_name: string;
  base_url: string;
  auth_type?: string;
  token: string;
  community_id?: string | null;
  domain_id?: string | null;
  asset_type_mapping?: Record<string, string>;
  relation_type_mapping?: Record<string, string>;
  responsibility_mapping?: Record<string, string>;
  sync_scope?: string;
  sync_mode?: string;
}

export interface CollibraConnectionUpdate {
  display_name?: string;
  base_url?: string;
  auth_type?: string;
  token?: string;
  community_id?: string | null;
  domain_id?: string | null;
  asset_type_mapping?: Record<string, string>;
  relation_type_mapping?: Record<string, string>;
  responsibility_mapping?: Record<string, string>;
  sync_scope?: string;
  sync_mode?: string;
  is_active?: boolean;
}

export interface CollibraConnection {
  id: string;
  project_id: string;
  model_id: string;
  display_name: string;
  base_url: string;
  auth_type: string;
  community_id: string | null;
  domain_id: string | null;
  sync_scope: string;
  sync_mode: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface CollibraValidateRequest {
  connection_id: string;
}

export interface CollibraValidateResponse {
  // ok is tri-state: true = verified live, false = verified failed,
  // null = simulated (connector not contacted).
  ok: boolean | null;
  simulated: boolean;
  base_url: string;
  community_found: boolean | null;
  domain_found: boolean | null;
  missing_asset_types: string[];
  missing_relation_types: string[];
  warnings: string[];
}

export interface CollibraExportPreviewRequest {
  include_business_assets?: boolean;
  include_technical_assets?: boolean;
  include_hidden_objects?: boolean;
  include_glossary?: boolean;
  include_downstream_assets?: boolean;
  include_aggregates?: boolean;
  include_data_tags?: boolean;
  include_responsibilities?: boolean;
}

export interface CollibraExportPreviewResponse {
  assets_total: number;
  relations_total: number;
  attributes_total: number;
  responsibilities_total: number;
  by_asset_type: Record<string, number>;
  warnings: Record<string, unknown>[];
}

export interface CollibraSyncRequest {
  connection_id: string;
  dry_run?: boolean;
  include_technical_assets?: boolean;
  include_glossary?: boolean;
  include_downstream_assets?: boolean;
  include_aggregates?: boolean;
  include_data_tags?: boolean;
  include_responsibilities?: boolean;
  deprecate_missing?: boolean;
}

export interface CollibraSyncResponse {
  run_id: string;
  status: string;
  assets_total: number;
  relations_total: number;
  attributes_total: number;
  responsibilities_total: number;
  assets_created: number;
  assets_updated: number;
  relations_created: number;
  relations_updated: number;
  warnings: Record<string, unknown>[];
  error_message: string | null;
}

export interface CollibraSyncRun {
  id: string;
  connection_id: string;
  project_id: string;
  model_id: string | null;
  mode: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  tessallite_snapshot_hash: string | null;
  collibra_import_job_id: string | null;
  assets_total: number;
  relations_total: number;
  attributes_total: number;
  responsibilities_total: number;
  assets_created: number;
  assets_updated: number;
  relations_created: number;
  relations_updated: number;
  error_message: string | null;
}

export interface CollibraObjectMapping {
  id: string;
  connection_id: string;
  tessallite_object_type: string;
  tessallite_object_id: string;
  tessallite_stable_key: string;
  collibra_resource_type: string;
  collibra_resource_id: string | null;
  collibra_full_name: string | null;
  last_payload_hash: string;
  last_synced_at: string;
  last_sync_run_id: string | null;
  is_deprecated: boolean;
}
