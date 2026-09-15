/**
 * Map canvas edge data onto the renderer's drawn-route resolution.
 *
 * `edgeRouting.resolveDisplayedRoute` takes geometry; this is the translation
 * from what the canvas actually holds — React Flow edge data, the global
 * pathing and notation preferences, terminal overrides — into that geometry.
 *
 * It exists as its own module because two features need the drawn route and
 * neither is the renderer: locking a relationship captures it, and the
 * locked-route guard tests a moved card against it. Repeating the translation
 * at each call site is how the drawn route and the frozen route drift apart.
 */
import { markerExtent, resolveMarkerKinds, type RelationNotation } from "./docking";
import {
  type DisplayedRoute,
  resolveDisplayedRoute,
} from "../edgeGeometry";
import type { CrowsFootEdgeData } from "../CrowsFootEdge";
import type { PathMode, Rect, RouteMode } from "./types";

export interface CanvasEdgeLike {
  id: string;
  source: string;
  target: string;
  data?: CrowsFootEdgeData;
}

export interface DisplayedRouteContext {
  /** Fallback path mode for an edge carrying no explicit override. */
  globalPathing?: PathMode;
  notation: RelationNotation;
  terminalOverrides?: Record<string, { source: string; target: string }>;
}

export interface ResolvedCanvasRoute {
  route: DisplayedRoute;
  pathMode: PathMode;
  /** Provenance of the stored bends, resolved the same way the renderer does. */
  routeMode: RouteMode;
}

/**
 * The route this relationship is currently drawn with, or `null` when either
 * endpoint has not been measured — an unmeasured card means there is no drawn
 * route yet, which is a real state during hydration and not an error.
 */
export function displayedRouteFor(
  edge: CanvasEdgeLike,
  cards: ReadonlyMap<string, Rect>,
  context: DisplayedRouteContext,
): ResolvedCanvasRoute | null {
  const source = cards.get(edge.source);
  const target = cards.get(edge.target);
  if (!source || !target) return null;

  const data = edge.data ?? ({} as CrowsFootEdgeData);
  const stored = data.waypoints ?? (data.waypoint ? [data.waypoint] : []);
  const routeMode: RouteMode = data.routeMode ?? (stored.length ? "manual" : "auto");
  const pathMode: PathMode = data.pathing ?? context.globalPathing ?? "orthogonal";
  const kinds = resolveMarkerKinds({
    sourceIsFact: data.sourceIsFact === true,
    targetIsFact: data.targetIsFact === true,
    sourceIsDim: data.sourceIsDim === true,
    targetIsDim: data.targetIsDim === true,
    override: context.terminalOverrides?.[edge.id],
  });

  const obstacles: Rect[] = [];
  for (const [id, rect] of cards) {
    if (id !== edge.source && id !== edge.target) obstacles.push(rect);
  }

  return {
    route: resolveDisplayedRoute({
      source,
      target,
      sourceSide: data.sourceSide,
      targetSide: data.targetSide,
      sourceRatio: data.sourceRatio,
      targetRatio: data.targetRatio,
      waypoints: stored,
      routeMode,
      pathMode,
      sourceMarkerExtent: markerExtent(kinds.source, context.notation),
      targetMarkerExtent: markerExtent(kinds.target, context.notation),
      obstacles,
      offsetIndex: data.offsetIndex,
      totalEdges: data.totalEdges,
      // A locked route keeps the fan-out it was frozen with, so adding or
      // removing a parallel relationship cannot move a frozen attachment.
      frozenParallelOffset: data.lockedParallelOffset,
    }),
    pathMode,
    routeMode,
  };
}

/** The drawn polyline, heel to heel — what the user sees as the connector. */
export function routePolyline(route: DisplayedRoute) {
  return [route.heelSource, ...route.waypoints, route.heelTarget];
}
