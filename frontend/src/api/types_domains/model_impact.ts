/**
 * Impact Analysis wire contract types (Bug-7787, Phase 4).
 *
 * These types mirror the backend shared/schemas/domains/model_impact.py 1:1.
 * Field names match exactly for producer/consumer alignment.
 */

// --- request types ---

export interface ImpactTarget {
  object_type: string;
  object_id: string;
}

export interface ImpactChange {
  change_kind: "rename" | "rebind" | "definition" | "relationship" | "classification";
  changed_fields: string[];
  proposed_values: Record<string, unknown>;
}

export type ImpactOperation = "inspect" | "delete" | "change";

export interface ImpactQueryRequest {
  target: ImpactTarget;
  operation: ImpactOperation;
  change?: ImpactChange | null;
  include_cross_model?: boolean;
}

// --- response types ---

export type ImpactSeverity = "hard_break" | "soft_degrade" | "informational";
export type ImpactEffect =
  | "breaks_reference"
  | "changes_semantics"
  | "loses_coverage"
  | "loses_visibility"
  | "cascade_deleted"
  | "detached"
  | "stale"
  | "cleanup";
export type ImpactDeletePolicy = "restrict" | "cascade" | "detach" | "invalidate" | "recompute";
export type ImpactGuardDecision =
  | "allowed"
  | "blocked"
  | "acknowledgement_required"
  | "blocked_unresolved";

export interface ImpactObjectRef {
  object_type: string;
  object_id: string;
  model_id: string;
  name: string;
  display_name: string;
  route: string | null;
}

export interface ImpactPathEdge {
  kind: string;
  source_field: string;
}

export interface ImpactPathModel {
  nodes: string[];
  edges: ImpactPathEdge[];
}

export interface ImpactItem {
  impact_id: string;
  object: ImpactObjectRef;
  severity: ImpactSeverity;
  effect: ImpactEffect;
  delete_policy: ImpactDeletePolicy;
  direct: boolean;
  min_depth: number;
  reason_key: string;
  reason_params: Record<string, string>;
  paths: ImpactPathModel[];
  scc_id: number | null;
}

export interface ImpactGuard {
  decision: ImpactGuardDecision;
  blocking_impact_ids: string[];
  acknowledgement_required: boolean;
}

export interface ImpactSummaryModel {
  total: number;
  hard_break: number;
  soft_degrade: number;
  cascade_deleted: number;
  direct: number;
  max_depth: number;
  by_object_type: Record<string, number>;
  truncated: boolean;
  unresolved: number;
}

export interface ImpactResponse {
  analysis_id: string;
  authority: "live_draft";
  project_id: string;
  model_id: string;
  dependency_revision: number;
  operation: ImpactOperation;
  target: ImpactObjectRef;
  guard: ImpactGuard;
  summary: ImpactSummaryModel;
  impacts: ImpactItem[];
  cycles: string[][];
  diagnostics: Record<string, string>[];
}

// --- catalogue types ---

export interface ImpactCatalogueItem {
  object_type: string;
  object_id: string;
  model_id: string;
  name: string;
  display_name: string;
  container_ids: Record<string, string>;
  route: string | null;
}

export interface ImpactCatalogueResponse {
  project_id: string;
  model_id: string;
  dependency_revision: number;
  total: number;
  items: ImpactCatalogueItem[];
  next_cursor: string | null;
}

// --- guard conflict envelope ---

export interface ModelDependencyConflict {
  code: string;
  message_key: string;
  dependency_revision: number;
  impact: ImpactResponse | null;
}
