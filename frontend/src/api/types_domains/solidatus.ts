// ---------------------------------------------------------------------------
// Solidatus Integration Types
// ---------------------------------------------------------------------------

export interface SolidatusConnectionCreate {
  display_name: string;
  base_url: string;
  auth_type?: string;
  token: string;
  workspace_id?: string | null;
  model_ref?: string | null;
  sync_scope?: string;
}

export interface SolidatusConnectionUpdate {
  display_name?: string;
  base_url?: string;
  auth_type?: string;
  token?: string;
  workspace_id?: string | null;
  model_ref?: string | null;
  sync_scope?: string;
  is_active?: boolean;
}

export interface SolidatusConnection {
  id: string;
  project_id: string;
  model_id: string;
  display_name: string;
  base_url: string;
  auth_type: string;
  workspace_id: string | null;
  model_ref: string | null;
  sync_scope: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface SolidatusValidateRequest {
  connection_id: string;
}

export interface SolidatusValidateResponse {
  // ok is tri-state: true = verified live, false = verified failed,
  // null = simulated (connector not contacted).
  ok: boolean | null;
  simulated: boolean;
  base_url: string;
  workspace_found: boolean | null;
  model_ref_found: boolean | null;
  warnings: string[];
}

export interface SolidatusExportPreviewRequest {
  include_technical?: boolean;
  include_aggregates?: boolean;
  include_downstream_assets?: boolean;
  include_glossary?: boolean;
  include_security_tags?: boolean;
}

export interface SolidatusExportPreviewResponse {
  nodes_total: number;
  edges_total: number;
  by_type: Record<string, number>;
  warnings: Record<string, unknown>[];
}

export interface SolidatusSyncRequest {
  connection_id: string;
  mode: "dry_run" | "push";
  include_technical?: boolean;
  include_aggregates?: boolean;
  include_downstream_assets?: boolean;
  include_glossary?: boolean;
  include_security_tags?: boolean;
}

export interface SolidatusSyncResponse {
  run_id: string;
  status: string;
  nodes_total: number;
  edges_total: number;
  nodes_created: number;
  nodes_updated: number;
  edges_created: number;
  edges_updated: number;
  error_message: string | null;
}

export interface SolidatusSyncRun {
  id: string;
  connection_id: string;
  project_id: string;
  model_id: string | null;
  mode: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  tessallite_snapshot_hash: string | null;
  solidatus_target_ref: string | null;
  nodes_total: number;
  edges_total: number;
  nodes_created: number;
  nodes_updated: number;
  edges_created: number;
  edges_updated: number;
  error_message: string | null;
}

export interface SolidatusObjectMapping {
  id: string;
  connection_id: string;
  tessallite_object_type: string;
  tessallite_object_id: string;
  tessallite_stable_key: string;
  solidatus_object_id: string | null;
  solidatus_object_ref: string | null;
  last_payload_hash: string;
  last_synced_at: string;
  last_sync_run_id: string | null;
}
