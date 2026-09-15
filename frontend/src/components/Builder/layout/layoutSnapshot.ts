/**
 * Translate canvas state into the worker's serialisable layout snapshot.
 *
 * Pure and engine-free so it can be unit tested, and deliberately the *only*
 * place that decides how canvas state maps onto the worker contract. In
 * particular it resolves the two values the renderer also derives for itself —
 * the endpoint marker extents and the parallel-relationship offset — through the
 * shared `./docking` functions, so a worker route and the drawn route dock at
 * the same point.
 */
import { markerExtent, resolveMarkerKinds, type RelationNotation } from "./docking";
import type {
  AnchorSide,
  LayoutEdgeSnapshot,
  LayoutNodeSnapshot,
  LayoutOptions,
  LayoutSnapshot,
  PathMode,
  Point,
  RouteMode,
} from "./types";

export interface CanvasSnapshotNode {
  id: string;
  position: Point;
  /** DOM-measured size when available, otherwise the persisted provisional size. */
  measuredWidth?: number;
  measuredHeight?: number;
  provisionalWidth?: number;
  provisionalHeight?: number;
  tableType: string;
  pinned?: boolean;
  selected?: boolean;
}

export interface CanvasSnapshotEdge {
  id: string;
  source: string;
  target: string;
  sourceColumn?: string | null;
  targetColumn?: string | null;
  sourceSide?: string;
  targetSide?: string;
  sourceRatio?: number;
  targetRatio?: number;
  offsetIndex?: number;
  totalEdges?: number;
  pathing?: PathMode;
  /** Legacy single waypoint; converted to the array form here. */
  waypoint?: Point;
  waypoints?: Point[];
  locked?: boolean;
  routeMode?: RouteMode;
  sourceIsFact: boolean;
  targetIsFact: boolean;
  sourceIsDim: boolean;
  targetIsDim: boolean;
  terminalOverride?: { source: string; target: string };
  notation: RelationNotation;
}

export interface BuildSnapshotInput {
  projectId: string;
  modelId: string;
  revision: number;
  nodes: CanvasSnapshotNode[];
  edges: CanvasSnapshotEdge[];
  options: Partial<LayoutOptions>;
  /**
   * Global pathing preference the renderer falls back to when an edge carries no
   * explicit `pathing` override. Resolved here so the worker lays an inherited
   * Straight edge out as Straight rather than silently as Orthogonal (F08).
   */
  globalPathing?: PathMode;
}

function validSide(value: string | undefined): AnchorSide | undefined {
  return value === "left" || value === "right" || value === "top" || value === "bottom" ? value : undefined;
}

function finiteRatio(value: number | undefined): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

/**
 * Card size for layout purposes.
 *
 * The engine must never place variable-height cards using identical placeholder
 * sizes, so a card with neither a measurement nor a persisted size is an
 * explicit failure rather than a guess (spec §4).
 */
function resolveSize(node: CanvasSnapshotNode): { width: number; height: number; measured: boolean } {
  const measuredWidth = node.measuredWidth;
  const measuredHeight = node.measuredHeight;
  if (
    typeof measuredWidth === "number" && Number.isFinite(measuredWidth) && measuredWidth > 0 &&
    typeof measuredHeight === "number" && Number.isFinite(measuredHeight) && measuredHeight > 0
  ) {
    return { width: measuredWidth, height: measuredHeight, measured: true };
  }
  const width = node.provisionalWidth ?? 0;
  const height = node.provisionalHeight ?? 0;
  if (width > 0 && height > 0) return { width, height, measured: false };
  throw new Error(`table ${node.id} has no measured or persisted size`);
}

export function buildLayoutSnapshot(input: BuildSnapshotInput): LayoutSnapshot {
  const nodes: LayoutNodeSnapshot[] = [];
  const nodeIds = new Set<string>();

  for (const node of input.nodes) {
    if (nodeIds.has(node.id)) throw new Error(`duplicate table id: ${node.id}`);
    if (!Number.isFinite(node.position.x) || !Number.isFinite(node.position.y)) {
      throw new Error(`table ${node.id} has an invalid position`);
    }
    const size = resolveSize(node);
    nodeIds.add(node.id);
    nodes.push({
      id: node.id,
      x: node.position.x,
      y: node.position.y,
      width: size.width,
      height: size.height,
      tableType: node.tableType,
      pinned: node.pinned === true,
      fixed: false,
      selected: node.selected === true,
      measured: size.measured,
    });
  }

  const edges: LayoutEdgeSnapshot[] = [];
  const edgeIds = new Set<string>();
  for (const edge of input.edges) {
    // Intentional-hidden-endpoint filtering: no node is invented for a join
    // whose endpoint is not on this canvas (spec §4).
    if (!nodeIds.has(edge.source) || !nodeIds.has(edge.target)) continue;
    if (edgeIds.has(edge.id)) throw new Error(`duplicate relationship id: ${edge.id}`);
    edgeIds.add(edge.id);

    const waypoints = edge.waypoints ?? (edge.waypoint ? [edge.waypoint] : []);
    const kinds = resolveMarkerKinds({
      sourceIsFact: edge.sourceIsFact,
      targetIsFact: edge.targetIsFact,
      sourceIsDim: edge.sourceIsDim,
      targetIsDim: edge.targetIsDim,
      override: edge.terminalOverride,
    });
    const routeMode: RouteMode = edge.routeMode ?? (waypoints.length ? "manual" : "auto");
    // Same rule as the renderer: an explicit per-edge override wins, otherwise
    // the global preference applies. Resolving here keeps the worker geometry in
    // lock-step with the edge the user actually sees.
    const pathMode: PathMode = edge.pathing ?? input.globalPathing ?? "orthogonal";

    edges.push({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      sourceColumn: edge.sourceColumn ?? null,
      targetColumn: edge.targetColumn ?? null,
      sourceSide: validSide(edge.sourceSide),
      targetSide: validSide(edge.targetSide),
      sourceRatio: finiteRatio(edge.sourceRatio),
      targetRatio: finiteRatio(edge.targetRatio),
      offsetIndex: edge.offsetIndex ?? 0,
      totalEdges: edge.totalEdges ?? 1,
      pathMode,
      routeMode,
      waypoints: waypoints.map((point) => ({ x: point.x, y: point.y })),
      locked: edge.locked === true,
      sourceMarkerExtent: markerExtent(kinds.source, edge.notation),
      targetMarkerExtent: markerExtent(kinds.target, edge.notation),
    });
  }

  return {
    scope: { projectId: input.projectId, modelId: input.modelId },
    revision: input.revision,
    nodes,
    edges,
    options: {
      preset: input.options.preset ?? "hierarchical",
      direction: input.options.direction ?? "DOWN",
      spacing: input.options.spacing ?? "normal",
    },
  };
}
