// ---------------------------------------------------------------------------
// Connections
// ---------------------------------------------------------------------------
export interface ConnectionCreate {
  display_name: string;
  connection_type: "bigquery" | "postgresql" | "hadoop_spark" | "snowflake" | "redshift" | "sqlserver";
  credentials: Record<string, unknown>;
  config?: Record<string, unknown>;
}
export interface Connection {
  id: string;
  display_name: string;
  connection_type: string;
  config: Record<string, unknown>;
  credentials_preview?: Record<string, unknown>;
  created_at: string;
}

// ---------------------------------------------------------------------------
// Models
// ---------------------------------------------------------------------------
export interface ModelCreate {
  slug: string;
  display_name?: string;
  aggregations_enabled?: boolean;
  include_all_measures?: boolean;
  max_aggregates?: number;
  miss_threshold_daily?: number;
  miss_threshold_weekly?: number;
  pocket_size_budget_bytes?: number | null;
}
export interface CanvasLayout {
  tables?: Record<string, { x: number; y: number; w?: number; h?: number }>;
  edges?: Record<string, {
    waypoint?: { x: number; y: number };
    waypoints?: { x: number; y: number }[];
    pathing?: "orthogonal" | "straight";
    sourceSide?: string;
    targetSide?: string;
    sourceRatio?: number;
    targetRatio?: number;
  }>;
  viewport?: { x: number; y: number; zoom: number };
  notes?: string;
}
export type EvictionPolicy =
  | "predicted_first"
  | "lru"
  | "validated_survives"
  | "never_evict";

export interface ModelUpdate {
  slug?: string;
  display_name?: string;
  description?: string;
  refresh_strategy?: string;
  status?: string;
  aggregations_enabled?: boolean;
  include_all_measures?: boolean;
  max_aggregates?: number;
  miss_threshold_daily?: number;
  miss_threshold_weekly?: number;
  canvas_layout?: CanvasLayout;
  predictive_storage_budget_bytes?: number | null;
  predictive_storage_budget_count?: number | null;
  predictive_eviction_policy?: EvictionPolicy;
  predictive_requires_approval?: boolean;
  pocket_size_budget_bytes?: number | null;
}
export interface Model {
  id: string;
  slug: string;
  display_name: string;
  status: string;
  aggregations_enabled: boolean;
  include_all_measures?: boolean;
  max_aggregates: number;
  canvas_layout?: CanvasLayout;
  deployed_version_id?: string | null;
  last_deployed_at?: string | null;
  deployed_version_number?: number | null;
  last_saved_version_number?: number | null;
  created_at: string;
  predictive_storage_budget_bytes?: number | null;
  predictive_storage_budget_count?: number | null;
  predictive_eviction_policy?: EvictionPolicy;
  predictive_requires_approval?: boolean;
  pocket_size_budget_bytes?: number | null;
}

