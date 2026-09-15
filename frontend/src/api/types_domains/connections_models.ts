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
  tables?: Record<string, {
    x: number;
    y: number;
    w?: number;
    h?: number;
    /**
     * Auto-placement protection for this table. Absent means false, so a layout
     * written before pinning existed stays valid.
     */
    pinned?: boolean;
  }>;
  edges?: Record<string, {
    waypoint?: { x: number; y: number };
    waypoints?: { x: number; y: number }[];
    pathing?: "orthogonal" | "straight";
    sourceSide?: string;
    targetSide?: string;
    sourceRatio?: number;
    targetRatio?: number;
    /**
     * The complete displayed route and docking for this relationship are
     * frozen. Independent of `tables[id].pinned`; unlocking never unpins.
     */
    locked?: boolean;
    /**
     * Provenance of the stored bends. Legacy entries with saved waypoints but no
     * `routeMode` are treated as "manual".
     */
    routeMode?: "auto" | "manual";
    /**
     * The parallel fan-out in force when this route was locked.
     *
     * The fan-out is normally derived from the current set of relationships
     * between the two cards, so adding one moved an existing LOCKED
     * attachment while its frozen bends stayed absolute. A locked route draws
     * with the offset it was frozen with. Absent on an unlocked route and on
     * every layout saved before locks carried it.
     */
    lockedParallelOffset?: number;
    /**
     * `pathing` was written by the lock, not chosen by the user.
     *
     * A locked route must own its resolved path mode, or a later change to the
     * model-wide Edge Pathing setting discards the bends the lock froze.
     * Recording that the override is the lock's own doing is what lets Unlock
     * return the relationship to the model setting instead of leaving a
     * permanent style override nobody asked for.
     */
    pathingFrozenByLock?: boolean;
  }>;
  /**
   * Last successfully applied layout preferences. Absent or unknown values fall
   * back to validated defaults.
   */
  layoutOptions?: {
    preset?: "hierarchical" | "compact" | "radial";
    direction?: "DOWN" | "RIGHT";
    spacing?: "normal" | "dense";
  };
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
  // Bug-8101 / F-104-01: whether the current caller may author (mutate) this
  // model. False for a read-only consumer role (model_viewer / viewer), which
  // makes the Model Builder open read-only. Absent on list responses.
  caller_can_author?: boolean | null;
  // G-013-02: whether the caller holds project ADMIN for this model (the
  // revert route's require_role("admin") precedence). Drives the Versions
  // dialog Revert button so a project-scoped admin sees it. Absent on lists.
  caller_can_admin?: boolean | null;
}

