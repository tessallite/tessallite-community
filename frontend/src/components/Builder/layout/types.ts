export type LayoutPreset = "hierarchical" | "compact" | "radial";
export type LayoutDirection = "DOWN" | "RIGHT";
export type LayoutSpacing = "normal" | "dense";
export type LayoutOperation = "arrange-all" | "arrange-selected" | "reroute-links" | "repair-routes";
export type RouteMode = "auto" | "manual";
export type PathMode = "orthogonal" | "straight";
export type AnchorSide = "left" | "right" | "top" | "bottom";

export interface Point {
  x: number;
  y: number;
}

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface LayoutNodeSnapshot extends Rect {
  id: string;
  tableType: string;
  pinned: boolean;
  fixed: boolean;
  selected: boolean;
  measured: boolean;
}

export interface LayoutEdgeSnapshot {
  id: string;
  source: string;
  target: string;
  sourceColumn?: string | null;
  targetColumn?: string | null;
  /** Persisted base anchor ratio; the renderer re-applies the parallel offset. */
  sourceSide?: AnchorSide;
  targetSide?: AnchorSide;
  sourceRatio?: number;
  targetRatio?: number;
  /** Parallel-relationship docking inputs, mirroring the renderer's edge data. */
  offsetIndex?: number;
  totalEdges?: number;
  pathMode: PathMode;
  routeMode: RouteMode;
  waypoints: Point[];
  locked: boolean;
  /** Drawn marker extent per endpoint; the heel is border + outward * extent. */
  sourceMarkerExtent?: number;
  targetMarkerExtent?: number;
}

export interface LayoutOptions {
  preset: LayoutPreset;
  direction: LayoutDirection;
  spacing: LayoutSpacing;
}

export interface LayoutScope {
  projectId: string;
  modelId: string;
}

export interface LayoutSnapshot {
  scope: LayoutScope;
  revision: number;
  nodes: LayoutNodeSnapshot[];
  edges: LayoutEdgeSnapshot[];
  options: LayoutOptions;
}

export interface LayoutRoute {
  edgeId: string;
  /** Heel-to-heel polyline; `points[0]`/`points.at(-1)` are the marker heels. */
  points: Point[];
  /** `points` without the two heel endpoints — the persisted waypoint list. */
  waypoints: Point[];
  sourceSide: AnchorSide;
  targetSide: AnchorSide;
  /** Persisted base ratios (parallel offset NOT baked in); renderer re-applies. */
  sourceRatio: number;
  targetRatio: number;
  pathMode: PathMode;
  routeMode: RouteMode;
  locked: boolean;
}

export interface LayoutMetrics {
  nodeOverlapCount: number;
  throughNodeSegmentCount: number;
  edgeCrossingCount: number;
  totalBends: number;
  totalLength: number;
  elapsedMs: number;
}

export interface LayoutResult {
  kind: "success";
  scope: LayoutScope;
  revision: number;
  positions: Record<string, Point>;
  routes: Record<string, LayoutRoute>;
  options: LayoutOptions;
  metrics: LayoutMetrics;
}

export type LayoutFailureCode =
  | "invalid-input"
  | "engine-unavailable"
  | "no-route"
  | "geometry-invalid"
  | "cancelled"
  | "timeout"
  /** A newer model, revision or gesture replaced this request before it landed. */
  | "superseded"
  | "unknown";

export interface LayoutFailure {
  kind: "failure";
  scope: LayoutScope;
  revision: number;
  code: LayoutFailureCode;
  message: string;
}

export type LayoutWorkerRequest = {
  type: "layout";
  operation: LayoutOperation;
  requestId: string;
  snapshot: LayoutSnapshot;
};

export type LayoutWorkerCancel = {
  type: "cancel";
  requestId: string;
};

export type LayoutWorkerMessage = LayoutWorkerRequest | LayoutWorkerCancel;
export type LayoutWorkerResponse = LayoutResult | LayoutFailure;

export interface LayoutEngineRoute {
  edgeId: string;
  points: Point[];
}

export interface LayoutEngineOutput {
  positions: Record<string, Point>;
  routes: LayoutEngineRoute[];
}

export interface LayoutEngineOptions {
  nodeSpacing: number;
  layerSpacing: number;
  direction: LayoutDirection;
}
